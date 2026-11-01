"""End-to-end backtest against a synthetic on-disk dataset."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.research.backtest import BacktestParams, run_backtest, score
from src.research.dataset import clear_series_cache, load_series

SLOT = 1_770_000_000
END = SLOT + 900


def _write_day(root: Path, date: str, markets: list[dict]) -> None:
    day = root / "asset=btc" / "period=15m" / f"date={date}"
    day.mkdir(parents=True, exist_ok=True)
    with (day / "markets.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["slug", "start_epoch", "end_epoch", "resolved"])
        for m in markets:
            writer.writerow([m["slug"], m["start"], m["end"], m["resolved"]])
    with (day / "klines_fine.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["slug", "time", "up_price", "up_volume", "down_price", "down_volume"])
        for m in markets:
            for offset, up in m["series"]:
                writer.writerow([m["slug"], (m["end"] - 300 + offset) * 1000,
                                 f"{up:.4f}", 0, f"{1 - up:.4f}", 0])


def _series(trigger_at: int, price: float, hold: float | None = None) -> list[tuple[int, float]]:
    """Flat 0.50, then `price` from second `trigger_at` (0..299)."""
    hold = price if hold is None else hold
    out = []
    for second in range(300):
        if second < trigger_at:
            out.append((second, 0.50))
        else:
            out.append((second, hold))
    return out


@pytest.fixture()
def dataset(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    slot_a, slot_b = SLOT, SLOT + 900
    _write_day(root, "2026-01-01", [
        {
            "slug": f"btc-updown-15m-{slot_a}", "start": slot_a, "end": slot_a + 900,
            "resolved": "UP", "series": _series(250, 0.92),
        },
        {
            "slug": f"btc-updown-15m-{slot_b}", "start": slot_b, "end": slot_b + 900,
            "resolved": "DOWN", "series": _series(250, 0.05),
        },
    ])
    clear_series_cache()
    return root


def test_backtest_enters_holds_and_charges_offical_fee(dataset: Path):
    result = run_backtest(dataset, "BTC", "15m", None, None,
                          BacktestParams(slippage=0.005, fee_rate=0.07))
    assert result.summary["markets_scanned"] == 2
    assert result.summary["trades"] == 1
    trade = result.trades[0]
    assert trade["side"] == "UP"
    assert trade["entry_price"] == pytest.approx(0.92)
    assert trade["entry_fill"] == pytest.approx(0.925)
    shares = 10 / 0.925
    fee = shares * 0.07 * 0.925 * 0.075
    assert trade["shares"] == pytest.approx(shares)
    assert trade["fees"] == pytest.approx(fee)
    assert trade["pnl"] == pytest.approx(shares - 10 - fee)
    assert result.summary["skip_counts"]["price_too_high"] == 1


def test_price_cap_rejects_095(dataset: Path):
    result = run_backtest(dataset, "BTC", "15m", None, None,
                          BacktestParams(max_entry_price=0.96))
    assert result.summary["trades"] == 2


def test_loader_reads_series(dataset: Path):
    clear_series_cache()
    series = load_series(dataset, "btc", "15m", None, None, 300)
    assert len(series) == 2
    assert all(len(item.times) == 300 for item in series)


def test_score_rejects_thin_samples():
    assert score({"trades": 5, "net_pnl": 100}, min_trades=20) == float("-inf")
    assert score({"trades": 30, "net_pnl": 10, "max_drawdown": 4}, "profit_dd", 20) == 8.0


def test_empty_dataset_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        run_backtest(tmp_path, "BTC", "15m", None, None, BacktestParams())
