#!/usr/bin/env python
"""Backtest the mid-crossing filter over the PolyKlines history.

For every market the backtest records the number of effective UP-mid
crossings of 0.5 from the window open to the entry tick. The same set of
trades can then be re-scored at several ``MAX_MID_CROSSINGS`` limits without
re-reading the dataset.

    python scripts/analyze_mid_crossings.py \
        --root /opt/polyklines-history/dataset \
        --start 2026-05-01 --end 2026-11-04 \
        --scopes DOGE:5m,BNB:15m
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


def load_params(path: str) -> dict[str, dict]:
    if not path:
        return {}
    return json.loads(Path(path).read_text())


def parse_scopes(raw: str, params: dict[str, dict]) -> list[tuple[str, str]]:
    if not raw:
        scopes = sorted(tuple(k.split(":", 1)) for k in params)
    else:
        scopes = []
        for item in raw.split(","):
            asset, _, period = item.strip().partition(":")
            if not asset or not period:
                raise SystemExit(f"bad scope {item!r}; use ASSET:PERIOD")
            scopes.append((asset.upper(), period))
    return scopes


def params_for(asset: str, period: str, stake: float, slippage: float,
               tuned: dict[str, dict], level: float, band: float) -> BacktestParams:
    base = tuned.get(f"{asset}:{period}", {})
    return BacktestParams(
        entry_window_sec=float(base.get("entry_window_sec", 60)),
        threshold=float(base.get("threshold", 0.90)),
        confirm_sec=float(base.get("confirm_sec", 0)),
        max_entry_price=float(base.get("max_entry_price", 0.94)),
        min_entry_price=float(base.get("min_entry_price", 0.0)),
        stake=stake,
        slippage=slippage,
        fee_rate=0.05 if asset == "BTC" else 0.01,
        fee_model="official",
        mid_cross_band=band,
        mid_cross_level=level,
        mid_cross_sample_sec=float(base.get(
            "mid_cross_sample_sec", 5.0 if period == "15m" else 1.0
        )),
        full_window=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/polyklines-history/dataset")
    parser.add_argument("--start", default="2026-05-01")
    parser.add_argument("--end", default="2026-11-04")
    parser.add_argument("--scopes", default="", help="ASSET:PERIOD,... (default: all)")
    parser.add_argument("--thresholds", default="1,2,3,5")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--level", type=float, default=0.5,
                        help="UP-mid crossing level (default 0.5)")
    parser.add_argument("--band", type=float, default=0.02,
                        help="deadband around the crossing level")
    parser.add_argument("--params-json", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    thresholds = [int(x) for x in args.thresholds.split(",") if x.strip()]
    tuned = load_params(args.params_json)
    if not tuned and not args.scopes:
        raise SystemExit("provide --params-json or --scopes")
    scopes = parse_scopes(args.scopes, tuned)
    per_scope: dict[str, dict] = {}
    aggregate: dict[str, list[dict]] = {"off": []}
    for th in thresholds:
        aggregate[str(th)] = []

    for asset, period in scopes:
        params = params_for(
            asset, period, args.stake, args.slippage, tuned,
            args.level, args.band,
        )
        label = f"{asset}:{period}"
        started = time.time()
        progress_state = {"last": -1}

        def progress(frac: float, message: str) -> None:
            step = int(frac * 10)
            if step != progress_state["last"]:
                progress_state["last"] = step
                print(f"  {label} {step * 10:>3}% · {message}", flush=True)

        print(f"=== {label}  params={params.to_dict()} ===", flush=True)
        result = run_backtest(
            args.root, asset, period, args.start, args.end, params,
            progress=progress,
        )
        trades = result.trades
        top_skips = sorted(result.skips.items(), key=lambda kv: -kv[1])[:6]
        print(
            f"  markets={result.summary['markets_scanned']} trades={len(trades)} "
            f"net={result.summary['net_pnl']:+.2f} "
            f"win={result.summary['win_rate'] * 100:.1f}% "
            f"({time.time() - started:.0f}s) skips={top_skips}",
            flush=True,
        )
        scope_rows = {}
        for key in ["off"] + [str(t) for t in thresholds]:
            limit = None if key == "off" else int(key)
            kept = trades if limit is None else [
                t for t in trades
                if t["mid_crossings"] is not None and t["mid_crossings"] <= limit
            ]
            skipped = [] if limit is None else [
                t for t in trades
                if t["mid_crossings"] is None or t["mid_crossings"] > limit
            ]
            m = metrics(kept)
            skipped_pnl = sum(float(t["pnl"]) for t in skipped)
            row = {
                "trades": m["trades"],
                "win_rate": m["win_rate"],
                "net_pnl": m["net_pnl"],
                "max_drawdown": m["max_drawdown"],
                "skipped": len(skipped),
                "skipped_pnl": skipped_pnl,
            }
            scope_rows[key] = row
            aggregate[key].extend(kept)
            print(
                f"  {'off' if limit is None else '>'+key:>4}: "
                f"trades {row['trades']:>5}  win {row['win_rate']*100:5.1f}%  "
                f"net {row['net_pnl']:+9.2f}  dd {row['max_drawdown']:+8.2f}  "
                f"skipped {row['skipped']:>4} ({row['skipped_pnl']:+8.2f})",
                flush=True,
            )
        per_scope[label] = {"params": params.to_dict(), "rows": scope_rows}
        clear_series_cache()

    print("\n=== aggregate ===", flush=True)
    agg_rows = {}
    for key in ["off"] + [str(t) for t in thresholds]:
        m = metrics(aggregate[key])
        agg_rows[key] = {
            "trades": m["trades"], "win_rate": m["win_rate"],
            "net_pnl": m["net_pnl"], "max_drawdown": m["max_drawdown"],
        }
        label = "off" if key == "off" else ">" + key
        print(
            f"  {label:>4}: trades {m['trades']:>6}  win {m['win_rate']*100:5.1f}%  "
            f"net {m['net_pnl']:+10.2f}  dd {m['max_drawdown']:+9.2f}",
            flush=True,
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"scopes": per_scope, "aggregate": agg_rows,
             "date_range": [args.start, args.end]},
            indent=2, ensure_ascii=False,
        ))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
