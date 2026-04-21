import asyncio
import json
import logging
import time
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from core.models import OrderBook, PriceLevel

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/"


class WebSocketClient:
    def __init__(
        self,
        token_id: str,
        on_book_update: Callable[[OrderBook], None],
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ):
        self.token_id = token_id
        self.on_book_update = on_book_update
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase

        self._ws = None
        self._running = False
        self._reconnect_delay = 1.0
        self._order_book = OrderBook(token_id=token_id)

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._connect_and_run()
                self._reconnect_delay = 1.0
            except Exception as e:
                if not self._running:
                    break
                logger.warning("WebSocket disconnected: %s — reconnecting in %.1fs", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_and_run(self):
        url = WS_URL + "market"
        logger.info("Connecting to WebSocket: %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
            await self._subscribe(ws)
            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle_message(raw)
                except Exception as e:
                    logger.debug("Error handling WS message: %s", e)

    async def _subscribe(self, ws):
        msg = {
            "auth": {},
            "markets": [self.token_id],
            "assets_ids": [self.token_id],
            "type": "market",
        }
        if self.api_key:
            msg["auth"] = {
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "passphrase": self.api_passphrase,
            }
        await ws.send(json.dumps(msg))
        logger.info("Subscribed to market %s", self.token_id)

    def _handle_message(self, raw: str):
        events = json.loads(raw)
        if not isinstance(events, list):
            events = [events]

        for event in events:
            event_type = event.get("event_type") or event.get("type", "")
            if event_type == "book":
                self._apply_snapshot(event)
            elif event_type == "price_change":
                self._apply_delta(event)
            elif event_type == "tick_size_change":
                pass  # ignore

    def _apply_snapshot(self, event: dict):
        bids = [
            PriceLevel(price=float(b["price"]), size=float(b["size"]))
            for b in event.get("buys", [])
        ]
        asks = [
            PriceLevel(price=float(a["price"]), size=float(a["size"]))
            for a in event.get("sells", [])
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        self._order_book = OrderBook(
            token_id=self.token_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )
        self.on_book_update(self._order_book)

    def _apply_delta(self, event: dict):
        changes = event.get("changes", [])
        bids = {l.price: l for l in self._order_book.bids}
        asks = {l.price: l for l in self._order_book.asks}

        for change in changes:
            price = float(change["price"])
            size = float(change["size"])
            side = change.get("side", "").upper()

            if side == "BUY":
                if size == 0:
                    bids.pop(price, None)
                else:
                    bids[price] = PriceLevel(price=price, size=size)
            elif side == "SELL":
                if size == 0:
                    asks.pop(price, None)
                else:
                    asks[price] = PriceLevel(price=price, size=size)

        self._order_book.bids = sorted(bids.values(), key=lambda x: x.price, reverse=True)
        self._order_book.asks = sorted(asks.values(), key=lambda x: x.price)
        self._order_book.timestamp = time.time()
        self.on_book_update(self._order_book)

    def get_order_book(self) -> OrderBook:
        return self._order_book
