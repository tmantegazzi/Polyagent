"""Self-contained demo of the Polyagent market-making engine.

Runs without any API keys or network access. It synthesizes an order book
around a drifting "true" fair value, feeds it to the RiskManager + PaperTrader,
and prints a human-readable trace of quotes, fills, and P&L.

Run with:
    python demo.py
"""

import asyncio
import logging
import random
import time

from config import Config
from core.models import (
    FairValueEstimate,
    MarketState,
    OrderBook,
    OpenOrder,
    PriceLevel,
    Side,
)
from market_maker.risk import RiskManager
from paper_trading.simulator import PaperTrader


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo")


DEMO_TOKEN_ID = "demo-token-0xDEADBEEF"
TICKS = 45
TICK_INTERVAL_S = 0.2


def demo_config() -> Config:
    return Config(
        anthropic_api_key="demo",
        private_key="demo",
        api_key="",
        api_secret="",
        api_passphrase="",
        chain_id=137,
        host="https://clob.polymarket.com",
        paper_trading=True,
        target_token_id=DEMO_TOKEN_ID,
        max_position_size_pct=0.08,
        min_liquidity=500.0,
        maker_rebate_target=0.001,
        cooldown_period=600,
        max_daily_drawdown=0.10,
        base_spread_pct=0.010,
        cancel_replace_interval_ms=200,
        ai_update_interval_s=5,
        min_order_size=10.0,
        max_order_size=200.0,
        order_levels=3,
    )


class SyntheticMarket:
    """A fake order book that mean-reverts toward a slowly drifting fair value."""

    def __init__(self, token_id: str, fair_value: float = 0.62, seed: int = 42):
        self._token_id = token_id
        self._true_fv = fair_value
        self._rng = random.Random(seed)

    def step(self) -> tuple[OrderBook, float]:
        # Random walk on the true fair value, bounded.
        self._true_fv += self._rng.gauss(0, 0.003)
        self._true_fv = max(0.05, min(0.95, self._true_fv))

        # Construct a plausible order book around true_fv.
        mid_noise = self._rng.gauss(0, 0.003)
        mid = max(0.02, min(0.98, self._true_fv + mid_noise))
        half_spread = self._rng.uniform(0.006, 0.012)

        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for level in range(6):
            tick = 0.005 * (level + 1)
            bid_px = round(max(0.01, mid - half_spread - tick + 0.005), 4)
            ask_px = round(min(0.99, mid + half_spread + tick - 0.005), 4)
            size = round(self._rng.uniform(600, 1500) / (level + 1), 2)
            bids.append(PriceLevel(price=bid_px, size=size))
            asks.append(PriceLevel(price=ask_px, size=size))

        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        book = OrderBook(
            token_id=self._token_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )
        return book, self._true_fv


class MockValuation:
    """Stand-in for the Claude-backed ValuationEngine.

    Returns a fair value close to the synthetic market's true FV, with a bit
    of noise and a confidence that varies with market spread.
    """

    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)

    def estimate(self, true_fv: float, book: OrderBook) -> FairValueEstimate:
        noise = self._rng.gauss(0, 0.005)
        fv = max(0.02, min(0.98, true_fv + noise))
        spread = book.spread() or 0.02
        if spread < 0.015:
            confidence, spread_factor = "high", 1.0
        elif spread < 0.030:
            confidence, spread_factor = "medium", 1.4
        else:
            confidence, spread_factor = "low", 2.0
        return FairValueEstimate(
            fair_value=round(fv, 4),
            confidence=confidence,
            spread_factor=spread_factor,
            reasoning="synthetic estimate (demo)",
        )


def banner(msg: str):
    line = "=" * 64
    print(f"\n{line}\n{msg}\n{line}")


async def run_demo():
    banner("Polyagent Market-Maker Demo (paper trading, no network)")

    config = demo_config()
    risk = RiskManager(config)
    paper = PaperTrader(config)
    valuation = MockValuation()
    market = SyntheticMarket(DEMO_TOKEN_ID)

    state = MarketState(token_id=DEMO_TOKEN_ID)
    state.daily_start_balance = paper.get_balance()
    logger.info("Starting balance: %.2f USDC", state.daily_start_balance)

    # Warm up with a book + first fair value estimate so risk checks pass.
    book, true_fv = market.step()
    state.order_book = book
    state.fair_value = valuation.estimate(true_fv, book)
    logger.info(
        "Initial FV=%.4f (conf=%s, sf=%.2f) | book mid=%.4f",
        state.fair_value.fair_value,
        state.fair_value.confidence,
        state.fair_value.spread_factor,
        book.mid_price(),
    )

    for tick in range(1, TICKS + 1):
        # 1) Market moves.
        book, true_fv = market.step()
        state.order_book = book

        # 2) Process fills against our existing resting quotes.
        paper.process_book_update(book, state)

        # 3) Periodically refresh the AI fair value.
        if tick % 3 == 0:
            state.fair_value = valuation.estimate(true_fv, book)
            logger.info(
                "[tick %02d] FV refresh: %.4f (conf=%s, sf=%.2f)",
                tick,
                state.fair_value.fair_value,
                state.fair_value.confidence,
                state.fair_value.spread_factor,
            )

        # 4) Risk gate + cancel/replace cycle.
        balance = paper.get_balance()
        can_quote, reason = risk.check_can_quote(state, balance)
        if not can_quote:
            logger.warning("[tick %02d] quoting paused: %s", tick, reason)
            paper.cancel_all()
            state.open_orders.clear()
            await asyncio.sleep(TICK_INTERVAL_S)
            continue

        paper.cancel_all()
        state.open_orders.clear()

        side_capital = balance / 2
        placed = []
        for level in range(config.order_levels):
            bid_px, ask_px = risk.compute_quotes(state.fair_value, state, level)
            size = risk.compute_order_size(side_capital, state.fair_value, level)
            if size < config.min_order_size:
                continue
            for side, price in ((Side.BUY, bid_px), (Side.SELL, ask_px)):
                oid = paper.place_order(DEMO_TOKEN_ID, side, price, size)
                if oid:
                    state.open_orders.append(
                        OpenOrder(order_id=oid, token_id=DEMO_TOKEN_ID,
                                  side=side, price=price, size=size)
                    )
                    placed.append((side.value, price, size))

        if tick % 9 == 0 and placed:
            preview = ", ".join(f"{s} {sz:.0f}@{p:.4f}" for s, p, sz in placed[:4])
            logger.info("[tick %02d] quotes: %s%s", tick, preview,
                        " …" if len(placed) > 4 else "")

        await asyncio.sleep(TICK_INTERVAL_S)

    paper.cancel_all()
    summary = paper.get_summary()
    banner("Demo complete — paper trading summary")
    print(f"  Final balance : {summary['balance']:.2f} USDC")
    print(f"  P&L           : {summary['pnl']:+.2f} USDC "
          f"({summary['pnl_pct']:+.3f}%)")
    print(f"  Trades filled : {summary['total_trades']}")
    print(f"  Notional vol  : {summary['total_volume']:.2f} USDC")
    print(f"  Maker rebates : {summary['total_rebates']:.4f} USDC")
    print(f"  YES inventory : {state.position.yes_shares:+.2f} shares")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(run_demo())
    except KeyboardInterrupt:
        pass
