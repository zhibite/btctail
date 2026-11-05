"""Persistence, stats and risk guards."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from src.config import Settings
from src.persistence.db import DB, compute_stats, equity_curve
from src.strategy.risk import RiskManager


@pytest.fixture()
def db(tmp_path):
    database = DB(tmp_path / "test.db")
    yield database
    database.close()


def _add(db: DB, slot: int, *, status="open", pnl=0.0, side="UP", cost=10.0, fee=0.5,
         entry_ts=None, period="15m", asset="BTC") -> int:
    trade_id = db.create_trade(
        slot=slot, period=period, asset=asset, mode="paper", side=side,
        token_id="t", condition_id="c",
        signal_price=0.92, entry_price=0.92, shares=cost / 0.92, cost=cost, fee=fee,
        fee_rate=0.07, stake=10.0, order_id="o", entry_ts=entry_ts or time.time(),
    )
    if status != "open":
        db.settle_trade(trade_id, resolved_side=side if status == "won" else
                        ("DOWN" if side == "UP" else "UP"),
                        pnl=pnl, payout=cost / 0.92 if status == "won" else 0.0,
                        status=status)
    return trade_id


def test_unique_trade_per_window(db):
    _add(db, 100)
    _add(db, 100)  # ON CONFLICT DO NOTHING
    assert len(db.recent_trades()) == 1
    assert db.trade_exists(100, "paper")


def test_stats_and_equity(db):
    _add(db, 1, status="won", pnl=1.0)
    _add(db, 2, status="lost", pnl=-10.5)
    _add(db, 3, status="won", pnl=1.2)
    _add(db, 4, status="open")
    stats = compute_stats(db.all_settled_trades())
    assert stats["trades"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["pnl"] == pytest.approx(-8.3)
    assert stats["win_rate"] == pytest.approx(2 / 3)
    curve = equity_curve(db.all_settled_trades())
    assert curve[-1]["cumulative"] == pytest.approx(-8.3)
    assert min(row["drawdown"] for row in curve) == pytest.approx(-10.5)


def test_runtime_state_roundtrip(db):
    db.state_set("killswitch", {"active": True, "reason": "x"})
    assert db.state_get("killswitch")["active"] is True
    db.state_delete("killswitch")
    assert db.state_get("killswitch") is None


def test_settings_overrides_roundtrip(db):
    db.settings_set_many({"ENTRY_THRESHOLD": "0.92"})
    assert db.settings_all()["ENTRY_THRESHOLD"] == "0.92"


def test_risk_daily_loss_and_streak(db):
    settings = Settings(DAILY_LOSS_LIMIT_USD=20, MAX_CONSEC_LOSSES=0,
                        MIN_WALLET_BALANCE_USD=0)
    risk = RiskManager(db, settings, period="15m", mode="paper")
    now = time.time()
    _add(db, 10, status="lost", pnl=-12.0, entry_ts=now - 10)
    _add(db, 11, status="lost", pnl=-9.0, entry_ts=now - 5)
    status = risk.evaluate(now, stake=10.0, balance=1000.0)
    assert status.allowed is False
    assert status.reason == "daily_loss_limit"


def test_risk_consecutive_losses_pauses(db):
    settings = Settings(DAILY_LOSS_LIMIT_USD=0, MAX_CONSEC_LOSSES=2,
                        PAUSE_MINUTES_AFTER_STREAK=5, MIN_WALLET_BALANCE_USD=0)
    risk = RiskManager(db, settings, period="15m", mode="paper")
    now = time.time()
    _add(db, 20, status="won", pnl=1.0, entry_ts=now - 30)
    _add(db, 21, status="lost", pnl=-10.0, entry_ts=now - 20)
    _add(db, 22, status="lost", pnl=-10.0, entry_ts=now - 10)
    assert risk.consecutive_losses() == 2
    status = risk.evaluate(now, stake=10.0, balance=1000.0)
    assert status.allowed is False
    assert status.reason == "consecutive_losses"
    assert risk.pause_until > now
    risk.clear_pause()
    assert risk.evaluate(now, stake=10.0, balance=1000.0).allowed is True


def test_risk_balance_guard(db):
    settings = Settings(DAILY_LOSS_LIMIT_USD=0, MAX_CONSEC_LOSSES=0,
                        MIN_WALLET_BALANCE_USD=15, RISK_RESERVE_USD=2)
    risk = RiskManager(db, settings, period="15m", mode="paper")
    status = risk.evaluate(time.time(), stake=10.0, balance=12.0)
    assert status.allowed is False
    assert status.reason == "insufficient_balance"


def test_risk_killswitch_persists(db, tmp_path):
    settings = Settings()
    risk = RiskManager(db, settings, period="15m", mode="paper")
    risk.engage_killswitch("test")
    again = RiskManager(db, settings, period="15m", mode="paper")  # simulates a restart
    assert again.killswitch["active"] is True
    assert again.evaluate(time.time(), 10.0, 1000.0).reason == "killswitch"
    again.release_killswitch()
    assert again.evaluate(time.time(), 10.0, 1000.0).allowed is True


def test_two_strategies_can_share_a_slot(db):
    _add(db, 500, period="15m")
    _add(db, 500, period="5m")
    assert len(db.recent_trades(period="15m")) == 1
    assert len(db.recent_trades(period="5m")) == 1
    assert len(db.recent_trades()) == 2


def test_risk_is_isolated_per_period_and_mode(db):
    settings = Settings(DAILY_LOSS_LIMIT_USD=50, MAX_CONSEC_LOSSES=0,
                        MIN_WALLET_BALANCE_USD=0)
    now = time.time()
    _add(db, 600, status="lost", pnl=-60.0, period="5m", entry_ts=now - 10)

    five = RiskManager(db, settings, period="5m", mode="paper")
    fifteen = RiskManager(db, settings, period="15m", mode="paper")
    live = RiskManager(db, settings, period="5m", mode="live")

    assert five.evaluate(now, 10.0, 1000.0).reason == "daily_loss_limit"
    assert fifteen.evaluate(now, 10.0, 1000.0).allowed is True
    # Paper losses must not block the live strategy.
    assert live.evaluate(now, 10.0, 1000.0).allowed is True


def test_assets_are_isolated_in_the_same_slot(db):
    _add(db, 700, asset="BTC")
    _add(db, 700, asset="ETH")
    assert db.trade_exists(700, "paper", "15m", "BTC") is True
    assert db.trade_exists(700, "paper", "15m", "ETH") is True
    assert len(db.recent_trades(asset="BTC")) == 1
    assert len(db.recent_trades(asset="ETH")) == 1
    assert len(db.recent_trades()) == 2
    # Risk counters are per asset too.
    settings = Settings(DAILY_LOSS_LIMIT_USD=50, MAX_CONSEC_LOSSES=0,
                        MIN_WALLET_BALANCE_USD=0)
    now = time.time()
    _add(db, 701, status="lost", pnl=-60.0, asset="ETH", entry_ts=now - 5)
    eth_risk = RiskManager(db, settings, period="15m", mode="paper", asset="ETH")
    btc_risk = RiskManager(db, settings, period="15m", mode="paper", asset="BTC")
    assert eth_risk.evaluate(now, 10.0, 1000.0).reason == "daily_loss_limit"
    assert btc_risk.evaluate(now, 10.0, 1000.0).allowed is True
