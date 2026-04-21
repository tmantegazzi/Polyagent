import logging
import time
from typing import Optional

from config import Config
from core.models import MarketState, FairValueEstimate

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config: Config):
        self._config = config

    def check_can_quote(self, state: MarketState, usdc_balance: float) -> tuple[bool, str]:
        if state.halted:
            return False, state.halt_reason

        drawdown_breach, reason = self._check_daily_drawdown(state, usdc_balance)
        if drawdown_breach:
            state.halted = True
            state.halt_reason = reason
            logger.critical("CIRCUIT BREAKER TRIGGERED: %s", reason)
            return False, reason

        if state.order_book is None:
            return False, "No order book data"

        liq = state.order_book.liquidity_at_levels(5)
        if liq["total"] < self._config.min_liquidity:
            return False, f"Insufficient market liquidity: {liq['total']:.0f} < {self._config.min_liquidity}"

        if state.fair_value is None or state.fair_value.is_stale(max_age_s=600):
            return False, "Fair value estimate is stale or missing"

        return True, ""

    def _check_daily_drawdown(self, state: MarketState, usdc_balance: float) -> tuple[bool, str]:
        if state.daily_start_balance <= 0:
            return False, ""

        drawdown = (state.daily_start_balance - usdc_balance) / state.daily_start_balance
        if drawdown >= self._config.max_daily_drawdown:
            reason = f"Daily drawdown {drawdown:.2%} >= max {self._config.max_daily_drawdown:.2%}"
            return True, reason
        return False, ""

    def compute_order_size(
        self,
        side_capital: float,
        fair_value: FairValueEstimate,
        level: int,
    ) -> float:
        max_size = side_capital * self._config.max_position_size_pct
        size = min(max_size / (level + 1), self._config.max_order_size)
        size = max(size, self._config.min_order_size)
        return round(size, 2)

    def compute_quotes(
        self,
        fair_value: FairValueEstimate,
        state: MarketState,
        level: int = 0,
    ) -> tuple[float, float]:
        fv = fair_value.fair_value
        half_spread = (self._config.base_spread_pct * fair_value.spread_factor) / 2

        # Level offset — each deeper level is wider
        level_offset = level * (half_spread * 0.5)

        # Inventory skew — if long YES, lower bid and raise ask to reduce accumulation
        yes_skew = self._inventory_skew(state)

        bid = round(fv - half_spread - level_offset + yes_skew, 4)
        ask = round(fv + half_spread + level_offset + yes_skew, 4)

        # Clamp to valid probability range with a minimum spread
        bid = max(0.01, min(bid, 0.99))
        ask = max(0.01, min(ask, 0.99))
        if ask <= bid:
            ask = bid + 0.01

        return bid, ask

    def _inventory_skew(self, state: MarketState) -> float:
        if state.fair_value is None:
            return 0.0
        fv = state.fair_value.fair_value
        yes = state.position.yes_shares
        no = state.position.no_shares
        net = yes * fv - no * (1 - fv)
        # Skew proportional to net exposure, capped at half the base spread
        max_skew = self._config.base_spread_pct / 2
        skew = -net * 0.001
        return max(-max_skew, min(skew, max_skew))
