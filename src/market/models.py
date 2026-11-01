"""Market data models (no business logic)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def opposite(self) -> "Side":
        return Side.DOWN if self is Side.UP else Side.UP

    @staticmethod
    def parse(value: str | None) -> "Side | None":
        text = (value or "").strip().upper()
        if text in ("UP", "YES", "1"):
            return Side.UP
        if text in ("DOWN", "NO", "0"):
            return Side.DOWN
        return None


@dataclass
class MarketInfo:
    """One recurring {asset}-updown-{period}-{slot} market."""

    asset: str
    period: str
    slot: int                       # unix seconds, window start
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    end_ts: int
    tick_size: float = 0.01
    neg_risk: bool = False
    fee_rate: Optional[float] = None
    # Filled in from Gamma once the event resolves.
    resolved: Optional[Side] = None
    resolved_at: Optional[float] = None
    volume: float = 0.0

    @property
    def window_start(self) -> datetime:
        return datetime.fromtimestamp(self.slot, tz=timezone.utc)

    @property
    def window_end(self) -> datetime:
        return datetime.fromtimestamp(self.end_ts, tz=timezone.utc)

    def token_for(self, side: Side) -> str:
        return self.token_up if side is Side.UP else self.token_down

    def side_for_token(self, token_id: str) -> Optional[Side]:
        if token_id == self.token_up:
            return Side.UP
        if token_id == self.token_down:
            return Side.DOWN
        return None


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel] = field(default_factory=list)   # best first (desc)
    asks: list[BookLevel] = field(default_factory=list)   # best first (asc)
    ts: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(0.0, self.best_ask - self.best_bid)

    def depth_at_or_below(self, limit_price: float) -> float:
        """USDC of ask liquidity available at <= limit_price."""
        total = 0.0
        for level in self.asks:
            if level.price > limit_price + 1e-9:
                break
            total += level.price * level.size
        return total

    def fill_for_notional(self, notional: float, limit_price: float) -> tuple[float, float]:
        """Walk the ask side for `notional` USDC. Returns (shares, avg_price).

        Shares = 0 when nothing is available at or below `limit_price`.
        """
        remaining = notional
        shares = 0.0
        spent = 0.0
        for level in self.asks:
            if level.price > limit_price + 1e-9 or level.size <= 0:
                continue
            level_notional = level.price * level.size
            take = min(remaining, level_notional)
            take_shares = take / level.price
            shares += take_shares
            spent += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if shares <= 0:
            return 0.0, 0.0
        return shares, spent / shares
