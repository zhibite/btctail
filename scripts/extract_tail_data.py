#!/usr/bin/env python
"""Extract the last N seconds of every market into a compact dense table.

The full dataset is ~300 MB compressed for ETH alone. Parameter searches
re-read the same series thousands of times, so this script distils each
market down to the only samples the late-window rule can touch: the
``wmax`` seconds before ``end_epoch``.

Output (``--out`` directory)::

    markets_<asset>_<period>.csv.gz   idx,slug,end_ts,resolved,date
    samples_<asset>_<period>.csv.gz   idx,offset,up,down
    summary_<asset>_<period>.json

``offset`` is whole seconds before the market close (1..wmax), ``up`` and
``down`` are prices in millionths (-1 means no observation). Fine (1 s)
rows win over the coarse main series, matching ``src.research.dataset``.

Only the Python standard library is used so it runs on the research VPS
without installing numpy/pandas.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from pathlib import Path

PERIOD_SECONDS = {"5m": 300, "15m": 900}


def _open_text(path: Path):
    return gzip.open(path, "rt", newline="", encoding="utf-8", errors="replace")


def _find(day_dir: Path, stem: str) -> Path | None:
    for name in (f"{stem}.csv.gz", f"{stem}.csv"):
        path = day_dir / name
        if path.exists():
            return path
    return None


def _read_markets(path: Path) -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
    with _open_text(path) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            slug = (row.get("slug") or "").strip()
            if not slug:
                continue
            try:
                end_ts = float(row.get("end_epoch") or "")
            except ValueError:
                continue
            resolved = (row.get("resolved") or "").strip().upper()
            previous = out.get(slug)
            if previous is None:
                out[slug] = (end_ts, resolved)
                continue
            prev_end, prev_resolved = previous
            # History merges can append a metadata-empty row for the same
            # slug. A plain last-wins dict silently drops every resolution;
            # prefer a row that actually carries a resolved side.
            if resolved and not prev_resolved:
                out[slug] = (end_ts, resolved)
            elif resolved and prev_resolved:
                if end_ts > prev_end:
                    out[slug] = (end_ts, resolved)
            elif not prev_resolved:
                out[slug] = (end_ts, resolved)
    return out


def _read_tail(path: Path, ends: dict[str, float], wmax: int,
               sink: dict[str, dict[int, tuple[int, int]]],
               counters: dict[str, int]) -> int:
    """Merge rows with 0 < end - time <= wmax into ``sink``.

    ``sink`` keeps the first value seen for a timestamp; callers pass the
    fine file first so it shadows the coarse main series.
    """
    kept = 0
    with _open_text(path) as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            slug = row.get("slug")
            if not slug:
                counters["missing_slug"] += 1
                continue
            end_ts = ends.get(slug)
            if end_ts is None:
                counters["unknown_market"] += 1
                continue
            try:
                time_ms = int(row["time"])
            except (KeyError, ValueError):
                counters["bad_time"] += 1
                continue
            offset_ms = round((end_ts * 1000.0) - time_ms)
            if offset_ms <= 0 or offset_ms > wmax * 1000:
                counters["outside_tail"] += 1
                continue
            if offset_ms % 1000:
                # The tail series is sampled on whole seconds; a stray
                # sub-second row would break the dense grid.
                counters["subsecond"] += 1
                continue
            offset = offset_ms // 1000
            market = sink.setdefault(slug, {})
            if offset in market:
                counters["duplicate"] += 1
                continue
            up = _micro(row.get("up_price"))
            down = _micro(row.get("down_price"))
            market[offset] = (up, down)
            kept += 1
    return kept


def _micro(value: str | None) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1
    if number != number:  # NaN
        return -1
    return int(round(number * 1_000_000))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract late-window samples")
    parser.add_argument("--root", required=True, help="dataset root")
    parser.add_argument("--asset", default="eth")
    parser.add_argument("--period", required=True, choices=sorted(PERIOD_SECONDS))
    parser.add_argument("--wmax", type=int, default=120,
                        help="seconds before close to keep (default 120)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root) / f"asset={args.asset.lower()}" / f"period={args.period}"
    days = sorted(p for p in root.glob("date=*") if p.is_dir())
    if args.start:
        days = [p for p in days if p.name.split("=", 1)[1] >= args.start]
    if args.end:
        days = [p for p in days if p.name.split("=", 1)[1] <= args.end]
    if not days:
        print(f"no days under {root}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / f"markets_{args.asset}_{args.period}.csv.gz"
    samples_path = out_dir / f"samples_{args.asset}_{args.period}.csv.gz"

    started = time.time()
    idx = 0
    total_samples = 0
    resolved_counts: dict[str, int] = {}
    date_counts: dict[str, int] = {}
    first_date = last_date = None
    counters = {name: 0 for name in
                ("missing_slug", "unknown_market", "bad_time", "outside_tail",
                 "subsecond", "duplicate")}

    with gzip.open(meta_path, "wt", newline="", encoding="utf-8") as meta_handle, \
            gzip.open(samples_path, "wt", newline="", encoding="utf-8") as sample_handle:
        meta_writer = csv.writer(meta_handle)
        sample_writer = csv.writer(sample_handle)
        meta_writer.writerow(["idx", "slug", "end_ts", "resolved", "date"])
        sample_writer.writerow(["idx", "offset", "up", "down"])

        for day_index, day_dir in enumerate(days):
            date = day_dir.name.split("=", 1)[1]
            markets_path = _find(day_dir, "markets")
            if markets_path is None:
                continue
            markets = _read_markets(markets_path)
            if not markets:
                continue
            ends = {slug: end for slug, (end, _) in markets.items()}
            sink: dict[str, dict[int, tuple[int, int]]] = {}

            fine = _find(day_dir, "klines_fine")
            main = _find(day_dir, "klines")
            if fine is not None:
                _read_tail(fine, ends, args.wmax, sink, counters)
            if main is not None:
                _read_tail(main, ends, args.wmax, sink, counters)

            for slug in sorted(sink, key=lambda s: ends[s]):
                samples = sink[slug]
                if not samples:
                    continue
                end_ts, resolved = markets[slug]
                meta_writer.writerow([idx, slug, int(end_ts), resolved, date])
                for offset in sorted(samples):
                    up, down = samples[offset]
                    sample_writer.writerow([idx, offset, up, down])
                idx += 1
                total_samples += len(samples)
                resolved_counts[resolved or ""] = resolved_counts.get(resolved or "", 0) + 1
                date_counts[date] = date_counts.get(date, 0) + 1
                if first_date is None:
                    first_date = date
                last_date = date

            if (day_index + 1) % 20 == 0 or day_index + 1 == len(days):
                print(f"  [{day_index + 1:3d}/{len(days)}] {date} · "
                      f"{idx:,} markets · {total_samples:,} samples", flush=True)

    summary = {
        "asset": args.asset.lower(),
        "period": args.period,
        "wmax": args.wmax,
        "dataset_root": str(root),
        "date_min": first_date,
        "date_max": last_date,
        "days": len(days),
        "markets": idx,
        "samples": total_samples,
        "resolved_counts": resolved_counts,
        "date_counts": date_counts,
        "row_counters": counters,
        "elapsed_sec": round(time.time() - started, 1),
        "outputs": [str(meta_path), str(samples_path)],
    }
    (out_dir / f"summary_{args.asset}_{args.period}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: summary[k] for k in
                      ("date_min", "date_max", "days", "markets", "samples",
                       "resolved_counts", "elapsed_sec")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
