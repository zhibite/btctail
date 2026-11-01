"""Window arithmetic and time formatting.

Polymarket's recurring up/down markets are aligned to fixed slots:

    slot_start = floor(unix_seconds / period) * period

so the window boundaries need no network call — they are pure arithmetic.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ts() -> float:
    return time.time()


def slot_start(ts: float, period: int) -> int:
    return int(ts // period) * period


def slot_end(ts: float, period: int) -> int:
    return slot_start(ts, period) + period


def seconds_remaining(ts: float, period: int) -> float:
    return slot_end(ts, period) - ts


def next_slot(ts: float, period: int) -> int:
    return slot_start(ts, period) + period


def utc_iso(ts: float | int | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
