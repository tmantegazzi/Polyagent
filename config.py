import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Anthropic
    anthropic_api_key: str

    # Polymarket credentials
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str

    # Network
    chain_id: int
    host: str

    # Trading mode
    paper_trading: bool
    target_token_id: str

    # Risk
    max_position_size_pct: float
    min_liquidity: float
    maker_rebate_target: float
    cooldown_period: int
    max_daily_drawdown: float

    # Market making
    base_spread_pct: float
    cancel_replace_interval_ms: int
    ai_update_interval_s: int

    # Order sizing
    min_order_size: float
    max_order_size: float
    order_levels: int


def load_config() -> Config:
    return Config(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        private_key=_require("POLYMARKET_PRIVATE_KEY"),
        api_key=os.getenv("POLYMARKET_API_KEY", ""),
        api_secret=os.getenv("POLYMARKET_API_SECRET", ""),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", ""),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        host=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
        paper_trading=os.getenv("PAPER_TRADING", "true").lower() == "true",
        target_token_id=os.getenv("TARGET_TOKEN_ID", ""),
        max_position_size_pct=float(os.getenv("MAX_POSITION_SIZE_PCT", "0.05")),
        min_liquidity=float(os.getenv("MIN_LIQUIDITY", "50000")),
        maker_rebate_target=float(os.getenv("MAKER_REBATE_TARGET", "0.001")),
        cooldown_period=int(os.getenv("COOLDOWN_PERIOD", "600")),
        max_daily_drawdown=float(os.getenv("MAX_DAILY_DRAWDOWN", "0.05")),
        base_spread_pct=float(os.getenv("BASE_SPREAD_PCT", "0.02")),
        cancel_replace_interval_ms=int(os.getenv("CANCEL_REPLACE_INTERVAL_MS", "1000")),
        ai_update_interval_s=int(os.getenv("AI_UPDATE_INTERVAL_S", "120")),
        min_order_size=float(os.getenv("MIN_ORDER_SIZE", "10")),
        max_order_size=float(os.getenv("MAX_ORDER_SIZE", "500")),
        order_levels=int(os.getenv("ORDER_LEVELS", "3")),
    )


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Required environment variable {key} is not set")
    return value
