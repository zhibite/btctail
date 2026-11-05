"""Shared WebSocket cache freshness timestamps."""
from __future__ import annotations

import time

import pytest

from src.market.market_ws import MarketWS, PriceCache


def test_set_updates_top_and_last_timestamps():
    cache = PriceCache()
    ws = MarketWS(cache)
    ws._set(cache.best_bid, "t", 0.90)
    ws._set(cache.best_ask, "t", 0.92)
    assert cache.fresh(cache.top_at, "t", 1.0)
    assert cache.mid("t") == 0.91
    # A last-trade update must not make a stale top-of-book look fresh.
    cache.top_at["t"] = time.time() - 30
    ws._set(cache.last_trade, "t", 0.915)
    assert cache.fresh(cache.last_at, "t", 1.0)
    assert not cache.fresh(cache.top_at, "t", 1.0)


def test_book_snapshot_updates_book_and_top_timestamps():
    cache = PriceCache()
    ws = MarketWS(cache)
    ws._ingest_book("t", {
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.42", "size": "10"}],
    })
    assert cache.fresh(cache.book_at, "t", 1.0)
    assert cache.fresh(cache.top_at, "t", 1.0)
    assert cache.mid("t") == pytest.approx(0.41)
    assert cache.book["t"].best_ask == 0.42


def test_clear_removes_all_timestamp_tables():
    cache = PriceCache()
    ws = MarketWS(cache)
    ws._set(cache.best_bid, "t", 0.4)
    ws._set(cache.last_trade, "t", 0.41)
    ws._ingest_book("t", {"bids": [{"price": "0.4", "size": "1"}],
                           "asks": [{"price": "0.42", "size": "1"}]})
    cache.clear(["t"])
    for table in (cache.best_bid, cache.last_trade, cache.book,
                  cache.updated_at, cache.top_at, cache.last_at, cache.book_at):
        assert "t" not in table
