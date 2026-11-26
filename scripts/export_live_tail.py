#!/usr/bin/env python
"""Export PolyKlines live-collector markets to the compact tail format.

Reads ``/opt/polyklines/data/live/polyklines.sqlite`` (read-only) and writes
``markets_<asset>_<period>.csv.gz`` / ``samples_<asset>_<period>.csv.gz``
matching the layout consumed by ``optimize_tail_server.py``.

The collector stores every market's samples in one large table. Querying
per market is far too slow; this exporter scans one UTC day at a time
(57 queries for Oct-Nov) and keeps the last ``wmax`` seconds of each
resolved market. Prices are stored in millionths, -1 = no observation.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _epoch(text: str) -> float:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _mid(row: sqlite3.Row, side: str) -> float | None:
    value = row[f"{side}_mid"]
    if value is not None:
        return float(value)
    bid, ask = row[f"{side}_bid"], row[f"{side}_ask"]
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    last = row[f"{side}_last"]
    return float(last) if last is not None else None


def _millionths(value: float | None) -> int:
    if value is None:
        return -1
    number = int(round(float(value) * 1_000_000))
    return max(0, min(1_000_000, number))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/opt/polyklines/data/live/polyklines.sqlite")
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", default="2026-10-01")
    parser.add_argument("--end", default="2026-11-27")
    parser.add_argument("--wmax", type=int, default=120)
    parser.add_argument("--periods", default="5m,15m")
    args = parser.parse_args()

    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    start, end = _epoch(args.start), _epoch(args.end)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    markets = list(conn.execute(
        """select slug, asset, period, start_epoch, end_epoch, resolved
           from markets
           where period in (%s) and start_epoch>=? and start_epoch<?
             and resolved in ('UP','DOWN')
           order by asset, period, start_epoch""" % ",".join("?" * len(periods)),
        (*periods, start, end),
    ))
    print(f"{len(markets)} resolved markets", flush=True)

    scopes = sorted({(m["asset"], m["period"]) for m in markets})
    meta_fh, sample_fh = {}, {}
    meta, samples = {}, {}
    index, slug_index = {}, {}
    for asset, period in scopes:
        mp = out_dir / f"markets_{asset}_{period}.csv.gz"
        sp = out_dir / f"samples_{asset}_{period}.csv.gz"
        meta_fh[(asset, period)] = gzip.open(mp, "wt", newline="", encoding="utf-8")
        sample_fh[(asset, period)] = gzip.open(sp, "wt", newline="", encoding="utf-8")
        meta[(asset, period)] = csv.writer(meta_fh[(asset, period)])
        samples[(asset, period)] = csv.writer(sample_fh[(asset, period)])
        meta[(asset, period)].writerow(["idx", "slug", "end_ts", "resolved", "date"])
        samples[(asset, period)].writerow(["idx", "offset", "up", "down"])
        # optimize_tail_server indexes its arrays with the market idx, so
        # this must be 0-based (the history extract uses 0..n-1 too).
        index[(asset, period)] = -1

    by_day: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for market in markets:
        key = (market["asset"], market["period"])
        index[key] += 1
        idx = index[key]
        date = datetime.fromtimestamp(
            int(market["start_epoch"]), timezone.utc
        ).strftime("%Y-%m-%d")
        meta[key].writerow([idx, market["slug"], int(market["end_epoch"]),
                            market["resolved"], date])
        slug_index[market["slug"]] = (key, idx, int(market["end_epoch"]))
        end_day = datetime.fromtimestamp(
            int(market["end_epoch"]), timezone.utc
        ).strftime("%Y-%m-%d")
        by_day[end_day].append(market)

    for day in sorted(by_day):
        day_start = _epoch(day + "T00:00:00+00:00")
        day_end = day_start + 86400
        rows = conn.execute(
            """select slug, ts, up_bid, up_ask, up_mid, up_last,
                      down_bid, down_ask, down_mid, down_last
               from samples where ts>=? and ts<?
               order by ts""",
            (day_start - args.wmax, day_end),
        ).fetchall()
        by_slug: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            by_slug[row["slug"]].append(row)
        for market in by_day[day]:
            key, idx, _ = slug_index[market["slug"]]
            seen: set[int] = set()
            for row in by_slug.get(market["slug"], []):
                offset = int(market["end_epoch"]) - int(row["ts"])
                if offset < 1 or offset > args.wmax or offset in seen:
                    continue
                seen.add(offset)
                samples[key].writerow([idx, offset, _millionths(_mid(row, "up")),
                                       _millionths(_mid(row, "down"))])
        print(f"{day}: {len(rows):,} sample rows", flush=True)

    for handle in list(meta_fh.values()) + list(sample_fh.values()):
        handle.close()
    conn.close()
    print(f"done -> {out_dir} ({len(markets)} markets)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
