"""Market discovery + quote fetching.

The strategy signal uses the CLOB **midpoint** (the same series the research
backtest consumed). The best bid/ask are fetched as well so the executor can
measure the real spread and estimate the fill.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from src.infra.logging import get_logger
from src.market.market_ws import PriceCache
from src.market.models import MarketInfo, OrderBook, Side
from src.market.pm_client import PolymarketClient

log = get_logger(__name__)


@dataclass
class Quote:
    ts: float
    up_mid: Optional[float] = None
    down_mid: Optional[float] = None
    up_last: Optional[float] = None
    down_last: Optional[float] = None
    up_bid: Optional[float] = None
    up_ask: Optional[float] = None
    down_bid: Optional[float] = None
    down_ask: Optional[float] = None
    up_book: Optional[OrderBook] = None
    down_book: Optional[OrderBook] = None

    def mid(self, side: Side) -> Optional[float]:
        return self.up_mid if side is Side.UP else self.down_mid

    def last(self, side: Side) -> Optional[float]:
        return self.up_last if side is Side.UP else self.down_last

    def bid(self, side: Side) -> Optional[float]:
        return self.up_bid if side is Side.UP else self.down_bid

    def ask(self, side: Side) -> Optional[float]:
        return self.up_ask if side is Side.UP else self.down_ask

    def book(self, side: Side) -> Optional[OrderBook]:
        return self.up_book if side is Side.UP else self.down_book

    def price(self, side: Side, source: str = "mid") -> Optional[float]:
        if source == "ask":
            value = self.ask(side)
            return value if value is not None else self.mid(side)
        if source == "last":
            value = self.last(side)
            return value if value is not None else self.mid(side)
        return self.mid(side)

    def spread(self, side: Side) -> Optional[float]:
        bid, ask = self.bid(side), self.ask(side)
        if bid is None or ask is None:
            return None
        return max(0.0, ask - bid)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "up_mid": self.up_mid,
            "down_mid": self.down_mid,
            "up_last": self.up_last,
            "down_last": self.down_last,
            "up_bid": self.up_bid,
            "up_ask": self.up_ask,
            "down_bid": self.down_bid,
            "down_ask": self.down_ask,
        }


@dataclass
class _SlotEntry:
    market: MarketInfo
    discovered_at: float = field(default_factory=time.time)
    enriched: bool = False


class MarketService:
    def __init__(self, pm: PolymarketClient, price_cache: PriceCache | None = None,
                 autodetect_fee: bool = False,
                 ws_max_age: float = 5.0) -> None:
        self._pm = pm
        self._ws_cache = price_cache
        self._slots: dict[int, _SlotEntry] = {}
        self._failed_at: dict[int, float] = {}
        self._autodetect_fee = autodetect_fee
        # Only trust a shared-WS quote for this long before falling back to
        # REST. A disconnected/stale socket must not freeze the signal price.
        self._ws_max_age = float(ws_max_age)

    # ── discovery ─────────────────────────────────────────────────────────

    async def discover(self, asset: str, period: str, slot: int,
                       *, force: bool = False) -> Optional[MarketInfo]:
        """Return the market for `slot`, with a short negative cache.

        Gamma can lag the boundary by a few seconds; callers retry every few
        seconds. A failure is cached for 3 s so a burst of ticks cannot turn
        into a burst of Gamma requests.
        """
        cached = self._slots.get(slot)
        if cached is not None and not force:
            return cached.market
        failed_at = self._failed_at.get(slot)
        if failed_at is not None and time.time() - failed_at < 3.0 and not force:
            return None

        market = await self._pm.discover_slot(asset, period, slot)
        if market is None:
            self._failed_at[slot] = time.time()
            return None

        self._failed_at.pop(slot, None)
        entry = _SlotEntry(market=market)
        self._slots[slot] = entry
        await self._enrich(entry)
        # Drop old slots; keep the previous one for resolution lookups.
        for stale in [s for s in self._slots if s < slot - 2 * (300 if period == "5m" else 900)]:
            self._slots.pop(stale, None)
        log.info(
            "market.discovered",
            slot=slot, slug=market.slug, condition_id=market.condition_id[:12],
            up_token=market.token_up[:16], down_token=market.token_down[:16],
            tick_size=market.tick_size, fee_rate=market.fee_rate,
        )
        return market

    def set_autodetect_fee(self, enabled: bool) -> None:
        self._autodetect_fee = bool(enabled)

    async def _enrich(self, entry: _SlotEntry) -> None:
        market = entry.market
        if entry.enriched:
            return
        tick_task = self._pm.get_tick_size(market.token_up)
        if self._autodetect_fee:
            tick, fee = await asyncio.gather(
                tick_task, self._pm.get_fee_rate(market.token_up), return_exceptions=True
            )
            if isinstance(fee, float) and 0 < fee < 0.5:
                if market.fee_rate is None or abs(market.fee_rate - fee) > 1e-9:
                    log.info("market.fee_rate_detected", slot=market.slot,
                             gamma=market.fee_rate, clob=fee)
                market.fee_rate = fee
        else:
            tick = await tick_task
        if isinstance(tick, float) and tick > 0:
            market.tick_size = tick
        entry.enriched = True

    def get_market(self, slot: int) -> Optional[MarketInfo]:
        entry = self._slots.get(slot)
        return entry.market if entry else None

    def known_slots(self) -> list[int]:
        return sorted(self._slots)

    # ── quotes ────────────────────────────────────────────────────────────

    async def fetch_quote(self, market: MarketInfo, *, with_books: bool = False,
                          ws_max_age: float | None = None) -> Quote:
        quote = Quote(ts=time.time())
        max_age = self._ws_max_age if ws_max_age is None else float(ws_max_age)
        # A "last trade" can legitimately be minutes old in a quiet market;
        # the timestamp marks when we learned it. Give it a longer lifetime
        # than top-of-book so we do not poll REST once per second for it.
        last_max_age = max(max_age, 60.0)
        cache = self._ws_cache

        # 1) Shared WebSocket cache first: it is the freshest source and
        # costs no REST call. Only fresh entries are trusted.
        if cache is not None:
            for side, token in ((Side.UP, market.token_up),
                                (Side.DOWN, market.token_down)):
                if cache.fresh(cache.top_at, token, max_age):
                    if side is Side.UP:
                        quote.up_bid = cache.best_bid.get(token)
                        quote.up_ask = cache.best_ask.get(token)
                        quote.up_mid = cache.mid(token)
                    else:
                        quote.down_bid = cache.best_bid.get(token)
                        quote.down_ask = cache.best_ask.get(token)
                        quote.down_mid = cache.mid(token)
                if cache.fresh(cache.last_at, token, last_max_age):
                    if side is Side.UP:
                        quote.up_last = cache.last_trade.get(token)
                    else:
                        quote.down_last = cache.last_trade.get(token)
            quote.up_book = cache.book.get(market.token_up)
            quote.down_book = cache.book.get(market.token_down)

        # 2) REST only for what the cache could not provide.
        missing_mid = [
            token for token, value in (
                (market.token_up, quote.up_mid),
                (market.token_down, quote.down_mid),
            ) if value is None
        ]
        missing_last = [
            token for token, value in (
                (market.token_up, quote.up_last),
                (market.token_down, quote.down_last),
            ) if value is None
        ]
        mids: dict[str, float] = {}
        lasts: dict[str, float] = {}
        try:
            if missing_mid and missing_last:
                mids, lasts = await asyncio.gather(
                    self._pm.get_midpoints(missing_mid),
                    self._pm.get_last_trade_prices(missing_last),
                )
            elif missing_mid:
                mids = await self._pm.get_midpoints(missing_mid)
            elif missing_last:
                lasts = await self._pm.get_last_trade_prices(missing_last)
        except Exception as exc:  # noqa: BLE001
            log.debug("market.quote_fetch_failed", error=str(exc))

        if quote.up_mid is None:
            quote.up_mid = mids.get(market.token_up)
        if quote.down_mid is None:
            quote.down_mid = mids.get(market.token_down)
        if quote.up_last is None:
            quote.up_last = lasts.get(market.token_up)
        if quote.down_last is None:
            quote.down_last = lasts.get(market.token_down)

        # 3) Order books: the shared cache covers normal polling; REST is
        # still used at entry time because the executor needs fresh depth.
        if with_books or quote.up_bid is None or quote.up_ask is None \
                or quote.down_bid is None or quote.down_ask is None:
            books = await self._pm.get_order_books([market.token_up, market.token_down])
            up_book = books.get(market.token_up)
            down_book = books.get(market.token_down)
            if up_book is not None:
                quote.up_book = up_book
                quote.up_bid = up_book.best_bid
                quote.up_ask = up_book.best_ask
            if down_book is not None:
                quote.down_book = down_book
                quote.down_bid = down_book.best_bid
                quote.down_ask = down_book.best_ask

        # A one-sided failure should not blank the whole quote.
        if quote.up_mid is None and quote.up_last is not None:
            quote.up_mid = quote.up_last
        if quote.down_mid is None and quote.down_last is not None:
            quote.down_mid = quote.down_last
        return quote
