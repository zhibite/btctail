#!/usr/bin/env python
"""Sweep MAX_ENTRY_PRICE on the PolyKlines history with the live params.

A lower cap only turns a signal into a skip; it does not change when the
signal fires. So one backtest with cap=1.0 records every entry, and a cap
sweep is just a filter on ``entry_price < cap``. That makes the grid cheap
and exact.

Outputs a per-scope table with net PnL, drawdown, return/drawdown, monthly
consistency and a May-Jul train / Aug-Nov test split.

    python scripts/cap_grid.py --root /opt/polyklines-history/dataset \
        --start 2026-05-01 --end 2026-11-04 \
        --params-json scripts/recommended_params.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.research.backtest import BacktestParams, metrics, run_backtest  # noqa: E402
from src.research.dataset import clear_series_cache  # noqa: E402

CAPS = [0.86, 0.87, 0.88, 0.89, 0.90, 0.91, 0.92, 0.93, 0.94,
        0.95, 0.96, 0.97, 0.98, 0.99]
TRAIN_END = "2026-07"


def params_for(asset: str, period: str, base: dict, stake: float,
               slippage: float) -> BacktestParams:
    return BacktestParams(
        entry_window_sec=float(base.get("entry_window_sec", 60)),
        threshold=float(base.get("threshold", 0.90)),
        confirm_sec=float(base.get("confirm_sec", 0)),
        max_entry_price=1.0,  # record every signal; caps filter afterwards
        min_entry_price=float(base.get("min_entry_price", 0.0)),
        stake=stake,
        slippage=slippage,
        fee_rate=0.05 if asset == "BTC" else 0.01,
        fee_model="official",
    )


def row_for(trades: list[dict], cap: float) -> dict:
    kept = [t for t in trades if float(t["entry_price"]) < cap]
    dropped = [t for t in trades if float(t["entry_price"]) >= cap]
    m = metrics(kept)
    train = metrics([t for t in kept if t["month"] <= TRAIN_END])
    test = metrics([t for t in kept if t["month"] > TRAIN_END])
    months = {}
    for trade in kept:
        months[trade["month"]] = months.get(trade["month"], 0.0) + float(trade["pnl"])
    dd = abs(float(m["max_drawdown"]))
    return {
        "cap": cap,
        "trades": m["trades"],
        "win_rate": m["win_rate"],
        "net_pnl": m["net_pnl"],
        "max_drawdown": m["max_drawdown"],
        "ratio": (m["net_pnl"] / dd) if dd else None,
        "dropped": len(dropped),
        "dropped_pnl": sum(float(t["pnl"]) for t in dropped),
        "months_positive": sum(1 for v in months.values() if v > 0),
        "months_total": len(months),
        "train_net": train["net_pnl"],
        "test_net": test["net_pnl"],
        "test_ratio": (test["net_pnl"] / abs(float(test["max_drawdown"])))
                      if test["max_drawdown"] else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/polyklines-history/dataset")
    parser.add_argument("--start", default="2026-05-01")
    parser.add_argument("--end", default="2026-11-04")
    parser.add_argument("--scopes", default="", help="ASSET:PERIOD,... (default: all)")
    parser.add_argument("--params-json", default="")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    tuned = json.loads(Path(args.params_json).read_text()) if args.params_json else {}
    scopes = []
    if args.scopes:
        for item in args.scopes.split(","):
            asset, _, period = item.strip().partition(":")
            scopes.append((asset.upper(), period))
    else:
        scopes = sorted(tuple(k.split(":", 1)) for k in tuned)
    if not scopes:
        raise SystemExit("provide --params-json or --scopes")

    out = {}
    for asset, period in scopes:
        label = f"{asset}:{period}"
        base = tuned.get(label, {})
        threshold = float(base.get("threshold", 0.90))
        current = float(base.get("max_entry_price", 0.94))
        params = params_for(asset, period, base, args.stake, args.slippage)
        started = time.time()
        print(f"=== {label} threshold={threshold} current_cap={current} ===",
              flush=True)
        try:
            res = run_backtest(args.root, asset, period, args.start, args.end, params)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {exc}", flush=True)
            clear_series_cache()
            continue
        trades = res.trades
        caps = sorted({current} | {c for c in CAPS if c > threshold})
        rows = {str(c): row_for(trades, c) for c in caps}
        print(f"  markets={res.summary['markets_scanned']} all_trades={len(trades)} "
              f"({time.time() - started:.0f}s)", flush=True)
        print("   cap  trades  win%    net     dd     net/dd  months+  drop_pnl", flush=True)
        for c in caps:
            r = rows[str(c)]
            mark = "*" if abs(c - current) < 1e-9 else " "
            ratio = f"{r['ratio']:.1f}" if r["ratio"] is not None else "  -"
            print(f"  {mark}{c:.2f} {r['trades']:>6} {r['win_rate']*100:5.1f} "
                  f"{r['net_pnl']:>+9.1f} {r['max_drawdown']:>+8.1f} {ratio:>7} "
                  f"{r['months_positive']}/{r['months_total']:<3} "
                  f"{r['dropped_pnl']:>+8.1f}", flush=True)
        best_net = max(rows.values(), key=lambda r: r["net_pnl"])
        ratio_rows = [r for r in rows.values() if r["ratio"] is not None]
        best_ratio = max(ratio_rows, key=lambda r: r["ratio"]) if ratio_rows else None
        best_train = max(rows.values(), key=lambda r: r["train_net"])
        ratio_text = (f"best_ratio={best_ratio['cap']:.2f} ratio={best_ratio['ratio']:.1f}"
                      if best_ratio else "best_ratio=n/a")
        print(f"  best_net={best_net['cap']:.2f} net={best_net['net_pnl']:+.1f}  "
              f"{ratio_text}  "
              f"train_best={best_train['cap']:.2f} test_net={best_train['test_net']:+.1f}",
              flush=True)
        out[label] = {
            "threshold": threshold,
            "current_cap": current,
            "rows": rows,
            "best_net_cap": best_net["cap"],
            "best_ratio_cap": best_ratio["cap"] if best_ratio else None,
            "train_best_cap": best_train["cap"],
            "train_best_test_net": best_train["test_net"],
        }
        clear_series_cache()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
