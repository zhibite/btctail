#!/usr/bin/env python
"""Evaluate the live robust combos on the Oct-Nov live tail extract."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import optimize_tail_server as ots  # noqa: E402

CURRENT = {
    "BTC:15m": dict(window=75, threshold=0.84, confirm=15, cap=0.96),
    "BTC:5m": dict(window=50, threshold=0.94, confirm=5, cap=0.96),
    "ETH:15m": dict(window=30, threshold=0.84, confirm=0, cap=0.91),
    "ETH:5m": dict(window=15, threshold=0.84, confirm=3, cap=0.99),
    "SOL:15m": dict(window=60, threshold=0.86, confirm=0, cap=0.97),
    "SOL:5m": dict(window=30, threshold=0.84, confirm=0, cap=0.95),
    "XRP:15m": dict(window=60, threshold=0.87, confirm=0, cap=0.99),
    "XRP:5m": dict(window=40, threshold=0.84, confirm=0, cap=0.99),
    "DOGE:15m": dict(window=45, threshold=0.92, confirm=5, cap=0.96),
    "DOGE:5m": dict(window=50, threshold=0.88, confirm=8, cap=0.97),
    "BNB:15m": dict(window=60, threshold=0.84, confirm=0, cap=0.99),
    "BNB:5m": dict(window=25, threshold=0.90, confirm=2, cap=0.99),
    "HYPE:15m": dict(window=75, threshold=0.89, confirm=15, cap=0.98),
    "HYPE:5m": dict(window=60, threshold=0.89, confirm=10, cap=0.96),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/root/tail_twap_live")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--scopes", default="", help="ASSET:PERIOD,...")
    args = parser.parse_args()

    scopes = list(CURRENT)
    if args.scopes:
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]

    print(f"{'scope':<9} {'markets':>7} {'trades':>6} {'win%':>6} {'net':>9} "
          f"{'dd':>8} {'ratio':>6}  months")
    for scope in scopes:
        asset, period = scope.split(":")
        wmax = 90 if period == "5m" else 120
        data = ots.load_tail(Path(args.root), asset.lower(), period, wmax)
        fee = 0.05 if asset.upper() == "BTC" else 0.01
        stats = ots.evaluate(data, CURRENT[scope], args.stake, fee, args.slippage)
        dd = abs(float(stats["max_drawdown"]))
        ratio = float(stats["net_pnl"]) / dd if dd else None
        months = ", ".join(
            f"{label} {stats['months_net'][i]:+.1f}/{stats['months_n'][i]}"
            for i, label in enumerate(data.month_labels)
        )
        print(f"{scope:<9} {data.n:>7} {stats['trades']:>6} "
              f"{stats['win_rate'] * 100:>5.1f} {stats['net_pnl']:>+9.1f} "
              f"{stats['max_drawdown']:>+8.1f} "
              f"{(f'{ratio:.1f}' if ratio is not None else '  -'):>6}  {months}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
