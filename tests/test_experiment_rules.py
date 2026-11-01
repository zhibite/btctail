"""Entry-opportunity rule variants (retry / rearm / soft confirm)."""
from __future__ import annotations

import csv
import random
from pathlib import Path

import pytest

from src.market.models import Side
from src.research.dataset import clear_series_cache
from src.research.experiments_late_window import (
    RuleParams,
    run_variants,
    simulate_window,
)
from src.strategy.late_favorite import StrategyParams, evaluate_window_array

END = 1_770_000_900.0
START = END - 900


def build(values: dict[int, float], n: int = 180, base: float = 0.5):
    """Samples at 1s spacing; `values[offset]` is `offset` seconds before end."""
    times = [int(END) - n + i for i in range(n)]
    up = [values.get(int(END) - t, base) for t in times]
    down = [round(1.0 - u, 6) for u in up]
    return times, up, down


def test_baseline_matches_array_engine_on_random_series():
    rng = random.Random(11)
    for _ in range(30):
        n = 140
        up = []
        level = 0.5
        for _ in range(n):
            level = rng.uniform(0.88, 0.98) if rng.random() < 0.06 else max(
                0.01, min(0.99, level + rng.uniform(-0.03, 0.03))
            )
            level = round(level, 4)
            up.append(level)
        down = [round(1 - u, 4) for u in up]
        times = [int(END) - n + i for i in range(n)]
        p = RuleParams(confirm_sec=rng.choice([0, 5, 10, 15]),
                       max_entry_price=rng.choice([0.92, 0.94, 0.97]))
        expected = evaluate_window_array(
            times, up, down, END, StrategyParams(
                entry_window_sec=p.entry_window_sec, threshold=p.threshold,
                confirm_sec=p.confirm_sec, max_entry_price=p.max_entry_price,
            ),
        )
        entry, _ = simulate_window(times, up, down, END, p)
        if expected.ok:
            assert entry is not None
            assert entry.side is expected.side
            assert entry.price == pytest.approx(expected.entry_price)
        else:
            assert entry is None, expected.reason


def test_retry_enters_on_pullback_where_baseline_skips():
    times, up, down = build({50: 0.90, 40: 0.95, 39: 0.95, 38: 0.95,
                             37: 0.95, 36: 0.95, 35: 0.93})
    base_entry, base_reason = simulate_window(times, up, down, END, RuleParams())
    assert base_entry is None
    assert base_reason == "price_too_high"

    retry_entry, reason = simulate_window(
        times, up, down, END, RuleParams(retry_below_cap=True)
    )
    assert retry_entry is not None
    assert retry_entry.retry is True
    assert retry_entry.side is Side.UP
    assert retry_entry.price == pytest.approx(0.93)


def test_cap_applies_to_confirmed_price_not_trigger():
    # First crossing is above the cap, but the price pulls back by the
    # confirmation moment: this must still enter (it is the baseline rule).
    times, up, down = build({50: 0.95, 40: 0.92})
    entry, _ = simulate_window(times, up, down, END, RuleParams())
    assert entry is not None
    assert entry.price == pytest.approx(0.92)


def test_rearm_requires_a_fresh_crossing():
    values = {59: 0.91, 49: 0.85, 45: 0.50, 40: 0.92, 30: 0.93}
    times, up, down = build(values)
    entry, reason = simulate_window(times, up, down, END, RuleParams())
    assert entry is None
    assert reason == "no_confirmation"

    entry, _ = simulate_window(
        times, up, down, END, RuleParams(rearm_after_fail=True)
    )
    assert entry is not None
    assert entry.price == pytest.approx(0.93)


def test_soft_confirmation_floor():
    times, up, down = build({50: 0.91, 40: 0.89})
    entry, reason = simulate_window(times, up, down, END, RuleParams())
    assert entry is None
    assert reason == "no_confirmation"

    entry, _ = simulate_window(
        times, up, down, END, RuleParams(confirm_threshold=0.88)
    )
    assert entry is not None
    assert entry.price == pytest.approx(0.89)


def test_retry_gives_up_when_price_never_dips():
    times, up, down = build({50: 0.90, **{o: 0.99 for o in range(40, 0, -1)}})
    entry, reason = simulate_window(
        times, up, down, END, RuleParams(retry_below_cap=True)
    )
    assert entry is None
    assert reason == "retry_no_fill"


def test_no_trigger():
    times, up, down = build({})
    entry, reason = simulate_window(times, up, down, END, RuleParams())
    assert entry is None
    assert reason == "no_trigger"


# ---------------------------------------------------------------------------
# End-to-end runner on a synthetic dataset
# ---------------------------------------------------------------------------


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
            for offset in range(300):
                up = m["levels"].get(offset, 0.5)
                writer.writerow([m["slug"], (m["end"] - 300 + offset) * 1000,
                                 f"{up:.4f}", 0, f"{1 - up:.4f}", 0])


def test_runner_compares_variants(tmp_path):
    slot = (1_770_000_000 // 900) * 900
    root = tmp_path / "ds"
    _write_day(root, "2026-08-01", [{
        "slug": f"btc-updown-15m-{slot}", "start": slot, "end": slot + 900,
        "resolved": "UP",
        # Row offset o is (300 - o) seconds before the end.
        # 50s out: trigger 0.90; confirm at 40s: 0.95 (too high);
        # then a pullback to 0.93 at 35s.
        "levels": {250: 0.90, 260: 0.95, 261: 0.95, 262: 0.95, 263: 0.95,
                   264: 0.95, 265: 0.93},
    }])
    clear_series_cache()
    results = run_variants(
        root, "BTC", "15m", "2026-08-01", "2026-08-01",
        {
            "base": ("base", RuleParams()),
            "retry": ("retry", RuleParams(retry_below_cap=True)),
        },
    )
    assert results["base"].summary["trades"] == 0
    assert results["base"].skips.get("price_too_high") == 1
    assert results["retry"].summary["trades"] == 1
    assert results["retry"].summary["net_pnl"] > 0
    assert results["retry"].trades[0]["retry"] is True
