"""Optimizer split derivation and termination (regression tests)."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.research.backtest import BacktestParams
from src.research.dataset import clear_series_cache
from src.research.optimize import _derive_ranges, optimize


def _write_day(root: Path, date: str, slot: int, up_final: float, resolved: str) -> None:
    day = root / "asset=btc" / "period=15m" / f"date={date}"
    day.mkdir(parents=True, exist_ok=True)
    slug = f"btc-updown-15m-{slot}"
    with (day / "markets.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["slug", "start_epoch", "end_epoch", "resolved"])
        writer.writerow([slug, slot, slot + 900, resolved])
    with (day / "klines_fine.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["slug", "time", "up_price", "up_volume", "down_price", "down_volume"])
        for second in range(300):
            up = 0.5 if second < 250 else up_final
            writer.writerow([slug, (slot + 600 + second) * 1000, f"{up:.4f}", 0,
                             f"{1 - up:.4f}", 0])


@pytest.fixture()
def small_dataset(tmp_path: Path) -> Path:
    root = tmp_path / "ds"
    base = (1_770_000_000 // 900) * 900
    _write_day(root, "2026-01-01", base, 0.92, "UP")
    _write_day(root, "2026-01-02", base + 900, 0.92, "UP")
    clear_series_cache()
    return root


def _dirs(root: Path, dates: list[str]) -> None:
    for date in dates:
        (root / "asset=btc" / "period=15m" / f"date={date}").mkdir(parents=True, exist_ok=True)


def test_default_split_is_chronological(tmp_path):
    root = tmp_path / "d"
    _dirs(root, [f"2026-01-{d:02d}" for d in range(1, 11)])
    train, validate = _derive_ranges(root, "btc", "15m", None, None, None, None, None)
    assert train == ("2026-01-01", "2026-01-07")
    assert validate == ("2026-01-08", "2026-01-10")


def test_train_end_alone_sets_validation_start(tmp_path):
    root = tmp_path / "d"
    _dirs(root, [f"2026-01-{d:02d}" for d in range(1, 11)])
    train, validate = _derive_ranges(root, "btc", "15m", "2026-01-05", None, None, None, None)
    assert train == ("2026-01-01", "2026-01-05")
    assert validate == ("2026-01-06", "2026-01-10")


def test_overlapping_ranges_are_rejected(tmp_path):
    root = tmp_path / "d"
    _dirs(root, [f"2026-01-{d:02d}" for d in range(1, 11)])
    with pytest.raises(ValueError):
        _derive_ranges(root, "btc", "15m", "2026-01-08", "2026-01-05", None, None, None)


def test_validate_end_alone_keeps_default_split(tmp_path):
    root = tmp_path / "d"
    _dirs(root, [f"2026-01-{d:02d}" for d in range(1, 11)])
    train, validate = _derive_ranges(root, "btc", "15m", None, None, "2026-01-09", None, None)
    assert train == ("2026-01-01", "2026-01-07")
    assert validate == ("2026-01-08", "2026-01-09")


def test_restricted_space_terminates(tmp_path, small_dataset):
    """A one-parameter space has ~19 values; asking for 60 must not hang."""
    result = optimize(
        small_dataset, "BTC", "15m", BacktestParams(),
        objective="profit_dd", iterations=60, min_trades=1,
        keys=["entry_window_sec"], train_end="2026-01-01",
    )
    assert result.best is not None
    assert len(result.leaderboard) <= 60
