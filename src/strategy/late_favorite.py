"""BTC 15m late-window favorite — the whole strategy, as pure logic.

Rule (docs/late_window_strategy_summary.md, "最强、最稳的组合"):

    In the last 60 seconds of a 15-minute Up/Down market, the first time
    either side trades at a midpoint >= 0.90, wait 10 seconds. If that side
    is still >= 0.90, buy it at the then-current price -- but only while
    that price is strictly below 0.94. Hold the shares to resolution.

    Stake 10 USDC per trade.

The module contains no I/O: `LateFavoriteStrategy` is fed one price tick at
a time (live), and `evaluate_window_array` replays the identical rules over a
whole sampled window (research/backtest). A parity test keeps the two in
lock-step.

Economics
---------
Buying `shares = stake / p` and holding to resolution:

    win  -> +shares * 1.0      (minus the stake already spent)
    lose -> 0

so the break-even win rate is exactly `p` before fees. The official taker
fee is

    fee = shares * fee_rate * p * (1 - p)

charged on entry; the resolution payout at p = 1 / 0 has no fee.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from src.market.models import Side


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyParams:
    entry_window_sec: float = 60.0
    threshold: float = 0.90
    confirm_sec: float = 10.0
    max_entry_price: float = 0.94
    min_entry_price: float = 0.0
    rearm_after_failed_confirm: bool = False
    max_entries_per_window: int = 1

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if not 0.0 < self.threshold < 1.0:
            errors.append("threshold 必须在 (0,1)")
        if not 0.0 < self.max_entry_price <= 1.0:
            errors.append("max_entry_price 必须在 (0,1]")
        if self.min_entry_price and self.min_entry_price >= self.max_entry_price:
            errors.append("min_entry_price 必须 < max_entry_price")
        if self.entry_window_sec <= 0:
            errors.append("entry_window_sec 必须 > 0")
        if self.confirm_sec < 0:
            errors.append("confirm_sec 不能为负")
        return errors


class Phase(str, Enum):
    WAITING = "WAITING"        # before the entry window opens
    SCANNING = "SCANNING"      # inside the window, waiting for a trigger
    CONFIRMING = "CONFIRMING"  # trigger seen, waiting out confirm_sec
    ENTERED = "ENTERED"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"        # window ended without an entry


@dataclass
class EntryDecision:
    side: Side
    price: float                # confirmed signal price (midpoint)
    trigger_price: float        # price at the first crossing
    trigger_ts: float
    confirm_ts: float
    first_cross_ts: float

    @property
    def confirm_delay_sec(self) -> float:
        return self.confirm_ts - self.first_cross_ts

    @property
    def confirm_price_delta(self) -> float:
        return self.price - self.trigger_price


@dataclass
class StrategyState:
    phase: Phase = Phase.WAITING
    slot: int = 0
    end_ts: float = 0.0
    trigger_side: Optional[Side] = None
    trigger_price: float = 0.0
    trigger_ts: float = 0.0
    confirm_due_ts: float = 0.0
    skip_reason: str = ""
    decision: Optional[EntryDecision] = None
    entries: int = 0
    blocked_until_below: bool = False
    last_tick_ts: float = 0.0
    last_up: Optional[float] = None
    last_down: Optional[float] = None
    # Telemetry: highest price seen on each side inside the entry window.
    peak_up: float = 0.0
    peak_down: float = 0.0

    @property
    def done(self) -> bool:
        return self.phase in (Phase.ENTERED, Phase.SKIPPED, Phase.EXPIRED)


class LateFavoriteStrategy:
    """One instance per window. Feed it ticks with `on_tick`."""

    def __init__(self, slot: int, end_ts: float, params: StrategyParams) -> None:
        self.slot = int(slot)
        self.end_ts = float(end_ts)
        self.params = params
        self.state = StrategyState(
            slot=self.slot,
            end_ts=self.end_ts,
            phase=Phase.WAITING,
        )

    # ── helpers ───────────────────────────────────────────────────────────

    @property
    def entry_start_ts(self) -> float:
        return self.end_ts - self.params.entry_window_sec

    def seconds_remaining(self, now: float) -> float:
        return self.end_ts - now

    def _skip(self, reason: str, now: float) -> None:
        self.state.phase = Phase.SKIPPED
        self.state.skip_reason = reason
        self.state.last_tick_ts = now

    def _pick_side(self, up: Optional[float], down: Optional[float]) -> tuple[Optional[Side], float]:
        threshold = self.params.threshold
        best_side: Optional[Side] = None
        best_price = -1.0
        if up is not None and up >= threshold and up > best_price:
            best_side, best_price = Side.UP, up
        if down is not None and down >= threshold and down > best_price:
            best_side, best_price = Side.DOWN, down
        return best_side, best_price

    # ── main entry point ──────────────────────────────────────────────────

    def on_tick(
        self, now: float, up: Optional[float], down: Optional[float]
    ) -> Optional[EntryDecision]:
        """Advance the state machine by one observation.

        Returns an EntryDecision exactly once, on the tick where the trade
        should be executed. Every other tick returns None.
        """
        s = self.state
        s.last_tick_ts = now
        if up is not None:
            s.last_up = up
        if down is not None:
            s.last_down = down

        if s.done:
            return None

        if now >= self.end_ts:
            if s.phase in (Phase.WAITING, Phase.SCANNING, Phase.CONFIRMING):
                self._skip("window_expired", now)
                s.phase = Phase.EXPIRED
            return None

        if now < self.entry_start_ts:
            s.phase = Phase.WAITING
            return None

        if s.phase is Phase.WAITING:
            s.phase = Phase.SCANNING

        if up is not None:
            s.peak_up = max(s.peak_up, up)
        if down is not None:
            s.peak_down = max(s.peak_down, down)

        # ── confirmation window ───────────────────────────────────────────
        if s.phase is Phase.CONFIRMING:
            if now < s.confirm_due_ts:
                return None
            side = s.trigger_side
            assert side is not None
            price = up if side is Side.UP else down
            if price is None:
                self._skip("confirm_price_missing", now)
                return None

            if price < self.params.threshold:
                if self.params.rearm_after_failed_confirm:
                    s.phase = Phase.SCANNING
                    s.blocked_until_below = True
                    s.skip_reason = "confirm_below_threshold_rearmed"
                    s.trigger_ts = 0.0
                    s.confirm_due_ts = 0.0
                    s.trigger_side = None
                    return None
                self._skip("confirm_below_threshold", now)
                return None

            if price >= self.params.max_entry_price:
                self._skip("price_too_high", now)
                return None
            if self.params.min_entry_price > 0 and price <= self.params.min_entry_price:
                self._skip("price_too_low", now)
                return None

            decision = EntryDecision(
                side=side,
                price=float(price),
                trigger_price=float(s.trigger_price),
                trigger_ts=s.trigger_ts,
                confirm_ts=now,
                first_cross_ts=s.trigger_ts,
            )
            s.decision = decision
            s.entries += 1
            s.phase = Phase.ENTERED
            return decision

        # ── scanning ──────────────────────────────────────────────────────
        if s.blocked_until_below:
            if (up is None or up < self.params.threshold) and (
                down is None or down < self.params.threshold
            ):
                s.blocked_until_below = False
            else:
                return None

        side, price = self._pick_side(up, down)
        if side is None:
            return None

        s.trigger_side = side
        s.trigger_price = price
        s.trigger_ts = now

        if self.params.confirm_sec <= 0:
            if price >= self.params.max_entry_price:
                self._skip("price_too_high", now)
                return None
            if self.params.min_entry_price > 0 and price <= self.params.min_entry_price:
                self._skip("price_too_low", now)
                return None
            decision = EntryDecision(
                side=side, price=price, trigger_price=price,
                trigger_ts=now, confirm_ts=now, first_cross_ts=now,
            )
            s.decision = decision
            s.entries += 1
            s.phase = Phase.ENTERED
            return decision

        s.confirm_due_ts = now + self.params.confirm_sec
        if s.confirm_due_ts > self.end_ts:
            # There is no time left to confirm before the market closes; the
            # reference backtest treats this as no_confirmation.
            self._skip("no_confirmation_time", now)
            return None
        s.phase = Phase.CONFIRMING
        return None

    # ── view for the dashboard ────────────────────────────────────────────

    def snapshot(self, now: float) -> dict:
        s = self.state
        return {
            "slot": self.slot,
            "end_ts": self.end_ts,
            # Params frozen when the window opened (runtime edits apply to
            # the NEXT window, so the global settings may already differ).
            "params": {
                "entry_window_sec": self.params.entry_window_sec,
                "threshold": self.params.threshold,
                "confirm_sec": self.params.confirm_sec,
                "max_entry_price": self.params.max_entry_price,
                "min_entry_price": self.params.min_entry_price,
            },
            "entry_start_ts": self.entry_start_ts,
            "remaining_s": max(0.0, self.end_ts - now),
            "in_entry_window": self.entry_start_ts <= now < self.end_ts,
            "phase": s.phase.value,
            "trigger_side": s.trigger_side.value if s.trigger_side else None,
            "trigger_price": s.trigger_price or None,
            "trigger_ts": s.trigger_ts or None,
            "confirm_due_ts": s.confirm_due_ts or None,
            "confirm_in_s": max(0.0, s.confirm_due_ts - now) if s.phase is Phase.CONFIRMING else None,
            "skip_reason": s.skip_reason,
            "peak_up": s.peak_up or None,
            "peak_down": s.peak_down or None,
            "decision": (
                {
                    "side": s.decision.side.value,
                    "price": s.decision.price,
                    "trigger_price": s.decision.trigger_price,
                    "confirm_delay_sec": s.decision.confirm_delay_sec,
                }
                if s.decision
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Fees and settlement
# ---------------------------------------------------------------------------


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    """Official Polymarket formula: C * feeRate * p * (1 - p)."""
    if shares <= 0 or fee_rate <= 0:
        return 0.0
    return shares * fee_rate * price * (1.0 - price)


def trade_pnl(shares: float, cost: float, fee: float, won: bool) -> float:
    """PnL for a hold-to-resolution position.

    `cost` is the USDC actually spent on the buy (shares * avg_price);
    the fee is charged at entry. Winning shares redeem at 1.0.
    """
    payout = shares if won else 0.0
    return payout - cost - fee


def roi(pnl: float, stake: float) -> float:
    return pnl / stake if stake > 0 else 0.0


# ---------------------------------------------------------------------------
# Vectorised replay (research / backtest)
# ---------------------------------------------------------------------------


@dataclass
class ArraySignal:
    """What `evaluate_window_array` found for one market."""

    reason: str = ""
    side: Optional[Side] = None
    entry_price: float = 0.0
    entry_ts: float = 0.0
    first_cross_price: float = 0.0
    first_cross_ts: float = 0.0
    confirm_delay_sec: float = 0.0
    confirm_price_delta: float = 0.0
    time_remaining_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.side is not None


def evaluate_window_array(
    times: Sequence[float],
    up: Sequence[float],
    down: Sequence[float],
    end_ts: float,
    params: StrategyParams,
) -> ArraySignal:
    """Replay the rule over one window's samples.

    `times` must be sorted ascending (epoch seconds). Samples outside the
    entry window are ignored; a NaN/None price means "no observation".
    Equivalent to iterating `LateFavoriteStrategy.on_tick` over the samples,
    but without constructing objects -- the parity test in
    tests/test_strategy_parity.py checks that claim on random series.
    """
    n = len(times)
    if n == 0:
        return ArraySignal(reason="no_data")

    start = end_ts - params.entry_window_sec
    # Skip straight to the entry window: a day's series is ~900 samples per
    # market but only the last `entry_window_sec` can contain an entry.
    first = bisect_left(times, start)

    def price_at(idx: int, side: Side) -> Optional[float]:
        value = up[idx] if side is Side.UP else down[idx]
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None  # NaN check

    # Index of the first sample inside [start, end] that crosses.
    first_idx = -1
    side: Optional[Side] = None
    best = -1.0
    for idx in range(first, n):
        t = times[idx]
        if t >= end_ts:
            break
        u = price_at(idx, Side.UP)
        d = price_at(idx, Side.DOWN)
        chosen = None
        chosen_price = -1.0
        if u is not None and u >= params.threshold and u > chosen_price:
            chosen, chosen_price = Side.UP, u
        if d is not None and d >= params.threshold and d > chosen_price:
            chosen, chosen_price = Side.DOWN, d
        if chosen is not None:
            first_idx = idx
            side = chosen
            best = chosen_price
            break

    if first_idx < 0 or side is None:
        return ArraySignal(reason="no_trigger")

    first_cross_ts = float(times[first_idx])

    if params.confirm_sec > 0:
        confirm_target = first_cross_ts + params.confirm_sec
        if confirm_target > end_ts:
            return ArraySignal(reason="no_confirmation_time")
        # Last sample at or before the confirmation time, inside the window.
        j = first_idx
        for idx in range(first_idx + 1, n):
            if float(times[idx]) > confirm_target:
                break
            if float(times[idx]) >= end_ts:
                break
            j = idx
        if j <= first_idx:
            return ArraySignal(reason="no_confirmation")
        price = price_at(j, side)
        if price is None or price < params.threshold:
            return ArraySignal(reason="no_confirmation")
        entry_idx = j
        entry_price = price
        entry_ts = float(times[j])
    else:
        entry_idx = first_idx
        entry_price = best
        entry_ts = first_cross_ts

    if entry_price >= params.max_entry_price:
        return ArraySignal(reason="price_too_high")
    if params.min_entry_price > 0 and entry_price <= params.min_entry_price:
        return ArraySignal(reason="price_too_low")

    return ArraySignal(
        side=side,
        entry_price=float(entry_price),
        entry_ts=entry_ts,
        first_cross_price=float(best),
        first_cross_ts=first_cross_ts,
        confirm_delay_sec=entry_ts - first_cross_ts,
        confirm_price_delta=float(entry_price - best),
        time_remaining_sec=end_ts - entry_ts,
        reason="entry",
    )
