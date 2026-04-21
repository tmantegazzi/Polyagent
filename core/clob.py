import asyncio
import logging
from typing import Optional
from functools import partial

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.constants import BUY, SELL

from config import Config
from core.models import OrderBook, PriceLevel, OpenOrder, Side, OrderStatus

logger = logging.getLogger(__name__)


class AsyncClobClient:
    def __init__(self, config: Config):
        self._config = config
        self._client: Optional[ClobClient] = None
        self._loop = asyncio.get_event_loop()

    def _init_client(self) -> ClobClient:
        creds = None
        if self._config.api_key:
            creds = ApiCreds(
                api_key=self._config.api_key,
                api_secret=self._config.api_secret,
                api_passphrase=self._config.api_passphrase,
            )
        client = ClobClient(
            host=self._config.host,
            chain_id=self._config.chain_id,
            key=self._config.private_key,
            creds=creds,
        )
        if not creds:
            client.set_api_creds(client.create_or_derive_api_creds())
        return client

    async def connect(self):
        self._client = await asyncio.get_event_loop().run_in_executor(
            None, self._init_client
        )
        logger.info("CLOB client initialized")

    def _get_client(self) -> ClobClient:
        if self._client is None:
            raise RuntimeError("CLOB client not initialized — call connect() first")
        return self._client

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    async def get_order_book(self, token_id: str) -> OrderBook:
        client = self._get_client()
        raw = await self._run(client.get_order_book, token_id)
        bids = [PriceLevel(price=float(b.price), size=float(b.size)) for b in (raw.bids or [])]
        asks = [PriceLevel(price=float(a.price), size=float(a.size)) for a in (raw.asks or [])]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def get_fee_rate_bps(self, token_id: str) -> int:
        client = self._get_client()
        try:
            resp = await self._run(client.get_order_book, token_id)
            # Fee rate is embedded in the order book response or fetched separately
            fee_rate = getattr(resp, "market_info", {})
            if hasattr(fee_rate, "get"):
                return int(fee_rate.get("feeRateBps", 0))
        except Exception:
            pass
        # Fallback: query market info endpoint
        try:
            market = await self._run(client.get_market, token_id)
            return int(getattr(market, "fee_rate_bps", 0))
        except Exception:
            logger.warning("Could not fetch feeRateBps for %s, defaulting to 0", token_id)
            return 0

    async def place_limit_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
    ) -> Optional[str]:
        client = self._get_client()

        # Always query fresh fee rate before signing
        fee_rate_bps = await self.get_fee_rate_bps(token_id)

        clob_side = BUY if side == Side.BUY else SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size, 2),
            side=clob_side,
            fee_rate_bps=fee_rate_bps,
        )

        try:
            signed = await self._run(client.create_order, order_args)
            resp = await self._run(client.post_order, signed, OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("order_id")
            logger.info("Placed %s order %s @ %.4f x %.2f", side.value, order_id, price, size)
            return order_id
        except Exception as e:
            logger.error("Failed to place order: %s", e)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        client = self._get_client()
        try:
            await self._run(client.cancel, order_id)
            logger.debug("Cancelled order %s", order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order %s: %s", order_id, e)
            return False

    async def cancel_all_orders(self) -> bool:
        client = self._get_client()
        try:
            await self._run(client.cancel_all)
            logger.info("Cancelled all open orders")
            return True
        except Exception as e:
            logger.error("Failed to cancel all orders: %s", e)
            return False

    async def get_open_orders(self, token_id: Optional[str] = None) -> list[OpenOrder]:
        client = self._get_client()
        try:
            params = {"market": token_id} if token_id else {}
            raw = await self._run(client.get_orders, **params)
            orders = []
            for o in (raw or []):
                side = Side.BUY if getattr(o, "side", "BUY") == "BUY" else Side.SELL
                orders.append(OpenOrder(
                    order_id=str(o.id),
                    token_id=str(getattr(o, "asset_id", token_id or "")),
                    side=side,
                    price=float(o.price),
                    size=float(o.original_size),
                    size_matched=float(getattr(o, "size_matched", 0)),
                    status=OrderStatus.OPEN,
                ))
            return orders
        except Exception as e:
            logger.error("Failed to get open orders: %s", e)
            return []

    async def get_usdc_balance(self) -> float:
        client = self._get_client()
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = await self._run(client.get_balance_allowance, params)
            return float(getattr(resp, "balance", 0)) / 1e6  # USDC has 6 decimals
        except Exception as e:
            logger.error("Failed to get USDC balance: %s", e)
            return 0.0

    async def get_market_info(self, token_id: str) -> dict:
        client = self._get_client()
        try:
            market = await self._run(client.get_market, token_id)
            return market if isinstance(market, dict) else vars(market)
        except Exception as e:
            logger.error("Failed to get market info for %s: %s", token_id, e)
            return {}
