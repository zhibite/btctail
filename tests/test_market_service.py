"""MarketService quote source: shared WS cache first, REST fallback."""
from __future__ import annotations

import time

import pytest

from src.market.market_service import MarketService
from src.market.market_ws import PriceCache
from src.market.models import BookLevel, MarketInfo, OrderBook


UP = "up-token"
DOWN = "down-token"


def make_market() -> MarketInfo:
    return MarketInfo(
        asset="BTC", period="15m", slot=100, slug="btc-updown-15m-100",
        condition_id="0xabc", token_up=UP, token_down=DOWN, end_ts=1000,
    )


class FakePM:
    def __init__(self) -> None:
        self.mid_calls: list[list[str]] = []
        self.last_calls: list[list[str]] = []
        self.book_calls: list[list[str]] = []
        self.mids = {UP: 0.61, DOWN: 0.39}
        self.lasts = {UP: 0.615, DOWN: 0.385}

    async def get_midpoints(self, token_ids):
        self.mid_calls.append(list(token_ids))
        return {token: self.mids[token] for token in token_ids if token in self.mids}

    async def get_last_trade_prices(self, token_ids):
        self.last_calls.append(list(token_ids))
        return {token: self.lasts[token] for token in token_ids if token in self.lasts}

    async def get_order_books(self, token_ids):
        self.book_calls.append(list(token_ids))
        books = {}
        for token in token_ids:
            books[token] = OrderBook(
                token_id=token,
                bids=[BookLevel(0.60, 100.0)],
                asks=[BookLevel(0.62, 100.0)],
            )
        return books


def seeded_cache() -> PriceCache:
    cache = PriceCache()
    now = time.time()
    cache.best_bid[UP] = 0.90
    cache.best_ask[UP] = 0.92
    cache.best_bid[DOWN] = 0.08
    cache.best_ask[DOWN] = 0.10
    cache.top_at[UP] = now
    cache.top_at[DOWN] = now
    cache.last_trade[UP] = 0.915
    cache.last_trade[DOWN] = 0.085
    cache.last_at[UP] = now
    cache.last_at[DOWN] = now
    return cache


@pytest.mark.asyncio
async def test_fresh_ws_quote_uses_no_rest_prices():
    pm = FakePM()
    service = MarketService(pm, seeded_cache(), ws_max_age=5.0)
    quote = await service.fetch_quote(make_market())
    assert quote.up_mid == pytest.approx(0.91)
    assert quote.down_mid == pytest.approx(0.09)
    assert quote.up_last == pytest.approx(0.915)
    assert quote.down_last == pytest.approx(0.085)
    assert pm.mid_calls == []
    assert pm.last_calls == []
    assert pm.book_calls == []


@pytest.mark.asyncio
async def test_stale_ws_quote_falls_back_to_rest():
    cache = seeded_cache()
    cache.top_at[UP] = cache.top_at[DOWN] = time.time() - 30
    # Last-trade quotes get a longer grace period; force them stale too.
    cache.last_at[UP] = cache.last_at[DOWN] = time.time() - 120
    pm = FakePM()
    service = MarketService(pm, cache, ws_max_age=5.0)
    quote = await service.fetch_quote(make_market())
    assert quote.up_mid == pytest.approx(0.61)
    assert quote.down_mid == pytest.approx(0.39)
    assert quote.up_last == pytest.approx(0.615)
    assert pm.mid_calls == [[UP, DOWN]]
    assert pm.last_calls == [[UP, DOWN]]
    # Missing WS top-of-book also triggers a REST book fallback.
    assert pm.book_calls == [[UP, DOWN]]


@pytest.mark.asyncio
async def test_partial_ws_quote_only_rests_for_missing_side():
    cache = seeded_cache()
    for table in (cache.best_bid, cache.best_ask, cache.top_at,
                  cache.last_trade, cache.last_at):
        table.pop(DOWN, None)
    pm = FakePM()
    service = MarketService(pm, cache, ws_max_age=5.0)
    quote = await service.fetch_quote(make_market())
    assert quote.up_mid == pytest.approx(0.91)
    assert quote.up_last == pytest.approx(0.915)
    assert quote.down_mid == pytest.approx(0.39)
    assert quote.down_last == pytest.approx(0.385)
    assert pm.mid_calls == [[DOWN]]
    assert pm.last_calls == [[DOWN]]
    assert pm.book_calls == [[UP, DOWN]]


@pytest.mark.asyncio
async def test_last_trade_has_a_longer_cache_grace():
    cache = seeded_cache()
    cache.last_at[UP] = cache.last_at[DOWN] = time.time() - 30
    pm = FakePM()
    service = MarketService(pm, cache, ws_max_age=5.0)
    quote = await service.fetch_quote(make_market())
    assert quote.up_last == pytest.approx(0.915)
    assert quote.down_last == pytest.approx(0.085)
    assert pm.mid_calls == []
    assert pm.last_calls == []


@pytest.mark.asyncio
async def test_execution_still_fetches_rest_books():
    pm = FakePM()
    service = MarketService(pm, seeded_cache(), ws_max_age=5.0)
    quote = await service.fetch_quote(make_market(), with_books=True,
                                      ws_max_age=2.0)
    assert quote.up_mid == pytest.approx(0.91)
    assert pm.mid_calls == []
    assert pm.book_calls == [[UP, DOWN]]
    assert quote.up_book is not None


@pytest.mark.asyncio
async def test_entry_uses_shorter_ws_freshness():
    cache = seeded_cache()
    three_seconds_ago = time.time() - 3
    cache.top_at[UP] = cache.top_at[DOWN] = three_seconds_ago
    pm = FakePM()
    service = MarketService(pm, cache, ws_max_age=5.0)
    quote = await service.fetch_quote(make_market(), ws_max_age=2.0)
    assert quote.up_mid == pytest.approx(0.61)
    assert pm.mid_calls == [[UP, DOWN]]
