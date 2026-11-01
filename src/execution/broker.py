"""Paper / live execution boundary.

Both brokers return the same `EntryFill`, so the strategy and accounting do
not care which one is active. Paper fills are simulated from the live order
book when it is available (walking the ask side up to the FAK limit), which
is the closest offline analogue of a taker order.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.config import Settings
from src.execution.clob_client import ClobClient
from src.infra.logging import get_logger
from src.market.market_service import Quote
from src.market.models import MarketInfo, Side
from src.persistence.db import DB
from src.strategy.late_favorite import StrategyParams, taker_fee

log = get_logger(__name__)


@dataclass
class EntryFill:
    ok: bool
    shares: float = 0.0
    cost: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    fee_rate: float = 0.0
    order_id: str = ""
    error: str = ""
    partial: bool = False
    extra: dict = field(default_factory=dict)


class Broker:
    mode = "paper"

    async def buy(
        self,
        market: MarketInfo,
        side: Side,
        signal_price: float,
        stake: float,
        limit_price: float,
        quote: Optional[Quote] = None,
    ) -> EntryFill:  # pragma: no cover - interface
        raise NotImplementedError

    async def balance(self) -> Optional[float]:  # pragma: no cover
        return None

    async def close(self) -> None:
        return None


def resolve_fee_rate(settings: Settings, market: MarketInfo) -> float:
    """Effective taker fee rate for this market.

    The market's own ``feeRate`` (Gamma) wins when present and sane -- that
    is the field the strategy doc says to re-check per market. Otherwise the
    configured category rate (0.07 for crypto) is used. A missing value is
    NOT treated as zero: that would silently overstate PnL.

    ``FEE_RATE_AUTODETECT`` controls a different thing: whether the
    MarketService is allowed to overwrite this field from the CLOB
    ``/fee-rate`` endpoint, whose base_fee unit is ambiguous.
    """
    if market.fee_rate and 0 < market.fee_rate < 0.5:
        return float(market.fee_rate)
    return float(settings.TAKER_FEE_RATE)


class PaperBroker(Broker):
    mode = "paper"

    def __init__(self, settings: Settings, db: DB) -> None:
        self._settings = settings
        self._db = db

    async def balance(self) -> Optional[float]:
        settled = self._db.all_settled_trades("paper")
        realized = sum(float(t.get("pnl") or 0) for t in settled)
        open_trades = self._db.open_trades("paper")
        committed = sum(float(t.get("cost") or 0) + float(t.get("fee") or 0)
                        for t in open_trades)
        return float(self._settings.PAPER_START_BALANCE) + realized - committed

    async def buy(
        self,
        market: MarketInfo,
        side: Side,
        signal_price: float,
        stake: float,
        limit_price: float,
        quote: Optional[Quote] = None,
    ) -> EntryFill:
        book = quote.book(side) if quote is not None else None
        fee_rate = resolve_fee_rate(self._settings, market)

        shares = 0.0
        cost = 0.0
        if self._settings.PAPER_USE_BOOK and book is not None and book.asks:
            shares, avg_price = book.fill_for_notional(stake, limit_price)
            if shares > 0:
                cost = shares * avg_price
                partial = cost < stake - 1e-6
                if partial:
                    log.warning("paper.partial_fill", side=side.value,
                                requested=stake, filled=round(cost, 4),
                                available=round(book.depth_at_or_below(limit_price), 4))
                fill_price = avg_price
                fee = taker_fee(shares, fill_price, fee_rate)
                return EntryFill(
                    True, shares=shares, cost=cost, avg_price=fill_price, fee=fee,
                    fee_rate=fee_rate, order_id=f"paper-{int(time.time()*1000)}",
                    partial=partial,
                    extra={"source": "book", "limit": limit_price,
                           "depth": round(book.depth_at_or_below(limit_price), 4)},
                )

        # No book (or no liquidity at the limit): fall back to the signal
        # price plus the configured paper slippage, capped by the limit.
        fill_price = min(limit_price, signal_price + self._settings.PAPER_SLIPPAGE)
        fill_price = round(max(fill_price, 0.01), 4)
        if fill_price <= 0 or fill_price >= 1:
            return EntryFill(False, error="paper_invalid_price")
        shares = stake / fill_price
        cost = stake
        fee = taker_fee(shares, fill_price, fee_rate)
        return EntryFill(
            True, shares=shares, cost=cost, avg_price=fill_price, fee=fee,
            fee_rate=fee_rate, order_id=f"paper-{int(time.time()*1000)}",
            extra={"source": "signal", "limit": limit_price},
        )


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, settings: Settings, clob: ClobClient) -> None:
        self._settings = settings
        self._clob = clob

    async def balance(self) -> Optional[float]:
        return await self._clob.get_balance()

    async def buy(
        self,
        market: MarketInfo,
        side: Side,
        signal_price: float,
        stake: float,
        limit_price: float,
        quote: Optional[Quote] = None,
    ) -> EntryFill:
        fee_rate = resolve_fee_rate(self._settings, market)
        token_id = market.token_for(side)
        result = await self._clob.place_fak_buy(
            token_id=token_id,
            notional=stake,
            limit_price=limit_price,
            tick_size=market.tick_size,
        )
        if not result.filled:
            return EntryFill(False, error=result.error or "no_fill")

        fee = result.fee
        if fee is None:
            fee = taker_fee(result.shares, result.avg_price, fee_rate)
        return EntryFill(
            True,
            shares=result.shares,
            cost=result.cost,
            avg_price=result.avg_price,
            fee=fee,
            fee_rate=fee_rate,
            order_id=result.order_id,
            partial=result.cost < stake * 0.98,
            extra={"source": "clob", "limit": limit_price, "signal_price": signal_price},
        )


def build_broker(settings: Settings, db: DB, clob: Optional[ClobClient]) -> Broker:
    if settings.is_live:
        if clob is None or not clob.is_configured:
            raise RuntimeError(
                "实盘模式需要可用的 CLOB 凭证："
                + (clob.init_error if clob else "client not created")
            )
        return LiveBroker(settings, clob)
    return PaperBroker(settings, db)
