"""
models.py — Portfolio dataclasses (Tranche, Position, Trade, Portfolio).
No business logic here — only data.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Tranche:
    price:    float
    quantity: int
    time:     str


@dataclass
class Position:
    symbol:   str
    name:     str
    tranches: list = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return sum(t.quantity for t in self.tranches)

    @property
    def avg_price(self) -> float:
        total = self.quantity
        if total == 0:
            return 0.0
        return round(sum(t.price * t.quantity for t in self.tranches) / total, 4)

    @property
    def buy_price(self) -> float:
        return self.avg_price

    @property
    def buy_time(self) -> str:
        return self.tranches[0].time if self.tranches else ""

    @property
    def last_tranche_price(self) -> float:
        return self.tranches[-1].price if self.tranches else 0.0

    @property
    def tranche_count(self) -> int:
        return len(self.tranches)

    @property
    def total_cost(self) -> float:
        return sum(t.price * t.quantity for t in self.tranches)


@dataclass
class Trade:
    time:       str
    symbol:     str
    side:       str
    price:      float
    quantity:   int
    amount:     float
    commission: float
    pnl:        Optional[float]
    reason:     str


@dataclass
class Portfolio:
    starting_balance:  float = 10_000.0
    cash:              float = 10_000.0
    positions:         dict  = field(default_factory=dict)
    trades:            list  = field(default_factory=list)
    day_start:         str   = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    POSITION_SIZE_PCT: float = 0.30

    def total_value(self, current_prices: dict) -> float:
        return self.cash + sum(
            pos.quantity * current_prices.get(sym, pos.avg_price)
            for sym, pos in self.positions.items()
        )

    def total_pnl(self, current_prices: dict) -> float:
        return self.total_value(current_prices) - self.starting_balance

    def pnl_pct(self, current_prices: dict) -> float:
        return self.total_pnl(current_prices) / self.starting_balance * 100
