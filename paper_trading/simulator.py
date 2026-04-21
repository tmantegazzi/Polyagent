import logging
import time
import uuid
from typing import Optional

from config import Config
from core.models import MarketState, OrderBook, OpenOrder, Side, OrderStatus, Trade

logger = logging.getLogger(__name__)

PAPER_STARTING_BALANCE = 10_000.0  # USDC


class PaperTrader:
    def __init__(self, config: Config):
        self._config = config
        self._balance = PAPER_STARTING_BALANCE
        self._open_orders: dict[str, OpenOrder] = {}
        self._trades: list[Trade] = []
        self._daily_start = PAPER_STARTING_BALANCE

    def get_balance(self) -> float:
        return self._balance

    def place_order(self, token_id: str, side: Side, price: float, size: float) -> Optional[str]:
        cost = price * size
        if side == Side.BUY and cost > self._balance:
            logger.debug("Paper: insufficient balance for BUY %.4f x %.2f (balance=%.2f)", price, size, self._balance)
            return None

        order_id = str(uuid.uuid4())[:8]
        order = OpenOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
        )
        self._open_orders[order_id] = order
        # Reserve capital for buys
        if side == Side.BUY:
            self._balance -= cost
        logger.debug("Paper: placed %s order %s @ %.4f x %.2f", side.value, order_id, price, size)
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        order = self._open_orders.pop(order_id, None)
        if order and order.side == Side.BUY:
            # Refund reserved capital
            self._balance += order.price * order.size_remaining
        return order is not None

    def cancel_all(self):
        for order_id in list(self._open_orders.keys()):
            self.cancel_order(order_id)

    def process_book_update(self, book: OrderBook, state: MarketState):
        filled_ids = []
        for order_id, order in self._open_orders.items():
            if self._check_fill(order, book, state):
                filled_ids.append(order_id)

        for order_id in filled_ids:
            order = self._open_orders.pop(order_id)
            self._settle_fill(order, state)

    def _check_fill(self, order: OpenOrder, book: OrderBook, state: MarketState) -> bool:
        if order.side == Side.BUY:
            best_ask = book.best_ask()
            return best_ask is not None and order.price >= best_ask
        else:
            best_bid = book.best_bid()
            return best_bid is not None and order.price <= best_bid

    def _settle_fill(self, order: OpenOrder, state: MarketState):
        fill_price = order.price
        size = order.size
        # Simulate maker rebate (0.1% of notional)
        rebate = fill_price * size * 0.001

        if order.side == Side.BUY:
            # Already debited on placement; add shares
            state.position.yes_shares += size
            state.position.usdc_spent += fill_price * size
            self._balance += rebate
        else:
            # SELL: receive USDC for shares
            state.position.yes_shares -= size
            received = fill_price * size + rebate
            self._balance += received
            state.position.usdc_spent -= fill_price * size

        trade = Trade(
            order_id=order.order_id,
            token_id=order.token_id,
            side=order.side,
            price=fill_price,
            size=size,
            fee=-rebate,  # negative fee = rebate
            is_maker=True,
        )
        self._trades.append(trade)
        order.status = OrderStatus.FILLED

        pnl_since_start = self._balance - self._daily_start
        logger.info(
            "Paper FILL: %s %.4f x %.2f | balance=%.2f | day_pnl=%.2f",
            order.side.value,
            fill_price,
            size,
            self._balance,
            pnl_since_start,
        )
        state.daily_pnl = pnl_since_start

    def get_summary(self) -> dict:
        total_volume = sum(t.price * t.size for t in self._trades)
        total_rebates = sum(-t.fee for t in self._trades if t.fee < 0)
        return {
            "balance": self._balance,
            "starting_balance": self._daily_start,
            "pnl": self._balance - self._daily_start,
            "pnl_pct": (self._balance - self._daily_start) / self._daily_start * 100,
            "total_trades": len(self._trades),
            "total_volume": total_volume,
            "total_rebates": total_rebates,
        }
