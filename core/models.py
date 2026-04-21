from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def mid_price(self) -> Optional[float]:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return None

    def spread(self) -> Optional[float]:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is not None and ask is not None:
            return ask - bid
        return None

    def liquidity_at_levels(self, depth: int = 5) -> dict:
        bid_liq = sum(l.price * l.size for l in self.bids[:depth])
        ask_liq = sum(l.price * l.size for l in self.asks[:depth])
        return {"bid": bid_liq, "ask": ask_liq, "total": bid_liq + ask_liq}


@dataclass
class Position:
    token_id: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    usdc_spent: float = 0.0
    realized_pnl: float = 0.0

    def net_exposure(self, fair_value: float) -> float:
        return self.yes_shares * fair_value + self.no_shares * (1 - fair_value)

    def unrealized_pnl(self, fair_value: float) -> float:
        value = self.yes_shares * fair_value + self.no_shares * (1 - fair_value)
        return value - self.usdc_spent


@dataclass
class OpenOrder:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    size_matched: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    created_at: float = field(default_factory=time.time)

    @property
    def size_remaining(self) -> float:
        return self.size - self.size_matched


@dataclass
class Trade:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    fee: float
    timestamp: float = field(default_factory=time.time)
    is_maker: bool = True


@dataclass
class FairValueEstimate:
    fair_value: float
    confidence: str  # "high", "medium", "low"
    spread_factor: float
    reasoning: str
    timestamp: float = field(default_factory=time.time)

    def is_stale(self, max_age_s: float = 300) -> bool:
        return (time.time() - self.timestamp) > max_age_s


@dataclass
class MarketState:
    token_id: str
    order_book: Optional[OrderBook] = None
    fair_value: Optional[FairValueEstimate] = None
    position: Position = field(default_factory=lambda: Position(""))
    open_orders: list[OpenOrder] = field(default_factory=list)
    daily_pnl: float = 0.0
    daily_start_balance: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self):
        if self.position.token_id == "":
            self.position = Position(self.token_id)
