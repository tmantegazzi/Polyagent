import asyncio
import logging
import time
from typing import Optional

from config import Config
from core.models import MarketState, OrderBook, OpenOrder, Side
from core.clob import AsyncClobClient
from core.ws_client import WebSocketClient
from market_maker.risk import RiskManager
from ai.valuation import ValuationEngine
from paper_trading.simulator import PaperTrader

logger = logging.getLogger(__name__)


class MarketMakerEngine:
    def __init__(
        self,
        config: Config,
        clob: AsyncClobClient,
        risk: RiskManager,
        valuation: ValuationEngine,
    ):
        self._config = config
        self._clob = clob
        self._risk = risk
        self._valuation = valuation
        self._state = MarketState(token_id=config.target_token_id)
        self._paper = PaperTrader(config) if config.paper_trading else None
        self._ws: Optional[WebSocketClient] = None
        self._last_ai_update = 0.0

    def _on_book_update(self, book: OrderBook):
        self._state.order_book = book
        if self._paper:
            self._paper.process_book_update(book, self._state)

    async def start(self):
        if not self._config.target_token_id:
            raise ValueError("TARGET_TOKEN_ID is not set in .env")

        mode = "PAPER" if self._config.paper_trading else "LIVE"
        logger.info("Starting market maker in %s mode for token %s", mode, self._config.target_token_id)

        if not self._config.paper_trading:
            await self._clob.connect()

        self._ws = WebSocketClient(
            token_id=self._config.target_token_id,
            on_book_update=self._on_book_update,
            api_key=self._config.api_key,
            api_secret=self._config.api_secret,
            api_passphrase=self._config.api_passphrase,
        )

        # Initialize daily start balance
        balance = await self._get_balance()
        self._state.daily_start_balance = balance
        logger.info("Starting balance: %.2f USDC", balance)

        # Fetch initial market context for AI
        market_info = {}
        if not self._config.paper_trading:
            market_info = await self._clob.get_market_info(self._config.target_token_id)

        await asyncio.gather(
            self._ws.start(),
            self._fast_loop(),
            self._slow_loop(market_info),
        )

    async def _fast_loop(self):
        interval = self._config.cancel_replace_interval_ms / 1000.0
        # Wait for first order book snapshot
        while self._state.order_book is None:
            await asyncio.sleep(0.1)

        while True:
            t0 = time.monotonic()
            try:
                await self._cancel_replace_cycle()
            except Exception as e:
                logger.error("Fast loop error: %s", e)

            elapsed = time.monotonic() - t0
            sleep_for = max(0, interval - elapsed)
            await asyncio.sleep(sleep_for)

    async def _slow_loop(self, market_info: dict):
        while True:
            try:
                await self._update_fair_value(market_info)
            except Exception as e:
                logger.error("Slow loop error: %s", e)
            await asyncio.sleep(self._config.ai_update_interval_s)

    async def _update_fair_value(self, market_info: dict):
        book = self._state.order_book
        if book is None:
            return

        estimate = await self._valuation.estimate_fair_value(
            token_id=self._config.target_token_id,
            order_book=book,
            position=self._state.position,
            market_info=market_info,
        )
        self._state.fair_value = estimate
        self._last_ai_update = time.time()
        logger.info(
            "AI fair value: %.4f (confidence=%s, spread_factor=%.2f)",
            estimate.fair_value,
            estimate.confidence,
            estimate.spread_factor,
        )

    async def _cancel_replace_cycle(self):
        balance = await self._get_balance()
        can_quote, reason = self._risk.check_can_quote(self._state, balance)

        if not can_quote:
            logger.warning("Quoting paused: %s", reason)
            await self._cancel_all_open_orders()
            return

        await self._cancel_all_open_orders()
        await self._place_quotes(balance)

    async def _cancel_all_open_orders(self):
        if self._config.paper_trading:
            if self._paper:
                self._paper.cancel_all()
            self._state.open_orders.clear()
            return

        open_orders = await self._clob.get_open_orders(self._config.target_token_id)
        if open_orders:
            await self._clob.cancel_all_orders()
        self._state.open_orders.clear()

    async def _place_quotes(self, balance: float):
        fv = self._state.fair_value
        if fv is None:
            return

        side_capital = balance / 2
        tasks = []

        for level in range(self._config.order_levels):
            bid_price, ask_price = self._risk.compute_quotes(fv, self._state, level)
            size = self._risk.compute_order_size(side_capital, fv, level)

            if size < self._config.min_order_size:
                continue

            tasks.append(self._place_single_order(Side.BUY, bid_price, size))
            tasks.append(self._place_single_order(Side.SELL, ask_price, size))

        await asyncio.gather(*tasks)

    async def _place_single_order(self, side: Side, price: float, size: float):
        if self._config.paper_trading and self._paper:
            order_id = self._paper.place_order(self._config.target_token_id, side, price, size)
            if order_id:
                self._state.open_orders.append(
                    OpenOrder(
                        order_id=order_id,
                        token_id=self._config.target_token_id,
                        side=side,
                        price=price,
                        size=size,
                    )
                )
            return

        order_id = await self._clob.place_limit_order(
            token_id=self._config.target_token_id,
            side=side,
            price=price,
            size=size,
        )
        if order_id:
            self._state.open_orders.append(
                OpenOrder(
                    order_id=order_id,
                    token_id=self._config.target_token_id,
                    side=side,
                    price=price,
                    size=size,
                )
            )

    async def _get_balance(self) -> float:
        if self._config.paper_trading and self._paper:
            return self._paper.get_balance()
        return await self._clob.get_usdc_balance()

    def get_state(self) -> MarketState:
        return self._state
