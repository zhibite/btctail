"""Strategy rule + live/backtest parity."""
from __future__ import annotations

import random

import pytest

from src.market.models import Side
from src.strategy.late_favorite import (
    LateFavoriteStrategy,
    Phase,
    StrategyParams,
    evaluate_window_array,
    taker_fee,
    trade_pnl,
)

END = 1_000_000.0
START = END - 900


def make(params: StrategyParams | None = None) -> LateFavoriteStrategy:
    return LateFavoriteStrategy(int(START), END, params or StrategyParams())


def test_waits_until_entry_window():
    s = make()
    assert s.on_tick(END - 120, 0.95, 0.05) is None
    assert s.state.phase is Phase.WAITING
    decision = s.on_tick(END - 55, 0.95, 0.05)
    assert decision is None
    assert s.state.phase is Phase.CONFIRMING
    assert s.state.trigger_side is Side.UP


def test_confirm_after_ten_seconds_enters_at_confirmed_price():
    s = make()
    s.on_tick(END - 55, 0.91, 0.09)
    assert s.on_tick(END - 50, 0.92, 0.08) is None
    decision = s.on_tick(END - 45, 0.93, 0.07)
    assert decision is not None
    assert decision.side is Side.UP
    assert decision.price == pytest.approx(0.93)
    assert decision.trigger_price == pytest.approx(0.91)
    assert decision.confirm_delay_sec == pytest.approx(10)
    assert s.state.phase is Phase.ENTERED


def test_failed_confirmation_skips_window():
    s = make()
    s.on_tick(END - 55, 0.91, 0.09)
    decision = s.on_tick(END - 45, 0.85, 0.15)
    assert decision is None
    assert s.state.phase is Phase.SKIPPED
    assert s.state.skip_reason == "confirm_below_threshold"
    assert s.on_tick(END - 40, 0.95, 0.05) is None


def test_rearm_requires_going_below_threshold_first():
    s = make(StrategyParams(rearm_after_failed_confirm=True))
    s.on_tick(END - 55, 0.91, 0.09)
    s.on_tick(END - 45, 0.85, 0.15)
    assert s.state.phase is Phase.SCANNING
    assert s.state.blocked_until_below is True
    # still below threshold -> nothing happens
    assert s.on_tick(END - 44, 0.88, 0.12) is None
    # crosses again -> arms a new confirmation
    s.on_tick(END - 40, 0.90, 0.10)
    assert s.state.phase is Phase.CONFIRMING
    assert s.on_tick(END - 30, 0.91, 0.09) is not None


def test_price_cap_is_exclusive():
    s = make()
    s.on_tick(END - 55, 0.93, 0.07)
    decision = s.on_tick(END - 45, 0.94, 0.06)
    assert decision is None
    assert s.state.skip_reason == "price_too_high"


def test_no_time_to_confirm():
    s = make()
    # trigger with 8s left, confirm needs 10s
    assert s.on_tick(END - 8, 0.95, 0.05) is None
    assert s.state.skip_reason == "no_confirmation_time"


def test_zero_confirm_enters_immediately():
    s = make(StrategyParams(confirm_sec=0))
    decision = s.on_tick(END - 30, 0.92, 0.08)
    assert decision is not None
    assert decision.price == pytest.approx(0.92)


def test_down_side_triggers():
    s = make()
    s.on_tick(END - 55, 0.10, 0.90)
    decision = s.on_tick(END - 45, 0.07, 0.93)
    assert decision is not None
    assert decision.side is Side.DOWN


def test_window_expiry():
    s = make()
    assert s.on_tick(END + 1, 0.5, 0.5) is None
    assert s.state.phase is Phase.EXPIRED


def test_fee_formula():
    # 10 USDC at 0.92 -> 10.8696 shares; fee = C * 0.07 * p * (1-p)
    shares = 10 / 0.92
    fee = taker_fee(shares, 0.92, 0.07)
    assert fee == pytest.approx(shares * 0.07 * 0.92 * 0.08)
    assert trade_pnl(shares, 10.0, fee, won=True) == pytest.approx(shares - 10.0 - fee)
    assert trade_pnl(shares, 10.0, fee, won=False) == pytest.approx(-10.0 - fee)


# ---------------------------------------------------------------------------
# Live state machine vs array replay
# ---------------------------------------------------------------------------


def _random_series(rng: random.Random, n: int = 120) -> tuple[list[float], list[float], list[float]]:
    """Random walk that occasionally jumps into the tail (so entries happen)."""
    times = [END - 300 + i for i in range(n)]
    up = 0.5
    ups, downs = [], []
    for _ in times:
        if rng.random() < 0.06:
            up = rng.uniform(0.88, 0.985)
        else:
            up = max(0.01, min(0.99, up + rng.uniform(-0.03, 0.03)))
        up = round(up, 4)
        ups.append(up)
        downs.append(round(1.0 - up, 4))
    return times, ups, downs


@pytest.mark.parametrize("seed", range(20))
def test_array_and_state_machine_agree(seed: int):
    rng = random.Random(seed)
    times, ups, downs = _random_series(rng)
    params = StrategyParams(confirm_sec=rng.choice([0, 5, 10, 15]),
                            max_entry_price=rng.choice([0.92, 0.94, 0.97]))

    expected = evaluate_window_array(times, ups, downs, END, params)

    live = LateFavoriteStrategy(int(START), END, params)
    got = None
    for t, u, d in zip(times, ups, downs):
        decision = live.on_tick(t, u, d)
        if decision is not None:
            got = decision
            break

    if expected.ok:
        assert got is not None, f"live missed an entry the array found: {expected.reason}"
        assert got.side is expected.side
        assert got.price == pytest.approx(expected.entry_price)
    else:
        assert got is None, f"live entered while array skipped ({expected.reason})"
