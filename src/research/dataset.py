"""Dataset loading.

Accepts both the historical research export
(``predicttick/data/dataset``) and the live samples this bot writes:

    <root>/asset=btc/period=15m/date=2026-10-30/klines_fine.csv[.gz]
    <root>/asset=btc/period=15m/date=2026-10-30/klines.csv[.gz]
    <root>/asset=btc/period=15m/date=2026-10-30/markets.csv[.gz]

Fine (1-second) rows win over the 5-second main series on duplicate
timestamps, matching the original backtest loader.
"""
from __future__ import annotations

import glob
import os
import time
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import pandas as pd

from src.infra.logging import get_logger

log = get_logger(__name__)

KLINE_COLUMNS = ["slug", "time", "up_price", "down_price"]
MARKET_COLUMNS = ["slug", "start_epoch", "end_epoch", "resolved"]


@dataclass
class MarketSeries:
    date: str
    slug: str
    times: np.ndarray      # epoch seconds, ascending
    up: np.ndarray
    down: np.ndarray
    end_ts: Optional[float]
    resolved: str          # "UP" | "DOWN" | ""


def _find(day_dir: Path, stem: str) -> Optional[Path]:
    for name in (f"{stem}.csv.gz", f"{stem}.csv"):
        path = day_dir / name
        if path.exists():
            return path
    return None


def list_days(root: str | Path, asset: str, period: str,
              start: Optional[str] = None, end: Optional[str] = None) -> list[tuple[str, Path]]:
    """[(date, day_dir)] sorted ascending, filtered to [start, end]."""
    pattern = os.path.join(str(root), f"asset={asset.lower()}", f"period={period}", "date=*")
    days: list[tuple[str, Path]] = []
    for path in sorted(glob.glob(pattern)):
        day_dir = Path(path)
        if not day_dir.is_dir():
            continue
        date = day_dir.name.split("=", 1)[-1]
        if start and date < start:
            continue
        if end and date > end:
            continue
        days.append((date, day_dir))
    return days


def _read_csv(path: Path, usecols: list[str]) -> pd.DataFrame:
    try:
        # A hard kill during a collector append can leave one truncated line;
        # skip malformed rows instead of discarding the whole day.
        frame = pd.read_csv(path, usecols=lambda c: c in usecols, on_bad_lines="skip")
    except Exception as exc:  # noqa: BLE001
        log.warning("dataset.read_failed", path=str(path), error=str(exc))
        frame = pd.DataFrame()
    for column in usecols:
        if column not in frame.columns:
            frame[column] = np.nan
    return frame[usecols]


