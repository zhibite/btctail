"""Late-window entry-opportunity experiments (15m and 5m).

The documented 15m rule skips ~94% of windows with ``price_too_high``: the
favourite jumps straight past the 0.94 cap, so waiting for a first crossing
inside [0.90, 0.94) rarely happens. The 5m series has the same problem plus
a jump to 0.985+ inside the last 30 seconds. This module tests rule variants
that try to convert those skips into entries without dropping the cap:

  * retry    — after a confirmed price >= cap, keep scanning the rest of the
               window for a dip back into [threshold, cap) and enter there
  * rearm    — after a failed confirmation, allow a new trigger (the live
               engine has this as an option; it is off by default)
  * soft     — confirmation only has to stay above a softer floor
  * window / threshold / confirm / cap variations

Usage
-----
    python -m src.research.experiments_late_window                 # 15m, Aug-Oct
    python -m src.research.experiments_late_window --period 5m     # 5m, Jul-Sep
    python -m src.research.experiments_late_window --period 5m --variants baseline,thr_088
    python -m src.research.experiments_late_window --period 5m --sweep

The data root defaults to ``<repo>/data/dataset`` when that has
``asset=*/period=*/date=*`` partitions, otherwise the sibling research export.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from src import config
from src.market.models import Side
from src.research.backtest import metrics
from src.research.dataset import horizon_seconds, load_series
from src.strategy.late_favorite import StrategyParams, taker_fee

DEFAULT_START = {"15m": "2026-08-01", "5m": "2026-07-01"}
DEFAULT_END = {"15m": "2026-10-31", "5m": "2026-09-30"}


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleParams:
    entry_window_sec: float = 60.0
    threshold: float = 0.90
    confirm_sec: float = 10.0
    max_entry_price: float = 0.94
    # Confirmation must stay at/above this (default: the trigger threshold).
    confirm_threshold: Optional[float] = None
    # After a confirmed price >= cap, keep scanning for a dip into range.
    retry_below_cap: bool = False
    # Confirmation used for entries that come from the retry path.
    retry_confirm_sec: float = 0.0
    # After a failed confirmation, require a fresh crossing.
    rearm_after_fail: bool = False

    def to_strategy_params(self) -> StrategyParams:
        return StrategyParams(
            entry_window_sec=self.entry_window_sec,
            threshold=self.threshold,
            confirm_sec=self.confirm_sec,
            max_entry_price=self.max_entry_price,
        )

    def key(self) -> str:
        parts = [
            f"w{self.entry_window_sec:.0f}",
            f"t{self.threshold:.3f}",
            f"c{self.confirm_sec:.0f}",
            f"cap{self.max_entry_price:.3f}",
        ]
        if self.confirm_threshold is not None:
            parts.append(f"ct{self.confirm_threshold:.3f}")
        if self.retry_below_cap:
            parts.append(f"retry{self.retry_confirm_sec:.0f}")
        if self.rearm_after_fail:
            parts.append("rearm")
        return "_".join(parts)


@dataclass
class Entry:
    side: Side
    price: float
    ts: float
    trigger_price: float
    trigger_ts: float
    retry: bool = False


def _price(seq: list[float], i: int) -> Optional[float]:
    value = seq[i]
    if value != value:  # NaN
        return None
    return value


def simulate_window(
    times: list[float],
    up: list[float],
    down: list[float],
    end_ts: float,
    p: RuleParams,
) -> tuple[Optional[Entry], str]:
    """Replay one market. Returns (entry, reason).

    The cap is checked on the **confirmed** price, exactly like the live
    strategy and `evaluate_window_array`; a first crossing above the cap can
    still enter if the price has pulled back by the confirmation moment.
    """
    n = len(times)
    if n == 0:
        return None, "no_data"
    start = end_ts - p.entry_window_sec
    i = bisect_left(times, start)
    # Trigger samples must be strictly inside the window; the confirmation
    # lookup may use a sample exactly at the boundary (mirroring
    # evaluate_window_array, which clamps to searchsorted(end, "right") - 1).
    scan_end_idx = bisect_left(times, end_ts) - 1
    confirm_end_idx = bisect_right(times, end_ts) - 1
    if i > scan_end_idx:
        return None, "no_window_data"

    confirm_threshold = (
        p.confirm_threshold if p.confirm_threshold is not None else p.threshold
    )
    blocked = False
    retry_mode = False

    while i <= scan_end_idx:
        u = _price(up, i)
        d = _price(down, i)
        side: Optional[Side] = None
        price = -1.0
        if u is not None and u >= p.threshold and u > price:
            side, price = Side.UP, u
        if d is not None and d >= p.threshold and d > price:
            side, price = Side.DOWN, d

        if blocked:
            # Re-arm only after both sides fall back below the threshold.
            if side is None:
                blocked = False
            i += 1
            continue
        if side is None:
            i += 1
            continue

        confirm_sec = p.retry_confirm_sec if retry_mode else p.confirm_sec
        floor = p.threshold if retry_mode else confirm_threshold

        if confirm_sec <= 0:
            if price >= p.max_entry_price:
                if p.retry_below_cap:
                    retry_mode = True
                    i += 1
                    continue
                return None, "price_too_high"
            return Entry(side, price, times[i], price, times[i], retry_mode), ""

        target = times[i] + confirm_sec
        if target > end_ts:
            return None, "no_confirmation_time"
        j = bisect_right(times, target) - 1
        if j > confirm_end_idx:
            j = confirm_end_idx
        if j <= i:
            if p.rearm_after_fail and not retry_mode:
                blocked = True
                i = j + 1
                continue
            return None, "no_confirmation"

        p2 = _price(up if side is Side.UP else down, j)
        if p2 is None or p2 < floor:
            if p.rearm_after_fail and not retry_mode:
                blocked = True
                i = j + 1
                continue
            return None, "no_confirmation"
        if p2 >= p.max_entry_price:
            if p.retry_below_cap:
                retry_mode = True
                i = j + 1
                continue
            return None, "price_too_high"
        return Entry(side, p2, times[j], price, times[i], retry_mode), ""

    return None, ("retry_no_fill" if retry_mode else "no_trigger")


# ---------------------------------------------------------------------------
# Variant catalogue
# ---------------------------------------------------------------------------


VARIANTS_15M: list[tuple[str, str, RuleParams]] = [
    ("baseline", "基线 60s/0.90/10s/0.94", RuleParams()),
    ("confirm_5", "确认 5s", RuleParams(confirm_sec=5)),
    ("confirm_0", "无确认", RuleParams(confirm_sec=0)),
    ("cap_095", "上限 0.95", RuleParams(max_entry_price=0.95)),
    ("thr_088", "阈值 0.88", RuleParams(threshold=0.88)),
    ("thr_086", "阈值 0.86", RuleParams(threshold=0.86)),
    ("win_120", "窗口 120s", RuleParams(entry_window_sec=120)),
    ("win_180", "窗口 180s", RuleParams(entry_window_sec=180)),
    ("rearm", "失败后重新武装", RuleParams(rearm_after_fail=True)),
    ("soft_088", "软确认 0.88", RuleParams(confirm_threshold=0.88)),
    ("retry", "超上限后等回撤(确认10s)",
     RuleParams(retry_below_cap=True, retry_confirm_sec=10)),
    ("retry_fast", "超上限后等回撤(立即)", RuleParams(retry_below_cap=True)),
    ("retry_win120", "回撤 + 窗口120s",
     RuleParams(retry_below_cap=True, entry_window_sec=120)),
    ("retry_win180", "回撤 + 窗口180s",
     RuleParams(retry_below_cap=True, entry_window_sec=180)),
    ("retry_thr088", "回撤 + 阈值0.88",
     RuleParams(retry_below_cap=True, threshold=0.88)),
    ("retry_cap095", "回撤 + 上限0.95",
     RuleParams(retry_below_cap=True, max_entry_price=0.95)),
    ("retry_rearm", "回撤 + 重新武装",
     RuleParams(retry_below_cap=True, rearm_after_fail=True)),
    ("retry_combo", "回撤 + 120s + 阈值0.88",
     RuleParams(retry_below_cap=True, entry_window_sec=120, threshold=0.88)),
]


VARIANTS_5M: list[tuple[str, str, RuleParams]] = [
    ("baseline", "基线 30s/0.90/无确认/0.92",
     RuleParams(entry_window_sec=30, threshold=0.90, confirm_sec=0,
                max_entry_price=0.92)),
    ("win_10", "窗口 10s",
     RuleParams(entry_window_sec=10, confirm_sec=0, max_entry_price=0.92)),
    ("win_15", "窗口 15s",
     RuleParams(entry_window_sec=15, confirm_sec=0, max_entry_price=0.92)),
    ("win_20", "窗口 20s",
     RuleParams(entry_window_sec=20, confirm_sec=0, max_entry_price=0.92)),
    ("win_45", "窗口 45s",
     RuleParams(entry_window_sec=45, confirm_sec=0, max_entry_price=0.92)),
    ("win_60", "窗口 60s",
     RuleParams(entry_window_sec=60, confirm_sec=0, max_entry_price=0.92)),
    ("confirm_5", "确认 5s",
     RuleParams(entry_window_sec=30, confirm_sec=5, max_entry_price=0.92)),
    ("confirm_10", "确认 10s",
     RuleParams(entry_window_sec=30, confirm_sec=10, max_entry_price=0.92)),
    ("thr_088", "阈值 0.88",
     RuleParams(entry_window_sec=30, threshold=0.88, confirm_sec=0,
                max_entry_price=0.92)),
    ("thr_086", "阈值 0.86",
     RuleParams(entry_window_sec=30, threshold=0.86, confirm_sec=0,
                max_entry_price=0.92)),
    ("cap_091", "上限 0.91",
     RuleParams(entry_window_sec=30, confirm_sec=0, max_entry_price=0.91)),
    ("cap_093", "上限 0.93",
     RuleParams(entry_window_sec=30, confirm_sec=0, max_entry_price=0.93)),
    ("cap_094", "上限 0.94",
     RuleParams(entry_window_sec=30, confirm_sec=0, max_entry_price=0.94)),
    ("cap_095", "上限 0.95",
     RuleParams(entry_window_sec=30, confirm_sec=0, max_entry_price=0.95)),
    ("soft_088", "软确认 0.88（确认5s）",
     RuleParams(entry_window_sec=30, confirm_sec=5, max_entry_price=0.92,
                confirm_threshold=0.88)),
    ("rearm", "失败后重新武装",
     RuleParams(entry_window_sec=30, confirm_sec=0, max_entry_price=0.92,
                rearm_after_fail=True)),
    ("retry", "超上限后等回撤",
     RuleParams(entry_window_sec=30, confirm_sec=0, max_entry_price=0.92,
                retry_below_cap=True)),
    ("retry_thr088", "回撤 + 阈值0.88",
     RuleParams(entry_window_sec=30, threshold=0.88, confirm_sec=0,
                max_entry_price=0.92, retry_below_cap=True)),
    ("retry_win15", "回撤 + 窗口15s",
     RuleParams(entry_window_sec=15, confirm_sec=0, max_entry_price=0.92,
                retry_below_cap=True)),
    ("combo_088_45", "0.88 + 45s",
     RuleParams(entry_window_sec=45, threshold=0.88, confirm_sec=0,
                max_entry_price=0.92)),
]


def catalogue_for(period: str) -> list[tuple[str, str, RuleParams]]:
    return VARIANTS_5M if period == "5m" else VARIANTS_15M


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class VariantResult:
    key: str
    label: str
    params: RuleParams
    summary: dict = field(default_factory=dict)
    months: dict = field(default_factory=dict)
    skips: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "params": vars(self.params),
            "summary": self.summary,
            "months": self.months,
            "skips": self.skips,
        }


def _month(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def _make_trade(entry: Entry, resolved: str, date: str, asset: str, end_ts: float,
                stake: float, slippage: float, fee_rate: float) -> tuple[Optional[dict], str]:
    fill = entry.price + slippage
    if not (0.0 < fill < 1.0):
        return None, "bad_fill"
    shares = stake / fill
    won = resolved == entry.side.value
    payout = 1.0 if won else 0.0
    fee = taker_fee(shares, fill, fee_rate)
    gross = shares * payout
    pnl = gross - stake - fee
    return {
        "asset": asset,
        "date": date,
        "month": _month(entry.ts),
        "side": entry.side.value,
        "entry_ts": entry.ts,
        "entry_price": entry.price,
        "entry_fill": fill,
        "time_remaining_sec": end_ts - entry.ts,
        "resolved": resolved,
        "payout": payout,
        "shares": shares,
        "stake": stake,
        "gross_proceeds": gross,
        "fees": fee,
        "pnl": pnl,
        "retry": entry.retry,
        "trigger_price": entry.trigger_price,
        "confirm_delay_sec": entry.ts - entry.trigger_ts,
        "time_to_end_sec": 0.0,
    }, ""


def run_variants(
    root,
    asset: str,
    period: str,
    start: Optional[str],
    end: Optional[str],
    variants: dict[str, tuple[str, RuleParams]],
    *,
    stake: float = 10.0,
    slippage: float = 0.005,
    fee_rate: float = 0.07,
    progress=None,
) -> dict[str, VariantResult]:
    max_window = max(p.entry_window_sec for _, p in variants.values())
    max_confirm = max(max(p.confirm_sec, p.retry_confirm_sec) for _, p in variants.values())
    horizon = horizon_seconds(max_window, max_confirm)
    series = load_series(root, asset, period, start, end, horizon)

    results = {
        key: VariantResult(key=key, label=label, params=p)
        for key, (label, p) in variants.items()
    }
    counters = {key: Counter() for key in variants}

    total = len(series)
    for idx, market in enumerate(series):
        if progress is not None and idx % 500 == 0:
            progress(idx / max(1, total), f"扫描 {idx}/{total} 个市场")
        if market.end_ts is None or not market.resolved:
            for key in variants:
                counters[key]["unresolved" if not market.resolved else "no_meta"] += 1
            continue
        times = market.times.tolist()
        up = market.up.tolist()
        down = market.down.tolist()
        end_ts = float(market.end_ts)
        for key, (_, params) in variants.items():
            entry, reason = simulate_window(times, up, down, end_ts, params)
            if entry is None:
                counters[key][reason] += 1
                continue
            trade, bad = _make_trade(entry, market.resolved, market.date, asset,
                                     end_ts, stake, slippage, fee_rate)
            if trade is None:
                counters[key][bad] += 1
                continue
            results[key].trades.append(trade)

    for key, result in results.items():
        result.summary = metrics(result.trades)
        months: dict[str, float] = {}
        for trade in result.trades:
            months[trade["month"]] = months.get(trade["month"], 0.0) + float(trade["pnl"])
        result.months = {m: round(v, 2) for m, v in sorted(months.items())}
        result.skips = dict(sorted(counters[key].items(), key=lambda kv: -kv[1]))
    if progress is not None:
        progress(1.0, "完成")
    return results


def run_sweep(root, asset: str, period: str, start: Optional[str], end: Optional[str],
              *, stake: float = 10.0, slippage: float = 0.005,
              fee_rate: float = 0.07, min_trades: int = 50,
              progress=None) -> list[VariantResult]:
    """Focused retry-family grid: threshold × window × confirm × cap."""
    if period == "5m":
        thresholds = (0.86, 0.88, 0.90)
        windows = (15.0, 30.0, 45.0)
        confirms = (0.0, 5.0)
        caps = (0.91, 0.92, 0.93)
    else:
        thresholds = (0.86, 0.88, 0.90)
        windows = (60.0, 120.0, 180.0)
        confirms = (0.0, 10.0)
        caps = (0.94, 0.95)
    variants: dict[str, tuple[str, RuleParams]] = {}
    for threshold in thresholds:
        for window in windows:
            for confirm in confirms:
                for cap in caps:
                    params = RuleParams(
                        entry_window_sec=window, threshold=threshold,
                        confirm_sec=confirm, max_entry_price=cap,
                        retry_below_cap=True,
                    )
                    key = f"t{threshold:.2f}_w{window:.0f}_c{confirm:.0f}_cap{cap:.2f}"
                    variants[key] = (f"{threshold:.2f}/{window:.0f}s/c{confirm:.0f}/{cap:.2f}", params)
    results = run_variants(root, asset, period, start, end, variants,
                           stake=stake, slippage=slippage, fee_rate=fee_rate,
                           progress=progress)
    ranked = [r for r in results.values() if r.summary.get("trades", 0) >= min_trades]
    ranked.sort(key=lambda r: float(r.summary.get("net_pnl") or 0), reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "--"
    return f"{float(value):.{digits}f}"


def _pct(value, digits: int = 2) -> str:
    if value is None:
        return "--"
    return f"{float(value) * 100:.{digits}f}%"


def print_table(results: Iterable[VariantResult]) -> None:
    rows = sorted(results, key=lambda r: (r.summary.get("trades", 0) > 0,
                                          float(r.summary.get("net_pnl") or 0)),
                  reverse=True)
    print()
    print(f"{'变体':<34}{'笔数':>7}{'胜率':>9}{'平均入场':>11}{'价差':>9}"
          f"{'净利':>10}{'ROI':>9}{'t':>7}{'正月份':>8}")
    print("-" * 108)
    for r in rows:
        s = r.summary
        pos = sum(1 for v in r.months.values() if v > 0)
        print(f"{r.label:<34}{s['trades']:>7}{_pct(s['win_rate']):>9}"
              f"{_fmt(s['avg_entry_price'], 4):>11}{_pct(s['price_edge']):>9}"
              f"{_fmt(s['net_pnl']):>10}{_pct(s['roi']):>9}"
              f"{_fmt(s['t_stat']):>7}{f'{pos}/{len(r.months)}':>8}")
    print()
    print("跳过原因（只列非零）：")
    for r in rows:
        skips = {k: v for k, v in r.skips.items() if v}
        if skips:
            print(f"  {r.label:<32} {skips}")
    print()
    print("分月净利：")
    for r in rows:
        print(f"  {r.label:<32} {r.months}")


def write_outputs(results: Iterable[VariantResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    items = list(results)
    (out_dir / "variants.json").write_text(
        json.dumps([r.to_dict() for r in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (out_dir / "variants.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["key", "label", "trades", "win_rate", "avg_entry_price",
                         "price_edge", "net_pnl", "roi", "t_stat", "max_drawdown",
                         "months_positive", "months_total", "skips"])
        for r in items:
            s = r.summary
            writer.writerow([
                r.key, r.label, s["trades"], s["win_rate"], s["avg_entry_price"],
                s["price_edge"], s["net_pnl"], s["roi"], s["t_stat"],
                s["max_drawdown"],
                sum(1 for v in r.months.values() if v > 0), len(r.months),
                json.dumps({k: v for k, v in r.skips.items() if v}, ensure_ascii=False),
            ])


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    settings = config.settings()
    parser = argparse.ArgumentParser(
        description="Late-window entry-opportunity experiments "
                    "(15m: Aug-Oct 2026; 5m: Jul-Sep 2026)"
    )
    parser.add_argument("--period", choices=["15m", "5m"], default="15m")
    parser.add_argument("--start", default=None,
                        help="默认 15m: 2026-08-01；5m: 2026-07-01")
    parser.add_argument("--end", default=None,
                        help="默认 15m: 2026-10-31；5m: 2026-09-30")
    parser.add_argument("--asset", default=settings.ASSET)
    parser.add_argument("--dataset-root", default=str(settings.dataset_root))
    parser.add_argument("--variants", default="",
                        help="逗号分隔的变体 key；留空 = 全部")
    parser.add_argument("--sweep", action="store_true",
                        help="改为运行 retry 家族的网格（阈值/窗口/确认/上限）")
    parser.add_argument("--stake", type=float, default=10.0)
    parser.add_argument("--slippage", type=float, default=0.005)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--min-trades", type=int, default=50)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    args.start = args.start or DEFAULT_START[args.period]
    args.end = args.end or DEFAULT_END[args.period]
    out_dir = (Path(args.out) if args.out
               else settings.research_dir / f"experiments_{args.period}")

    def progress(frac: float, message: str) -> None:
        print(f"\r{message:<60s} {frac * 100:5.1f}%", end="", flush=True)

    if args.sweep:
        print(f"数据: {args.dataset_root}  周期: {args.period}  "
              f"区间: {args.start} → {args.end}")
        ranked = run_sweep(args.dataset_root, args.asset, args.period, args.start, args.end,
                           stake=args.stake, slippage=args.slippage,
                           fee_rate=args.fee_rate, min_trades=args.min_trades,
                           progress=progress)
        print()
        print(f"网格 top {min(10, len(ranked))}（净利，至少 {args.min_trades} 笔）：")
        print_table(ranked[:10])
        write_outputs(ranked[:10], out_dir)
    else:
        catalogue = {key: (label, p) for key, label, p in catalogue_for(args.period)}
        if args.variants:
            wanted = [k.strip() for k in args.variants.split(",") if k.strip()]
            missing = [k for k in wanted if k not in catalogue]
            if missing:
                print(f"未知变体: {missing}")
                return 2
            catalogue = {k: catalogue[k] for k in wanted}
        print(f"数据: {args.dataset_root}  周期: {args.period}  "
              f"区间: {args.start} → {args.end}  变体: {len(catalogue)}")
        results = run_variants(args.dataset_root, args.asset, args.period,
                               args.start, args.end, catalogue,
                               stake=args.stake, slippage=args.slippage,
                               fee_rate=args.fee_rate, progress=progress)
        print()
        print_table(results.values())
        write_outputs(results.values(), out_dir)

    print(f"结果已写入 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
