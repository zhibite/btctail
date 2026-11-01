"""Runtime path: quote -> signal -> broker -> trade row -> settlement."""
from __future__ import annotations

import json

import pytest

from src.config import Settings
from src.execution.broker import Broker, EntryFill, LimitFill, LimitOrder
from src.market.market_service import Quote
from src.market.models import MarketInfo, Side
from src.persistence.db import DB
from src.runtime import ActiveWindow, Runtime
from src.strategy.late_favorite import LateFavoriteStrategy

# Must be aligned to the 900 s boundary that the runtime uses.
SLOT = (1_770_000_000 // 900) * 900
END = SLOT + 900


class FakePM:
    def __init__(self, resolution: Side | None = Side.UP, void: bool = False):
        self.resolution = resolution
        self.void = void
        self.queries = 0

    async def get_market_resolution(self, condition_id, slug):
        self.queries += 1
        return self.resolution

    async def get_market_void(self, condition_id, slug):
        return self.void

    async def close(self):
        return None


class FakeMarketService:
    def __init__(self, market: MarketInfo, quotes: list[Quote]):
        self.market = market
        self.quotes = quotes
        self.index = 0
        self.fetches = 0

    async def discover(self, asset, period, slot, force=False):
        return self.market

    async def get_market(self, slot):
        return self.market if slot == self.market.slot else None

    async def fetch_quote(self, market, with_books=False):
        self.fetches += 1
        quote = self.quotes[min(self.index, len(self.quotes) - 1)]
        self.index += 1
        return quote


class FakeBroker(Broker):
    mode = "paper"

    def __init__(self, fill_price: float | None = None):
        self.calls = []
        self.fill_price = fill_price

    async def balance(self):
        return 1000.0

    async def place_limit_buy(self, market, side, shares, price):
        return LimitOrder(order_id="fake-limit", token_id="t", side=side,
                          price=price, shares=shares, maker=True, mode=self.mode)

    async def limit_fill(self, order, quote=None):
        if quote is None:
            return LimitFill()
        ask = quote.ask(order.side)
        if ask is not None and ask <= order.price + 1e-9:
            return LimitFill(filled=order.shares, avg_price=order.price, done=True)
        return LimitFill()

    async def cancel(self, order):
        return True

    async def buy(self, market, side, signal_price, stake, limit_price, quote=None):
        self.calls.append({
            "side": side, "signal_price": signal_price,
            "stake": stake, "limit": limit_price,
        })
        price = self.fill_price if self.fill_price is not None else min(
            limit_price, signal_price + 0.005
        )
        shares = stake / price
        fee = shares * 0.07 * price * (1 - price)
        return EntryFill(True, shares=shares, cost=stake, avg_price=price,
                         fee=fee, fee_rate=0.07, order_id="fake-1")


def make_market() -> MarketInfo:
    return MarketInfo(
        asset="BTC", period="15m", slot=SLOT,
        slug=f"btc-updown-15m-{SLOT}", condition_id="0xabc123",
        token_up="up-token", token_down="down-token", end_ts=END,
        tick_size=0.01,
    )


def quote(ts: float, up: float, ask: float | None = None) -> Quote:
    up_ask = round(up + 0.01, 4) if ask is None else ask
    return Quote(
        ts=ts, up_mid=up, down_mid=round(1 - up, 4),
        up_bid=round(up - 0.01, 4), up_ask=up_ask,
        down_bid=round(1 - up - 0.01, 4), down_ask=round(1 - up + 0.01, 4),
    )


def build(tmp_path, quotes, broker=None, settings=None):
    settings = settings or Settings(
        DATA_COLLECT_ENABLED=False, DATA_DIR=str(tmp_path), TRADING_MODE="paper",
        MAX_SPREAD_AT_ENTRY=0.10, MIN_WALLET_BALANCE_USD=0,
    )
    db = DB(tmp_path / "rt.db")
    market = make_market()
    pm = FakePM()
    svc = FakeMarketService(market, quotes)
    broker = broker or FakeBroker()
    runtime = Runtime(settings, db, pm, svc, broker, ws=None)
    aw = ActiveWindow(
        slot=SLOT, end_ts=END, market=market,
        strategy=LateFavoriteStrategy(SLOT, END, settings_strategy(settings)),
    )
    runtime.active = aw
    return runtime, db, aw, svc, pm


def set_clock(runtime, ts: float) -> None:
    runtime._clock = lambda: ts


def settings_strategy(settings):
    from src.strategy.late_favorite import StrategyParams

    return StrategyParams(**settings.strategy_params())


async def test_entry_confirm_and_settlement(tmp_path):
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93)]
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    assert aw.strategy.state.phase.value == "CONFIRMING"
    assert db.recent_trades() == []

    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    trades = db.recent_trades()
    assert len(trades) == 1
    trade = trades[0]
    assert trade["side"] == "UP"
    assert trade["signal_price"] == pytest.approx(0.93)
    assert trade["entry_price"] == pytest.approx(0.935)
    assert trade["status"] == "open"
    assert trade["fee"] > 0

    signals = db.recent_signals()
    assert signals[0]["decision"] == "enter"

    runtime._last_settle_poll = 0
    await runtime._settle_open_trades(END + 10)
    settled = db.recent_trades()[0]
    assert settled["status"] == "won"
    assert settled["resolved_side"] == "UP"
    expected = trade["shares"] - trade["cost"] - trade["fee"]
    assert settled["pnl"] == pytest.approx(expected)
    assert pm.queries == 1