def load_day(day_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(klines, markets) for one day, with fine rows preferred."""
    frames = []
    fine = _find(day_dir, "klines_fine")
    main = _find(day_dir, "klines")
    if fine is not None:
        frame = _read_csv(fine, KLINE_COLUMNS)
        frame["src"] = 0
        frames.append(frame)
    if main is not None:
        frame = _read_csv(main, KLINE_COLUMNS)
        frame["src"] = 1
        frames.append(frame)
    if frames:
        klines = pd.concat(frames, ignore_index=True)
        klines = klines.dropna(subset=["slug", "time"])
        klines = klines.sort_values(["slug", "time", "src"], kind="mergesort")
        klines = klines.drop_duplicates(["slug", "time"], keep="first")
        klines = klines.reset_index(drop=True)
    else:
        klines = pd.DataFrame(columns=KLINE_COLUMNS + ["src"])

    markets_path = _find(day_dir, "markets")
    if markets_path is not None:
        markets = _read_csv(markets_path, MARKET_COLUMNS)
        markets = markets.dropna(subset=["slug"]).drop_duplicates("slug", keep="last")
    else:
        markets = pd.DataFrame(columns=MARKET_COLUMNS)
    return klines, markets


def _meta_maps(markets: pd.DataFrame) -> tuple[dict[str, float], dict[str, str]]:
    end_map: dict[str, float] = {}
    resolved_map: dict[str, str] = {}
    if markets.empty:
        return end_map, resolved_map
    for row in markets.itertuples(index=False):
        slug = str(getattr(row, "slug"))
        end_epoch = getattr(row, "end_epoch", None)
        if end_epoch is not None and not pd.isna(end_epoch):
            try:
                end_map[slug] = float(end_epoch)
            except (TypeError, ValueError):
                pass
        value = getattr(row, "resolved", "")
        if value is not None and not pd.isna(value):
            resolved_map[slug] = str(value).strip().upper()
    return end_map, resolved_map


def iter_markets(root: str | Path, asset: str, period: str,
                 start: Optional[str] = None, end: Optional[str] = None
                 ) -> Iterator[MarketSeries]:
    """Yield one MarketSeries per market, day by day."""
    for date, day_dir in list_days(root, asset, period, start, end):
        klines, markets = load_day(day_dir)
        if klines.empty:
            continue
        end_map, resolved_map = _meta_maps(markets)
        klines = klines.sort_values(["slug", "time"], kind="mergesort")
        times_all = klines["time"].to_numpy(dtype=np.int64)
        up_all = pd.to_numeric(klines["up_price"], errors="coerce").to_numpy(dtype=np.float64)
        down_all = pd.to_numeric(klines["down_price"], errors="coerce").to_numpy(dtype=np.float64)
        # Sorted by (slug, time) above, so each slug occupies one contiguous
        # slice; `groupby(...).indices` gives the positional index arrays.
        for slug, positions in klines.groupby("slug", sort=False).indices.items():
            positions = np.asarray(positions)
            if len(positions) == 0:
                continue
            slug_text = str(slug)
            end_ts = end_map.get(slug_text)
            if end_ts is None:
                try:
                    end_ts = float(slug_text.rsplit("-", 1)[-1]) + (
                        300 if period == "5m" else 900
                    )
                except (ValueError, IndexError):
                    end_ts = None
            yield MarketSeries(
                date=date,
                slug=slug_text,
                times=times_all[positions] / 1000.0,
                up=up_all[positions],
                down=down_all[positions],
                end_ts=end_ts,
                resolved=resolved_map.get(slug_text, ""),
            )


def horizon_seconds(entry_window_sec: float, confirm_sec: float) -> int:
    """Sampled span each market needs, bucketed so cache keys stay stable
    across an optimiser's candidate grid."""
    needed = entry_window_sec + confirm_sec + 30
    for bucket in (300, 600, 900, 1200):
        if needed <= bucket:
            return bucket
    return 1800


def _live_epoch(root: str, is_live: bool) -> int:
    # Live partitions grow while the bot runs; refresh their cache every
    # five minutes. Historical exports are immutable.
    return int(time.time() // 300) if is_live else 0


@lru_cache(maxsize=3)
def _load_series_cached(root: str, asset: str, period: str, start: Optional[str],
                        end: Optional[str], horizon: int, epoch: int) -> tuple:
    out: list[MarketSeries] = []
    for market in iter_markets(root, asset, period, start, end):
        if market.end_ts is None:
            out.append(market)
            continue
        cut = float(market.end_ts) - horizon
        idx = bisect_left(market.times, cut)
        if idx <= 0:
            out.append(market)
        else:
            out.append(MarketSeries(
                date=market.date, slug=market.slug,
                times=market.times[idx:], up=market.up[idx:], down=market.down[idx:],
                end_ts=market.end_ts, resolved=market.resolved,
            ))
    return tuple(out)


def load_series(root: str | Path, asset: str, period: str,
                start: Optional[str], end: Optional[str],
                horizon: int, *, is_live: bool = False) -> tuple[MarketSeries, ...]:
    """All MarketSeries for a range, sliced to the last `horizon` seconds.

    Cached because a parameter search re-reads the same files for every
    candidate; loading is ~40x the cost of evaluating a candidate.
    """
    return _load_series_cached(str(root), asset.lower(), period, start, end,
                               horizon, _live_epoch(str(root), is_live))


def clear_series_cache() -> None:
    _load_series_cached.cache_clear()


def day_slug_series(day_dir: Path) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """{slug: (times_seconds, up, down)} for one day (used by tests/tools)."""
    klines, _ = load_day(day_dir)
    if klines.empty:
        return {}
    klines = klines.sort_values(["slug", "time"], kind="mergesort")
    times = klines["time"].to_numpy(dtype=np.int64) / 1000.0
    up = pd.to_numeric(klines["up_price"], errors="coerce").to_numpy(dtype=np.float64)
    down = pd.to_numeric(klines["down_price"], errors="coerce").to_numpy(dtype=np.float64)
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for slug, positions in klines.groupby("slug", sort=False).indices.items():
        positions = np.asarray(positions)
        out[str(slug)] = (times[positions], up[positions], down[positions])
    return out


@lru_cache(maxsize=8)
def _summary_cached(root: str, asset: str, period: str, epoch: int) -> tuple:
    days = list_days(root, asset, period)
    dates = [d for d, _ in days]
    markets = 0
    sampled = 0
    for date, day_dir in days[:: max(1, len(days) // 20)] if days else []:
        _, mk = load_day(day_dir)
        markets += len(mk)
        sampled += 1
    scale = (len(days) / sampled) if sampled else 0
    return (
        dates[0] if dates else None,
        dates[-1] if dates else None,
        len(days),
        int(markets * scale),
    )


def dataset_summary(root: str | Path, asset: str, period: str,
                    *, is_live: bool = False) -> dict:
    root_path = Path(root)
    if not root_path.exists():
        return {"root": str(root_path), "exists": False, "days": 0,
                "date_min": None, "date_max": None, "markets_estimate": 0}
    # Live partitions grow while the bot runs; refresh their summary every
    # five minutes instead of caching it for the life of the process.
    epoch = int(time.time() // 300) if is_live else 0
    date_min, date_max, days, markets = _summary_cached(
        str(root_path), asset.lower(), period, epoch
    )
    return {
        "root": str(root_path),
        "exists": days > 0,
        "days": days,
        "date_min": date_min,
        "date_max": date_max,
        "markets_estimate": markets,
    }


def live_dataset_summary(collect_dir: str | Path, asset: str, period: str) -> dict:
    info = dataset_summary(collect_dir, asset, period, is_live=True)
    info["kind"] = "live"
    return info
