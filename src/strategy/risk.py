"""Risk guards around the entry decision.

Each strategy (15m / 5m) has its own RiskManager: its daily-loss and streak
counters only look at that strategy's trades in the mode it actually trades
(paper vs live). Sharing one counter across strategies would let a bad 5m
session block the 15m strategy, and mixing paper with live would corrupt
both.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.config import Settings, strategy_key
from src.infra.clock import utc_day
from src.infra.logging import get_logger
from src.persistence.db import DB

log = get_logger(__name__)

KILLSWITCH_KEY = "killswitch"
PAUSE_KEY = "pause_until"
# Number of settled trades at the moment the streak guard last fired. Without
# it a manual "resume" would be undone by the very next tick (the losing
# streak is still >= the limit), and an expired pause could never reopen
# trading until a trade won -- which cannot happen while entries are blocked.
STREAK_MARK_KEY = "risk_streak_mark"


@dataclass
class RiskStatus:
    allowed: bool
    reason: str = ""
    detail: str = ""
    pause_until: float = 0.0
    daily_pnl: float = 0.0
    daily_limit: float = 0.0
    consecutive_losses: int = 0
    balance: Optional[float] = None


class RiskManager:
    def __init__(self, db: DB, settings: Settings, *, period: str = "15m",
                 mode: str = "paper", asset: str = "BTC",
                 scope: str | None = None) -> None:
        self._db = db
        self._settings = settings
        self._period = period
        self._asset = (asset or "BTC").upper()
        self._mode = mode
        self._scope = scope or strategy_key(self._asset, period)
        self._pause_until = float(
            db.state_get(self._key(PAUSE_KEY), 0.0) or 0.0
        )

    def _key(self, name: str) -> str:
        return f"{name}:{self._scope}"

    @property
    def period(self) -> str:
        return self._period

    @property
    def asset(self) -> str:
        return self._asset

    @property
    def mode(self) -> str:
        return self._mode

    # ── killswitch ────────────────────────────────────────────────────────

    @property
    def killswitch(self) -> dict:
        value = self._db.state_get(self._key(KILLSWITCH_KEY), None)
        if isinstance(value, dict):
            return value
        return {"active": False, "reason": "", "since": None}

    def engage_killswitch(self, reason: str) -> dict:
        value = {"active": True, "reason": reason, "since": time.time()}
        self._db.state_set(self._key(KILLSWITCH_KEY), value)
        log.warning("risk.killswitch_engaged", period=self._period, reason=reason)
        return value

    def release_killswitch(self) -> dict:
        value = {"active": False, "reason": "", "since": None}
        self._db.state_set(self._key(KILLSWITCH_KEY), value)
        log.info("risk.killswitch_released", period=self._period)
        return value

    # ── pause ─────────────────────────────────────────────────────────────

    @property
    def pause_until(self) -> float:
        return self._pause_until

    def set_pause(self, seconds: float, reason: str) -> None:
        until = time.time() + max(0.0, seconds)
        self._pause_until = until
        self._db.state_set(self._key(PAUSE_KEY), until)
        log.warning("risk.paused", period=self._period, until=until, reason=reason,
                    minutes=round(seconds / 60.0, 1))

    def clear_pause(self) -> None:
        self._pause_until = 0.0
        self._db.state_set(self._key(PAUSE_KEY), 0.0)
        # Manual resume: do not let the existing losing streak re-arm on the
        # next tick; only a NEW loss may re-trigger the guard.
        self._db.state_set(self._key(STREAK_MARK_KEY), self._settled_count())
        log.info("risk.pause_cleared", period=self._period)

    # ── metrics ───────────────────────────────────────────────────────────

    def daily_pnl(self, now: float | None = None) -> float:
        now = now or time.time()
        day = utc_day(now)
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        trades = self._db.trades_between(start, start + 86400,
                                         mode=self._mode, period=self._period,
                                         asset=self._asset)
        return sum(float(t.get("pnl") or 0) for t in trades
                   if t.get("status") in ("won", "lost", "void"))

    def consecutive_losses(self) -> int:
        trades = self._db.all_settled_trades(mode=self._mode, period=self._period,
                                             asset=self._asset)
        streak = 0
        for trade in reversed(trades):
            if trade.get("status") == "won":
                break
            if trade.get("status") == "lost":
                streak += 1
        return streak

    def _settled_count(self) -> int:
        return len(self._db.all_settled_trades(mode=self._mode, period=self._period,
                                               asset=self._asset))

    # ── decision ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        now: float,
        stake: float,
        balance: Optional[float] = None,
        mode: str | None = None,
    ) -> RiskStatus:
        # `mode` is accepted for call-site clarity; the counters always use
        # the mode this manager was built for so paper and live never mix.
        _ = mode
        settings = self._settings
        kill = self.killswitch
        if kill.get("active"):
            return RiskStatus(False, "killswitch", str(kill.get("reason") or "手动停止"))

        if self._pause_until and now < self._pause_until:
            return RiskStatus(
                False, "paused",
                f"暂停至 {time.strftime('%H:%M:%S', time.localtime(self._pause_until))}",
                pause_until=self._pause_until,
            )

        daily = self.daily_pnl(now)
        limit = float(settings.DAILY_LOSS_LIMIT_USD or 0)
        if limit > 0 and daily <= -limit:
            return RiskStatus(False, "daily_loss_limit",
                              f"当日已实现 {daily:.2f} USDC，达到上限 {limit:.0f}",
                              daily_pnl=daily, daily_limit=limit)

        streak = self.consecutive_losses()
        max_streak = int(settings.MAX_CONSEC_LOSSES or 0)
        settled = self._settled_count()
        mark = int(self._db.state_get(self._key(STREAK_MARK_KEY), 0) or 0)
        if max_streak > 0 and streak >= max_streak and settled > mark:
            self.set_pause(settings.PAUSE_MINUTES_AFTER_STREAK * 60,
                           f"连亏 {streak} 次")
            self._db.state_set(self._key(STREAK_MARK_KEY), settled)
            return RiskStatus(False, "consecutive_losses",
                              f"连亏 {streak} 次，暂停 {settings.PAUSE_MINUTES_AFTER_STREAK} 分钟",
                              pause_until=self._pause_until,
                              consecutive_losses=streak)

        min_balance = float(settings.MIN_WALLET_BALANCE_USD or 0)
        if balance is not None and min_balance > 0:
            required = min_balance + float(settings.RISK_RESERVE_USD)
            if balance < required or balance < stake:
                return RiskStatus(False, "insufficient_balance",
                                  f"余额 {balance:.2f} USDC < 需要 {max(required, stake):.2f}",
                                  balance=balance)

        return RiskStatus(True, balance=balance, daily_pnl=daily,
                          daily_limit=limit, consecutive_losses=streak)

    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        kill = self.killswitch
        return {
            "period": self._period,
            "asset": self._asset,
            "strategy": self._scope,
            "mode": self._mode,
            "killswitch": kill,
            "pause_until": self._pause_until,
            "paused": bool(self._pause_until and now < self._pause_until),
            "daily_pnl": self.daily_pnl(now),
            "daily_limit": float(self._settings.DAILY_LOSS_LIMIT_USD or 0),
            "consecutive_losses": self.consecutive_losses(),
            "max_consec_losses": int(self._settings.MAX_CONSEC_LOSSES or 0),
        }
