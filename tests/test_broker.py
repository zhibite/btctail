"""Paper execution: book walking, partial fills and the no-book fallback."""
from __future__ import annotations

import pytest

from src.config import Settings
from src.execution.broker import PaperBroker, resolve_fee_rate
from src.market.market_service import Quote
from src.market.models import BookLevel, MarketInfo, OrderBook, Side
from src.persistence.db import DB
from src.strategy.late_favorite import taker_fee


def market(fee_rate=None) -> MarketInfo:
    return MarketInfo(
        asset="BTC", period="15m", slot=0, slug="s", condition_id="c",
        token_up="up", token_down="down", end_ts=900, fee_rate=fee_rate,
    )


def book(asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(token_id="up", asks=[BookLevel(p, s) for p, s in asks])


@pytest.fixture()
def broker(tmp_path):
    settings = Settings(
        DATA_DIR=str(tmp_path), PAPER_START_BALANCE=1000.0,
        PAPER_SLIPPAGE=0.005, PAPER_USE_BOOK=True, TAKER_FEE_RATE=0.07,
        FEE_RATE_AUTODETECT=False,
    )
    db = DB(tmp_path / "b.db")
    yield PaperBroker(settings, db)
    db.close()


async def test_walks_the_book(broker):
    quote = Quote(ts=0, up_mid=0.93, up_book=book([(0.93, 4.0), (0.94, 20.0)]))
    fill = await broker.buy(market(), Side.UP, 0.93, 10.0, 0.95, quote=quote)
    assert fill.ok
    assert fill.cost == pytest.approx(10.0)
    expected_shares = 4.0 + (10.0 - 4.0 * 0.93) / 0.94
    assert fill.shares == pytest.approx(expected_shares)
    assert fill.avg_price == pytest.approx(10.0 / expected_shares)
    assert fill.fee == pytest.approx(taker_fee(expected_shares, fill.avg_price, 0.07))
    assert fill.partial is False


async def test_partial_fill_when_depth_is_thin(broker):
    quote = Quote(ts=0, up_mid=0.93, up_book=book([(0.93, 2.0)]))  # 1.86 USDC
    fill = await broker.buy(market(), Side.UP, 0.93, 10.0, 0.95, quote=quote)
    assert fill.ok
    assert fill.partial is True
    assert fill.cost == pytest.approx(2.0 * 0.93)
    assert fill.shares == pytest.approx(2.0)


async def test_no_fill_when_everything_is_above_the_limit(broker):
    quote = Quote(ts=0, up_mid=0.93, up_book=book([(0.97, 50.0)]))
    fill = await broker.buy(market(), Side.UP, 0.93, 10.0, 0.95, quote=quote)
    # No liquidity at the limit -> fall back to signal + paper slippage.
    assert fill.ok
    assert fill.avg_price == pytest.approx(0.935)
    assert fill.extra["source"] == "signal"


async def test_falls_back_to_signal_price_without_a_book(broker):
    fill = await broker.buy(market(), Side.UP, 0.92, 10.0, 0.94, quote=None)
    assert fill.ok
    assert fill.avg_price == pytest.approx(0.925)
    assert fill.shares == pytest.approx(10 / 0.925)


async def test_signal_price_fallback_respects_the_limit(broker):
    # Slippage would take 0.94 -> 0.945, but the limit caps it at 0.94.
    fill = await broker.buy(market(), Side.UP, 0.94, 10.0, 0.94, quote=None)
    assert fill.avg_price == pytest.approx(0.94)


def test_fee_rate_prefers_market_value(tmp_path):
    settings = Settings(DATA_DIR=str(tmp_path), TAKER_FEE_RATE=0.07,
                        FEE_RATE_AUTODETECT=False)
    assert resolve_fee_rate(settings, market(fee_rate=0.05)) == pytest.approx(0.05)
    assert resolve_fee_rate(settings, market(fee_rate=None)) == pytest.approx(0.07)


async def test_paper_balance_tracks_realized_and_open(broker, tmp_path):
    assert await broker.balance() == pytest.approx(1000.0)
    db = broker._db
    db.create_trade(slot=1, period="15m", mode="paper", side="UP", token_id="up", condition_id="c",
                    signal_price=0.92, entry_price=0.92, shares=10.87, cost=10.0,
                    fee=0.5, fee_rate=0.07, stake=10.0, order_id="o")
    assert await broker.balance() == pytest.approx(1000.0 - 10.5)
