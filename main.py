import asyncio
import logging
import signal
import sys

from config import load_config
from core.clob import AsyncClobClient
from market_maker.engine import MarketMakerEngine
from market_maker.risk import RiskManager
from ai.valuation import ValuationEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def run():
    config = load_config()

    mode = "PAPER TRADING" if config.paper_trading else "LIVE TRADING"
    logger.info("=" * 60)
    logger.info("Polymarket Market Maker — %s", mode)
    logger.info("Token: %s", config.target_token_id or "(not set)")
    logger.info("Spread: %.2f%%  Levels: %d  Cancel/replace: %dms",
                config.base_spread_pct * 100, config.order_levels, config.cancel_replace_interval_ms)
    logger.info("=" * 60)

    clob = AsyncClobClient(config)
    risk = RiskManager(config)
    valuation = ValuationEngine(config.anthropic_api_key)

    engine = MarketMakerEngine(
        config=config,
        clob=clob,
        risk=risk,
        valuation=valuation,
    )

    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        await engine.start()
    except asyncio.CancelledError:
        logger.info("Shutting down gracefully")
        if config.paper_trading and engine._paper:
            summary = engine._paper.get_summary()
            logger.info("=== Paper Trading Summary ===")
            logger.info("Final balance:   %.2f USDC", summary["balance"])
            logger.info("P&L:             %.2f USDC (%.2f%%)", summary["pnl"], summary["pnl_pct"])
            logger.info("Total trades:    %d", summary["total_trades"])
            logger.info("Total volume:    %.2f USDC", summary["total_volume"])
            logger.info("Total rebates:   %.4f USDC", summary["total_rebates"])


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
