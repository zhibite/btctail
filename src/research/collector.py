"""Collect live 1-second mids into the same on-disk layout as the research
dataset (`asset=.../period=.../date=.../klines_fine.csv` + `markets.csv`).

That is what lets the tuning agent learn from *our* executions instead of
only the historical export: the same backtest loader reads both.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Optional

from src.infra.clock import utc_day, utc_iso
from src.infra.logging import get_logger
from src.market.models import MarketInfo, Side

log = get_logger(__name__)

KLINE_COLUMNS = ["slug", "time", "up_price", "up_volume", "down_price", "down_volume"]
MARKET_COLUMNS = [
    "slug", "asset", "period", "start_epoch", "start_utc", "end_epoch", "end_utc",
    "status", "title", "condition_id", "up_token", "down_token", "resolved",
    "resolved_token", "volume", "liquidity", "series_slug",
]


class LiveCollector:
    def __init__(self, root: str | Path, asset: str, period: str) -> None:
        self.root = Path(root)
        self.asset = asset.lower()
        self.period = period
        # slot -> {second: (ts, up, down)}
        self._buffers: dict[int, dict[int, tuple[int, float, float]]] = {}

    def add_sample(self, slot: int, ts: float, up: Optional[float],
                   down: Optional[float]) -> None:
        if up is None and down is None:
            return
        second = int(ts)
        bucket = self._buffers.setdefault(slot, {})
        # Last observation inside the same second wins (the newest mid).
        bucket[second] = (
            second * 1000,
            float(up) if up is not None else float("nan"),
            float(down) if down is not None else float("nan"),
        )

    def sample_count(self, slot: int) -> int:
        return len(self._buffers.get(slot, {}))

    def flush_window(self, slot: int, market: Optional[MarketInfo],
                     resolved: Optional[Side] = None) -> int:
        """Append this window's samples and market row to the day partition.

        Safe to call twice for the same slot: samples are popped (written
        once) while the market row is rewritten when resolution arrives.
        """
        bucket = self._buffers.pop(slot, {})
        if not bucket and market is None:
            return 0
        day_dir = self.root / f"asset={self.asset}" / f"period={self.period}" / f"date={utc_day(slot)}"
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            slug = market.slug if market else f"{self.asset}-updown-{self.period}-{slot}"
            if bucket:
                with (day_dir / "klines_fine.csv").open("a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if f.tell() == 0:
                        writer.writerow(KLINE_COLUMNS)
                    for second in sorted(bucket):
                        ts_ms, up, down = bucket[second]
                        writer.writerow([slug, ts_ms,
                                         _fmt(up), 0,
                                         _fmt(down), 0])
            if market is not None:
                with (day_dir / "markets.csv").open("a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if f.tell() == 0:
                        writer.writerow(MARKET_COLUMNS)
                    writer.writerow([
                        market.slug, market.asset.lower(), market.period,
                        market.slot, utc_iso(market.slot),
                        market.end_ts, utc_iso(market.end_ts),
                        "closed" if resolved else "open", "",
                        market.condition_id, market.token_up, market.token_down,
                        resolved.value if resolved else "",
                        market.token_for(resolved) if resolved else "",
                        market.volume, 0, f"{self.asset}-up-or-down-{self.period}",
                    ])
        except Exception as exc:  # noqa: BLE001
            log.warning("collector.flush_failed", slot=slot, error=str(exc))
            return 0
        log.debug("collector.flushed", slot=slot, samples=len(bucket))
        return len(bucket)


def _fmt(value: float) -> str:
    if value != value:  # NaN
        return ""
    return f"{value:.4f}"