async def test_second_tick_does_not_duplicate(tmp_path):
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93)]
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    set_clock(runtime, END - 39)
    await runtime._maybe_refresh_quote(aw, END - 39)
    assert len(db.recent_trades()) == 1


async def test_price_cap_skips(tmp_path):
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.93), quote(END - 40, 0.945), quote(END - 40, 0.945)]
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    assert db.recent_trades() == []
    assert aw.strategy.state.skip_reason == "price_too_high"
    assert db.recent_signals()[0]["skip_reason"] == "price_too_high"


async def test_killswitch_blocks_entry(tmp_path):
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93)]
    )
    runtime.risk.engage_killswitch("test")
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    assert db.recent_trades() == []
    assert "killswitch" in db.recent_signals()[0]["skip_reason"]


async def test_price_reversal_aborts_execution(tmp_path):
    # Signal confirms at 0.93, but by execution time the mid has collapsed.
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.70)]
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    assert db.recent_trades() == []
    assert db.recent_signals()[0]["skip_reason"] == "exec_price_reversed"


async def test_voided_market_settles_as_void(tmp_path):
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93)],
        settings=Settings(
            DATA_COLLECT_ENABLED=False, DATA_DIR=str(tmp_path), TRADING_MODE="paper",
            MAX_SPREAD_AT_ENTRY=0.10, MIN_WALLET_BALANCE_USD=0, SETTLE_TIMEOUT_S=1,
        ),
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    trade = db.recent_trades()[0]
    assert trade["status"] == "open"

    pm.resolution = None
    pm.void = True
    runtime._last_settle_poll = 0
    await runtime._settle_open_trades(END + 60)
    settled = db.recent_trades()[0]
    assert settled["status"] == "void"
    assert settled["resolved_side"] == "VOID"
    assert settled["pnl"] == pytest.approx(-trade["fee"])
    assert settled["payout"] == pytest.approx(trade["cost"])


async def test_gtd_fills_at_the_signal_price_as_maker(tmp_path):
    settings = Settings(
        DATA_COLLECT_ENABLED=False, DATA_DIR=str(tmp_path), TRADING_MODE="paper",
        MAX_SPREAD_AT_ENTRY=0.10, MIN_WALLET_BALANCE_USD=0,
        ENTRY_ORDER_TYPE="gtd", ENTRY_SHARES=5, GTD_FILL_TIMEOUT_S=0.5,
        MAKER_FEE_RATE=0.0,
    )
    # ask == mid at the poll -> the resting buy gets filled by a seller.
    fill_quote = quote(END - 40, 0.93, ask=0.93)
    runtime, db, aw, svc, pm = build(
        tmp_path,
        [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93), fill_quote],
        settings=settings,
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)

    trade = db.recent_trades()[0]
    assert trade["shares"] == pytest.approx(5.0)
    assert trade["entry_price"] == pytest.approx(0.93)
    assert trade["fee"] == pytest.approx(0.0)   # maker fee is zero
    extra = json.loads(trade["extra"])
    assert extra["order_type"] == "gtd"
    assert extra["maker"] is True
    assert extra["source"] == "gtd"


async def test_gtd_unfilled_falls_back_to_fak(tmp_path):
    settings = Settings(
        DATA_COLLECT_ENABLED=False, DATA_DIR=str(tmp_path), TRADING_MODE="paper",
        MAX_SPREAD_AT_ENTRY=0.10, MIN_WALLET_BALANCE_USD=0,
        ENTRY_ORDER_TYPE="gtd", ENTRY_SHARES=5, GTD_FILL_TIMEOUT_S=0.5,
        ENTRY_FALLBACK_FAK=True,
    )
    # ask never touches the resting price, so the GTD cannot fill.
    runtime, db, aw, svc, pm = build(
        tmp_path,
        [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93)],
        settings=settings,
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)

    trade = db.recent_trades()[0]
    extra = json.loads(trade["extra"])
    assert extra["order_type"] == "gtd"
    assert extra["fallback"] == "fak"
    assert trade["shares"] > 0


async def test_set_mode_refused_with_open_position(tmp_path):
    runtime, db, aw, svc, pm = build(
        tmp_path, [quote(END - 50, 0.92), quote(END - 40, 0.93), quote(END - 40, 0.93)]
    )
    set_clock(runtime, END - 50)
    await runtime._maybe_refresh_quote(aw, END - 50)
    set_clock(runtime, END - 40)
    await runtime._maybe_refresh_quote(aw, END - 40)
    assert db.recent_trades()[0]["status"] == "open"
    ok, message = runtime.set_mode("live")
    assert ok is False
    assert "未结算" in message


async def test_set_mode_switches_broker_and_risk(tmp_path):
    runtime, db, aw, svc, pm = build(tmp_path, [quote(END - 50, 0.92)])

    def factory(mode):
        broker = FakeBroker()
        broker.mode = mode
        return broker

    runtime._broker_factory = factory
    ok, message = runtime.set_mode("live")
    assert ok, message
    assert runtime.broker.mode == "live"
    assert runtime.risk.mode == "live"
    assert runtime.settings.TRADING_MODE == "live"
