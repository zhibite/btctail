#!/usr/bin/env python
"""Sweep ENTRY_WINDOW_SEC on the post-TWAP regime.

Polymarket switched crypto Up/Down settlement to TWAP on 2026-08-07 08:00
UTC (5m: 30 s, 15m: 60 s). This grid re-runs the live params with entry
windows from 30 s to 300 s over the post-switch data, keeping threshold,
confirm and cap at their current values.

    python scripts/window_grid.py --root /opt/polyklines-history/dataset \
        --start 2026-08-08 --end 2026-09-30 \
        --params-json scripts/recommended_params.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.research.backtest import BacktestParams, metrics, run_backtest  # noqa: E402
from src.research.dataset import clear_series_cache  # noqa: E402

WINDOWS = [30, 60, 120, 180, 300]


def params_for(asset: str, period: str, base: dict, window: float,
               stake: float, slippage: float) -> BacktestParams:
    return BacktestParams(
        entry_window_sec=window,
        threshold=float(base.get("threshold", 0.90)),
        confirm_sec=float(base.get("confirm_sec", 0)),
        max_entry_price=float(base.get("max_entry_price", 0.94)),
        min_entry_price=float(base.get("min_entry_price", 0.0)),
        stake=stake,
        slippage=slippage,
        fee_rate=0.05 if asset == "BTC" else 0.01,
        fee_model="official",
        full_window=True,
    )


def row_for(trades: list[dict], dates: int) -> dict:
    m = metrics(trades)
    dd = abs(float(m["max_drawdown"]))
    remaining = [float(t["time_remaining_sec"]) for t in trades
                 if t.get("time_remaining_sec") is not None]
    remaining.sort()
    return {
        "trades": m["trades"],
        "trades_per_day": (m["trades"] / dates) if dates else 0.0,
        "win_rate": m["win_rate"],
        "net_pnl": m["net_pnl"],
        "max_drawdown": m["max_drawdown"],
        "ratio": (m["net_pnl"] / dd) if dd else None,
        "median_remaining": remaining[len(remaining) // 2] if remaining else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/polyklines-history/dataset")
    parser.add_argument("--start", default="2026-08-07")
    parser.add_argument("--end", default="2026-11-04")
    parser.add_argument("--clean-end", default="2026-09-30",
                        help="end of the fully-resolved sub-sample")
    parser.add_argument("--scopes", default="", help="ASSET:PERIOD,... (default: all)")
    parser.add_argument("--params-json", default="")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    tuned = json.loads(Path(args.params_json).read_text()) if args.params_json else {}
    scopes: list[tuple[str, str]] = []
    if args.scopes:
        for item in args.scopes.split(","):
            asset, _, period = item.strip().partition(":")
            if not asset or not period:
                raise SystemExit(f"bad scope {item!r}; use ASSET:PERIOD")
            scopes.append((asset.upper(), period))
    else:
        scopes = sorted(tuple(k.split(":", 1)) for k in tuned)
    if not scopes:
        raise SystemExit("provide --params-json or --scopes")

    out = {}
    aggregate: dict[float, list[dict]] = {}
    aggregate_clean: dict[float, list[dict]] = {}
    clean_days = (
        date.fromisoformat(args.clean_end) - date.fromisoformat(args.start)
    ).days + 1
    for asset, period in scopes:
        label = f"{asset}:{period}"
        base = tuned.get(label, {})
        current = float(base.get("entry_window_sec", 60))
        windows = sorted(set([current] + WINDOWS))
        print(f"=== {label} threshold={base.get('threshold')} "
              f"cap={base.get('max_entry_price')} current_window={current} ===",
              flush=True)
        rows = {}
        started = time.time()
        dates = 0
        for window in windows:
            params = params_for(asset, period, base, window, args.stake, args.slippage)
            res = run_backtest(args.root, asset, period, args.start, args.end, params)
            dates = int(res.summary["dates_processed"] or 0)
            row = row_for(res.trades, dates)
            clean_trades = [t for t in res.trades if t["date"] <= args.clean_end]
            clean_row = row_for(clean_trades, clean_days)
            row["clean"] = clean_row
            rows[str(window)] = row
            aggregate.setdefault(window, []).extend(res.trades)
            aggregate_clean.setdefault(window, []).extend(clean_trades)
            mark = "*" if abs(window - current) < 1e-9 else " "
            ratio = f"{row['ratio']:.1f}" if row["ratio"] is not None else "  -"
            med = (f"{row['median_remaining']:.0f}s"
                   if row["median_remaining"] is not None else "-")
            print(f"  {mark}{window:>5.0f}s trades={row['trades']:>5} "
                  f"({row['trades_per_day']:>5.1f}/d) win={row['win_rate']*100:5.1f}% "
                  f"net={row['net_pnl']:>+9.1f} dd={row['max_drawdown']:>+8.1f} "
                  f"ratio={ratio:>6} med_rem={med} | clean "
                  f"trades={clean_row['trades']:>5} net={clean_row['net_pnl']:>+9.1f} "
                  f"dd={clean_row['max_drawdown']:>+8.1f}", flush=True)
        best_net_key = max(rows, key=lambda k: rows[k]["clean"]["net_pnl"])
        ratio_keys = [k for k in rows if rows[k]["clean"]["ratio"] is not None]
        best_ratio_key = (
            max(ratio_keys, key=lambda k: rows[k]["clean"]["ratio"])
            if ratio_keys else None
        )
        ratio_text = (
            f"best_clean_ratio={best_ratio_key}s "
            f"ratio={rows[best_ratio_key]['clean']['ratio']:.1f}"
            if best_ratio_key else "best_clean_ratio=n/a"
        )
        print(f"  best_clean_net={best_net_key}s "
              f"net={rows[best_net_key]['clean']['net_pnl']:+.1f}  {ratio_text}",
              flush=True)
        print(f"  ({time.time() - started:.0f}s)", flush=True)
        out[label] = {
            "current_window": current,
            "threshold": base.get("threshold"),
            "max_entry_price": base.get("max_entry_price"),
            "rows": rows,
        }
        clear_series_cache()

    print("\n=== aggregate (all scopes) ===", flush=True)
    agg_rows = {}
    for window in sorted(aggregate):
        row = row_for(aggregate[window], dates)
        agg_rows[str(window)] = row
        ratio = f"{row['ratio']:.1f}" if row["ratio"] is not None else "  -"
        print(f"  {window:>5.0f}s trades={row['trades']:>6} "
              f"({row['trades_per_day']:>5.1f}/d) win={row['win_rate']*100:5.1f}% "
              f"net={row['net_pnl']:>+10.1f} dd={row['max_drawdown']:>+9.1f} "
              f"ratio={ratio:>6}", flush=True)

    print("\n=== aggregate clean (fully resolved sub-sample) ===", flush=True)
    agg_clean_rows = {}
    for window in sorted(aggregate_clean):
        row = row_for(aggregate_clean[window], clean_days)
        agg_clean_rows[str(window)] = row
        ratio = f"{row['ratio']:.1f}" if row["ratio"] is not None else "  -"
        print(f"  {window:>5.0f}s trades={row['trades']:>6} "
              f"({row['trades_per_day']:>5.1f}/d) win={row['win_rate']*100:5.1f}% "
              f"net={row['net_pnl']:>+10.1f} dd={row['max_drawdown']:>+9.1f} "
              f"ratio={ratio:>6}", flush=True)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"scopes": out, "aggregate": agg_rows,
             "aggregate_clean": agg_clean_rows,
             "date_range": [args.start, args.end],
             "clean_end": args.clean_end},
            indent=2, ensure_ascii=False,
        ))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
