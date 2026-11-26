#!/usr/bin/env python
"""Server-side (numpy-only) variant of ``optimize_tail_grid.py``.

The research VPS has ~350 MB free RAM and no pandas. This script reads the
compact extracts produced by ``extract_tail_data.py`` with the standard
library csv module into float32/int16 arrays, then runs the same vectorised
grid as the local optimizer. It is intentionally a separate entry point so
the local pandas version remains the reference implementation.

Modes
-----
Grid::

    ~/venv/bin/python optimize_tail_server.py --asset btc --period 15m \
        --extract-dir ~/tail_extract --slippage 0.01 --out ~/tail_results/btc_15m

Candidate evaluation (after picking a shortlist)::

    ~/venv/bin/python optimize_tail_server.py --asset btc --period 15m \
        --extract-dir ~/tail_extract --evaluate-json candidates.json \
        --out ~/tail_results/btc_15m_final

``grid.csv`` and ``summary.json`` / ``candidates.json`` are small; only
those need to be copied off the server.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PERIOD_SECONDS = {"5m": 300, "15m": 900}

GRID_15M = {
    "window": [30, 45, 60, 75, 90, 105, 120],
    "threshold": [0.84, 0.86, 0.87, 0.88, 0.89, 0.90, 0.91, 0.92, 0.93, 0.94, 0.95],
    "confirm": [0, 2, 3, 5, 8, 10, 15, 20],
    "cap": [0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99],
}
GRID_5M = {
    "window": [10, 15, 20, 25, 30, 40, 50, 60, 75, 90],
    "threshold": [0.84, 0.86, 0.88, 0.89, 0.90, 0.91, 0.92, 0.93, 0.94, 0.95],
    "confirm": [0, 1, 2, 3, 5, 8, 10, 15],
    "cap": [0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99],
}


@dataclass
class TailData:
    asset: str
    period: str
    wmax: int
    up: np.ndarray
    down: np.ndarray
    has_sample: np.ndarray
    price_max: np.ndarray
    side: np.ndarray
    resolved: np.ndarray
    month: np.ndarray
    month_labels: list[str]
    end_ts: np.ndarray
    dates: np.ndarray
    next_sample: np.ndarray
    n: int

    @property
    def offsets(self) -> np.ndarray:
        return np.arange(self.wmax + 1, dtype=np.int16)


def load_tail(path: Path, asset: str, period: str, wmax: int) -> TailData:
    meta_path = path / f"markets_{asset}_{period}.csv.gz"
    samples_path = path / f"samples_{asset}_{period}.csv.gz"
    dates: list[str] = []
    ends: list[int] = []
    resolved_text: list[str] = []
    with gzip.open(meta_path, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            dates.append(row["date"])
            ends.append(int(row["end_ts"]))
            resolved_text.append((row.get("resolved") or "").strip().upper())
    n = len(dates)
    up = np.full((n, wmax + 1), np.nan, dtype=np.float32)
    down = np.full((n, wmax + 1), np.nan, dtype=np.float32)
    has_sample = np.zeros((n, wmax + 1), dtype=bool)

    with gzip.open(samples_path, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        for idx_s, off_s, up_s, down_s in reader:
            idx = int(idx_s)
            off = int(off_s)
            if off < 1 or off > wmax:
                continue
            u = int(up_s)
            d = int(down_s)
            if u >= 0:
                up[idx, off] = u / 1_000_000.0
            if d >= 0:
                down[idx, off] = d / 1_000_000.0
            has_sample[idx, off] = True

    month_labels = sorted({d[:7] for d in dates})
    month_map = {label: i for i, label in enumerate(month_labels)}
    month = np.array([month_map[d[:7]] for d in dates], dtype=np.int8)
    resolved = np.zeros(n, dtype=np.int8)
    for i, value in enumerate(resolved_text):
        if value == "UP":
            resolved[i] = 1
        elif value == "DOWN":
            resolved[i] = 2

    u_valid = ~np.isnan(up)
    d_valid = ~np.isnan(down)
    price_max = np.fmax(up, down)
    side = np.where(
        d_valid & (~u_valid | (down > up)),
        2,
        np.where(u_valid, 1, 0),
    ).astype(np.int8)

    next_sample = np.zeros((n, wmax + 2), dtype=np.int16)
    nxt = np.zeros(n, dtype=np.int16)
    for offset in range(wmax, 0, -1):
        nxt = np.where(has_sample[:, offset], offset, nxt)
        next_sample[:, offset] = nxt

    return TailData(
        asset=asset, period=period, wmax=wmax, up=up, down=down,
        has_sample=has_sample, price_max=price_max, side=side,
        resolved=resolved, month=month, month_labels=month_labels,
        end_ts=np.asarray(ends, dtype=np.int64),
        dates=np.asarray(dates, dtype=object), next_sample=next_sample, n=n,
    )


def segment_stats(mask: np.ndarray, pnl: np.ndarray, win: np.ndarray,
                  price: np.ndarray, month: np.ndarray, n_months: int,
                  stake: float) -> dict:
    n = int(mask.sum())
    if n == 0:
        return {
            "trades": 0, "net_pnl": 0.0, "roi": 0.0, "win_rate": 0.0,
            "avg_entry_price": 0.0, "price_edge": 0.0, "t_stat": None,
            "months_net": [0.0] * n_months, "months_n": [0] * n_months,
            "months_wins": [0] * n_months, "positive_months": 0,
            "max_drawdown": 0.0,
        }
    p = pnl[mask]
    net = float(p.sum())
    wins = int(win[mask].sum())
    prices = price[mask]
    monthly_net = np.bincount(month[mask], weights=p, minlength=n_months)
    monthly_n = np.bincount(month[mask], minlength=n_months)
    monthly_wins = np.bincount(month[mask][win[mask]], minlength=n_months)
    mean = net / n
    if n > 1:
        std = float(p.std(ddof=1))
        t_stat = mean / (std / math.sqrt(n)) if std > 0 else None
    else:
        t_stat = None
    cumulative = np.cumsum(p)
    peak = np.maximum.accumulate(cumulative)
    max_dd = float(np.min(cumulative - peak))
    win_rate = wins / n
    avg_price = float(prices.mean())
    return {
        "trades": n, "net_pnl": net,
        "roi": net / (n * stake) if stake else 0.0,
        "win_rate": win_rate, "avg_entry_price": avg_price,
        "price_edge": win_rate - avg_price, "t_stat": t_stat,
        "months_net": monthly_net.tolist(),
        "months_n": monthly_n.astype(int).tolist(),
        "months_wins": monthly_wins.astype(int).tolist(),
        "positive_months": int((monthly_net > 0).sum()),
        "max_drawdown": max_dd,
    }


def evaluate(data: TailData, params: dict, stake: float, fee_rate: float,
             slippage: float, months: list[int] | None = None) -> dict:
    rows = np.arange(data.n)
    window = int(params["window"])
    threshold = float(params["threshold"])
    confirm = float(params["confirm"])
    cap = float(params["cap"])

    hit = data.price_max[:, : window + 1] >= threshold
    reversed_hit = hit[:, ::-1]
    any_hit = reversed_hit.any(axis=1)
    from_end = reversed_hit.argmax(axis=1)
    first = np.where(any_hit, window - from_end, 0).astype(np.int16)
    side = data.side[rows, first]

    if confirm > 0:
        target = np.maximum(1, first.astype(np.int64) - int(confirm))
        entry_off = data.next_sample[rows, target]
        price = np.where(side == 1, data.up[rows, entry_off],
                         data.down[rows, entry_off])
        valid = (
            (first > 0) & (first >= confirm) & (entry_off > 0)
            & (entry_off < first) & (price >= threshold)
        )
    else:
        entry_off = first
        price = data.price_max[rows, first]
        valid = first > 0

    fill = price.astype(np.float64) + slippage
    valid &= np.isfinite(fill) & (fill > 0) & (fill < 1.0) & (price < cap)
    safe_fill = np.where(valid, fill, 1.0)
    shares = np.where(valid, stake / safe_fill, 0.0)
    fee = shares * fee_rate * safe_fill * (1.0 - safe_fill)
    win = (data.resolved == side) & valid
    pnl = np.where(win, shares, 0.0) - np.where(valid, stake + fee, 0.0)

    mask = valid
    if months is not None:
        mask = mask & np.isin(data.month, months)
    return segment_stats(mask, pnl, win, price, data.month,
                         len(data.month_labels), stake)


def grid_search(data: TailData, grid: dict, stake: float, fee_rate: float,
                slippage: float, train_months: list[int],
                progress=None) -> list[dict]:
    rows = np.arange(data.n)
    n_months = len(data.month_labels)
    offsets = data.offsets
    results: list[dict] = []
    total = (len(grid["threshold"]) * len(grid["window"])
             * len(grid["confirm"]) * len(grid["cap"]))
    done = 0
    started = time.time()

    for threshold in grid["threshold"]:
        hit = data.price_max >= threshold
        earliest = np.maximum.accumulate(
            np.where(hit, offsets[None, :], 0), axis=1
        ).astype(np.int16)
        for window in grid["window"]:
            first = earliest[:, window]
            side = data.side[rows, first]
            for confirm in grid["confirm"]:
                if confirm > 0:
                    target = np.maximum(1, first.astype(np.int64) - confirm)
                    entry_off = data.next_sample[rows, target]
                    price = np.where(side == 1, data.up[rows, entry_off],
                                     data.down[rows, entry_off])
                    valid = (
                        (first > 0) & (first >= confirm) & (entry_off > 0)
                        & (entry_off < first) & (price >= threshold)
                    )
                else:
                    entry_off = first
                    price = data.price_max[rows, first]
                    valid = first > 0

                fill = price.astype(np.float64) + slippage
                valid &= np.isfinite(fill) & (fill > 0) & (fill < 1.0)
                safe_fill = np.where(valid, fill, 1.0)
                shares = np.where(valid, stake / safe_fill, 0.0)
                fee = shares * fee_rate * safe_fill * (1.0 - safe_fill)
                win = (data.resolved == side) & valid
                pnl = np.where(win, shares, 0.0) - np.where(valid, stake + fee, 0.0)

                for cap in grid["cap"]:
                    done += 1
                    if cap <= threshold:
                        continue
                    mask = valid & (price < cap)
                    if not mask.any():
                        continue
                    stats = segment_stats(
                        mask, pnl, win, price, data.month, n_months, stake
                    )
                    if stats["trades"] == 0:
                        continue
                    stats["params"] = {
                        "window": window, "threshold": threshold,
                        "confirm": confirm, "cap": cap,
                    }
                    results.append(stats)
                    if progress is not None and done % 1000 == 0:
                        progress(done, total, time.time() - started)
        del earliest, hit
    if progress is not None:
        progress(total, total, time.time() - started)
    return results


def _combine(item: dict, indices: list[int], stake: float) -> dict:
    n = sum(item["months_n"][i] for i in indices)
    net = sum(item["months_net"][i] for i in indices)
    wins = sum(item["months_wins"][i] for i in indices)
    positive = sum(1 for i in indices if item["months_net"][i] > 0)
    if n == 0:
        return {"trades": 0, "net_pnl": 0.0, "roi": 0.0, "win_rate": 0.0,
                "positive_months": 0}
    return {"trades": n, "net_pnl": net, "roi": net / (n * stake),
            "win_rate": wins / n, "positive_months": positive}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Server-side tail optimizer")
    parser.add_argument("--asset", default="btc")
    parser.add_argument("--period", required=True, choices=sorted(PERIOD_SECONDS))
    parser.add_argument("--extract-dir", default=str(Path.home() / "tail_extract"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--stake", type=float, default=10.0)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--evaluate-json", default=None,
                        help="evaluate named candidates instead of running the grid")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args(argv)

    asset = args.asset.lower()
    period = args.period
    wmax = 120 if period == "15m" else 90
    out = Path(args.out) if args.out else Path.home() / "tail_results" / f"{asset}_{period}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"loading {asset.upper()} {period} from {args.extract_dir} ...", flush=True)
    data = load_tail(Path(args.extract_dir), asset, period, wmax)
    print(f"  {data.n:,} markets · months {data.month_labels[0]} → {data.month_labels[-1]}",
          flush=True)
    train_months = [i for i, m in enumerate(data.month_labels) if m <= "2026-07"]
    val_months = [i for i, m in enumerate(data.month_labels) if m >= "2026-08"]

    if args.evaluate_json:
        raw = json.loads(Path(args.evaluate_json).read_text(encoding="utf-8"))
        candidates = list(raw.items()) if isinstance(raw, dict) else [
            (f"cand_{i}", v) for i, v in enumerate(raw)
        ]
        payload = {}
        for name, params in candidates:
            entry = {"params": params, "stats": {}}
            for slip in (0.0, 0.005, 0.01, 0.015, 0.02):
                entry["stats"][str(slip)] = {
                    "full": evaluate(data, params, args.stake, args.fee_rate, slip),
                    "train": evaluate(data, params, args.stake, args.fee_rate, slip, train_months),
                    "validate": evaluate(data, params, args.stake, args.fee_rate, slip, val_months),
                }
            payload[name] = entry
            s = entry["stats"][str(args.slippage)]["full"]
            print(f"  {name:<22} n={s['trades']:>5} net={s['net_pnl']:+9.2f} "
                  f"roi={s['roi'] * 100:+6.2f}% t={s['t_stat']:.2f} "
                  f"dd={s['max_drawdown']:+.2f} months+={s['positive_months']}/6", flush=True)
        (out / "candidates.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=float),
            encoding="utf-8",
        )
        print(f"wrote {out / 'candidates.json'}")
        return 0

    grid = GRID_15M if period == "15m" else GRID_5M

    def progress(done, total, elapsed):
        print(f"  grid {done}/{total} · {elapsed:.1f}s", flush=True)

    print("grid search ...", flush=True)
    results = grid_search(data, grid, args.stake, args.fee_rate,
                          args.slippage, train_months, progress=progress)
    print(f"  {len(results):,} combos", flush=True)

    for item in results:
        item["train"] = _combine(item, train_months, args.stake)
        item["validate"] = _combine(item, val_months, args.stake)

    rows = []
    for item in results:
        p = item["params"]
        rows.append({
            "asset": asset, "period": period, **p,
            "trades": item["trades"], "net": item["net_pnl"],
            "roi": item["roi"], "win_rate": item["win_rate"],
            "avg_price": item["avg_entry_price"],
            "price_edge": item["price_edge"], "t_stat": item["t_stat"],
            "max_drawdown": item["max_drawdown"],
            "positive_months": item["positive_months"],
            **{f"m{i}_net": item["months_net"][i] for i in range(len(data.month_labels))},
            **{f"m{i}_n": item["months_n"][i] for i in range(len(data.month_labels))},
            **{f"m{i}_win": item["months_wins"][i] for i in range(len(data.month_labels))},
            "train_trades": item["train"]["trades"],
            "train_net": item["train"]["net_pnl"],
            "train_roi": item["train"]["roi"],
            "train_win_rate": item["train"]["win_rate"],
            "train_positive_months": item["train"]["positive_months"],
            "val_trades": item["validate"]["trades"],
            "val_net": item["validate"]["net_pnl"],
            "val_roi": item["validate"]["roi"],
            "val_win_rate": item["validate"]["win_rate"],
            "val_positive_months": item["validate"]["positive_months"],
        })
    csv_path = out / "grid.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "asset": asset, "period": period, "date_min": str(data.dates[0]),
        "date_max": str(data.dates[-1]), "markets": int(data.n),
        "months": data.month_labels, "slippage": args.slippage,
        "stake": args.stake, "fee_rate": args.fee_rate, "combos": len(results),
        "grid": grid,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8",
    )

    top = sorted([r for r in rows if r["trades"] >= (200 if period == "15m" else 500)],
                 key=lambda r: r["net"], reverse=True)[: args.top]
    print(f"\nTOP {len(top)} by full net (slippage {args.slippage}):")
    for r in top:
        print(f"  W={r['window']:>3} T={r['threshold']:.2f} C={r['confirm']:>2} "
              f"cap={r['cap']:.2f} n={r['trades']:>5} net={r['net']:+9.2f} "
              f"roi={r['roi'] * 100:+5.2f}% t={(r['t_stat'] or 0):.2f} "
              f"dd={r['max_drawdown']:+8.2f} m+={r['positive_months']}/6 "
              f"train={r['train_net']:+8.1f} val={r['val_net']:+8.1f}")
    print(f"\nwrote {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
