#!/usr/bin/env python
"""Backtest ETH 15m with the live BTC 15m strategy parameters.

The late-window rule is asset-agnostic: in the last ``entry_window_sec``
seconds of each 15-minute market, the first time either side trades at a
midpoint >= ``threshold``, wait ``confirm_sec``; if that side is still above
the threshold and below ``max_entry_price``, buy it and hold to settlement.

This script replays exactly that rule on the ETH dataset using the **current
15m runtime parameters** (the frozen BTC configuration). It can also run BTC
over the same date range with the same parameters as a baseline, which is
what makes the cross-asset comparison meaningful.

USAGE
-----
    python scripts/backtest_eth15m.py
    python scripts/backtest_eth15m.py --start 2026-08-01 --end 2026-10-31
    python scripts/backtest_eth15m.py --slippage 0.01
    python scripts/backtest_eth15m.py --no-compare

Outputs (default ``data/research/eth_15m_btc_params``):
    report.md, summary_eth.json, trades_eth.json
    summary_btc.json, trades_btc.json      (when comparing against BTC)

The report is a research artifact, not investment advice: the same slippage
and fee assumptions are applied to both assets, but ETH's liquidity and
volatility differ from BTC's.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.research.backtest import (  # noqa: E402
    BacktestParams,
    BacktestResult,
    metrics,
    run_backtest,
)

PERIOD = "15m"


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def _live_15m_params(args, settings) -> BacktestParams:
    """The live 15m rule, with optional CLI overrides for sensitivity tests."""
    strategy = settings.for_period(PERIOD)
    try:
        # Same precedence as the bot/CLI: dashboard runtime overrides win.
        from src.persistence.db import DB
        from src.web import runtime_config

        if settings.db_path.exists():
            db = DB(settings.db_path)
            try:
                runtime_config.apply_overrides(strategy, db, PERIOD)
            finally:
                db.close()
    except Exception:  # noqa: BLE001 - the script must work without a database
        pass

    pick = lambda value, fallback: fallback if value is None else value  # noqa: E731
    params = BacktestParams(
        entry_window_sec=pick(args.window, strategy.ENTRY_WINDOW_SEC),
        threshold=pick(args.threshold, strategy.ENTRY_THRESHOLD),
        confirm_sec=pick(args.confirm, strategy.CONFIRM_SEC),
        max_entry_price=pick(args.max_price, strategy.MAX_ENTRY_PRICE),
        min_entry_price=strategy.MIN_ENTRY_PRICE,
        stake=pick(args.stake, strategy.STAKE_USD),
        slippage=args.slippage,
        fee_rate=pick(args.fee_rate, strategy.TAKER_FEE_RATE),
    )
    return params


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _progress_callback():
    last = -1

    def callback(fraction: float, message: str) -> None:
        nonlocal last
        percent = int(fraction * 100)
        # One line per 10%: enough to show liveness without flooding CI logs.
        if percent // 10 != last // 10:
            last = percent
            print(f"   {message:<52s} {percent:3d}%")

    return callback


def _run(asset: str, args, params: BacktestParams) -> BacktestResult:
    print(f"\n扫描 {asset.upper()} {PERIOD} ...")
    result = run_backtest(
        args.dataset_root, asset, PERIOD, args.start, args.end, params,
        progress=_progress_callback(),
    )
    print()
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _t_text(summary: dict) -> str:
    value = summary.get("t_stat")
    return f"{value:.2f}" if value is not None else "n/a"


def _param_line(params: BacktestParams) -> str:
    return (
        f"窗口 {params.entry_window_sec:g}s / 阈值 {params.threshold:.2f} / "
        f"确认 {params.confirm_sec:g}s / 上限 {params.max_entry_price:.2f} / "
        f"单笔 {params.stake:g}U / 滑点 {params.slippage:g} / 费率 {params.fee_rate:g}"
    )


def _monthly_rows(result: BacktestResult) -> list[tuple[str, int, float, float, float]]:
    groups: dict[str, list[dict]] = {}
    for trade in result.trades:
        groups.setdefault(str(trade["month"]), []).append(trade)
    rows = []
    for month, trades in sorted(groups.items()):
        m = metrics(trades)
        rows.append((month, m["trades"], m["win_rate"], m["net_pnl"], m["roi"]))
    return rows


def _print_summary(label: str, result: BacktestResult) -> None:
    s = result.summary
    print("=" * 76)
    print(f"{label} · {s.get('date_min')} → {s.get('date_max')}")
    print(f"  样本: {s.get('markets_scanned', 0):,} 个市场 · {s.get('trades', 0):,} 笔交易")
    print(f"  胜率: {s.get('win_rate', 0) * 100:.2f}%  ·  "
          f"平均入场: {s.get('avg_entry_price', 0):.4f}  ·  "
          f"价差: {s.get('price_edge', 0) * 100:+.2f}pp")
    print(f"  净收益: {s.get('net_pnl', 0):+.2f} U  ·  ROI: {s.get('roi', 0) * 100:+.2f}%  ·  "
          f"t: {_t_text(s)}  ·  最大回撤: {s.get('max_drawdown', 0):+.2f} U")
    print(f"  正收益月份: {s.get('positive_months', 0)}/{s.get('total_months', 0)}  ·  "
          f"平均: {(s.get('avg_trades_per_day') or 0):.2f} 笔/天")
    skips = s.get("skip_counts") or {}
    if skips:
        print("  跳过: " + "  ".join(f"{k}={v:,}" for k, v in skips.items()))

    rows = _monthly_rows(result)
    if rows:
        print("\n  月份      笔数   胜率      净利       ROI")
        for month, trades, win_rate, net, roi in rows:
            print(f"  {month}  {trades:5d}   {win_rate * 100:5.2f}%   "
                  f"{net:+8.2f} U  {roi * 100:+6.2f}%")


def _comparison_table(eth: BacktestResult, btc: BacktestResult | None) -> str:
    e = eth.summary
    rows = [
        ("交易笔数", f"{e.get('trades', 0):,}", f"{btc.summary.get('trades', 0):,}" if btc else ""),
        ("胜率", f"{e.get('win_rate', 0) * 100:.2f}%",
         f"{btc.summary.get('win_rate', 0) * 100:.2f}%" if btc else ""),
        ("平均入场", f"{e.get('avg_entry_price', 0):.4f}",
         f"{btc.summary.get('avg_entry_price', 0):.4f}" if btc else ""),
        ("价差", f"{e.get('price_edge', 0) * 100:+.2f}pp",
         f"{btc.summary.get('price_edge', 0) * 100:+.2f}pp" if btc else ""),
        ("净收益", f"{e.get('net_pnl', 0):+.2f} U",
         f"{btc.summary.get('net_pnl', 0):+.2f} U" if btc else ""),
        ("ROI", f"{e.get('roi', 0) * 100:+.2f}%",
         f"{btc.summary.get('roi', 0) * 100:+.2f}%" if btc else ""),
        ("t 值", _t_text(e), _t_text(btc.summary) if btc else ""),
        ("最大回撤", f"{e.get('max_drawdown', 0):+.2f} U",
         f"{btc.summary.get('max_drawdown', 0):+.2f} U" if btc else ""),
        ("正收益月份", f"{e.get('positive_months', 0)}/{e.get('total_months', 0)}",
         f"{btc.summary.get('positive_months', 0)}/{btc.summary.get('total_months', 0)}"
         if btc else ""),
    ]
    if btc is None:
        return "\n".join(f"- {name}: {eth_value}" for name, eth_value, _ in rows)
    lines = ["| 指标 | ETH | BTC（同参数） |", "|---|---:|---:|"]
    lines.extend(f"| {name} | {eth_value} | {btc_value} |" for name, eth_value, btc_value in rows)
    return "\n".join(lines)


def _write_report(path: Path, params: BacktestParams, eth: BacktestResult,
                  btc: BacktestResult | None, args) -> None:
    e = eth.summary
    lines = [
        "# ETH 15m 回测（使用 BTC 15m 参数）",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数据目录: `{args.dataset_root}`",
        f"- 日期范围: `{e.get('date_min')}` → `{e.get('date_max')}`",
        f"- 参数: {_param_line(params)}",
        "- 价格: 数据集 1 秒/5 秒 mid；成交价 = 信号价 + 滑点；持有到结算",
        "",
        "## ETH 15m",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 市场数 | {e.get('markets_scanned', 0):,} |",
        f"| 交易笔数 | {e.get('trades', 0):,} |",
        f"| 胜率 | {e.get('win_rate', 0) * 100:.2f}% |",
        f"| 平均入场 | {e.get('avg_entry_price', 0):.4f} |",
        f"| 价差 | {e.get('price_edge', 0) * 100:+.2f}pp |",
        f"| 净收益 | {e.get('net_pnl', 0):+.2f} U |",
        f"| ROI | {e.get('roi', 0) * 100:+.2f}% |",
        f"| t 值 | {_t_text(e)} |",
        f"| 最大回撤 | {e.get('max_drawdown', 0):+.2f} U |",
        f"| 正收益月份 | {e.get('positive_months', 0)}/{e.get('total_months', 0)} |",
        f"| 平均笔数/天 | {(e.get('avg_trades_per_day') or 0):.2f} |",
        "",
        "### 每月",
        "",
        "| 月份 | 笔数 | 胜率 | 净利 | ROI |",
        "|---|---:|---:|---:|---:|",
    ]
    for month, trades, win_rate, net, roi in _monthly_rows(eth):
        lines.append(f"| {month} | {trades} | {win_rate * 100:.2f}% | {net:+.2f} U | {roi * 100:+.2f}% |")
    lines += ["", "### 跳过原因", ""]
    for reason, count in (e.get("skip_counts") or {}).items():
        lines.append(f"- `{reason}`: {count:,}")
    lines += [
        "",
        "## 与 BTC 对比（同一参数、同一区间）",
        "",
        _comparison_table(eth, btc),
        "",
        "## 说明",
        "",
        "- 这是**同一规则搬到 ETH** 的跨资产检验，不是 ETH 专用调参结果。",
        "- 两个资产使用相同的滑点与费率假设；ETH 的实际盘口深度和波动结构与 BTC 不同。",
        "- `price_too_high` 表示触发价已达到/超过 `max_entry_price`，策略按规则放弃；",
        "  这也是 BTC 15m 最主要的跳过原因。",
        "- 样本量较小时（< 30 笔）t 值和月胜率的参考意义有限。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass

    settings = config.build()
    parser = argparse.ArgumentParser(
        description="用 BTC 15m 的当前参数回测 ETH 15m（并可对比 BTC）"
    )
    parser.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD（默认全部）")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认全部）")
    parser.add_argument("--dataset-root", default=str(settings.dataset_root))
    parser.add_argument("--output-dir", default=None,
                        help="默认 data/research/eth_15m_btc_params")
    parser.add_argument("--window", type=float, default=None, help="入场窗口秒数")
    parser.add_argument("--threshold", type=float, default=None, help="触发阈值")
    parser.add_argument("--confirm", type=float, default=None, help="确认秒数")
    parser.add_argument("--max-price", type=float, default=None, help="入场价上限")
    parser.add_argument("--stake", type=float, default=None, help="单笔金额 USDC")
    parser.add_argument("--slippage", type=float, default=0.005, help="滑点（默认 0.005）")
    parser.add_argument("--fee-rate", type=float, default=None, help="费率（默认取 live 15m）")
    parser.add_argument("--compare-asset", default="BTC", help="对比资产，默认 BTC")
    parser.add_argument("--no-compare", action="store_true", help="不跑 BTC 基线")
    args = parser.parse_args(argv)

    params = _live_15m_params(args, settings)
    errors = params.strategy_params().validation_errors()
    if errors:
        print("参数不合法: " + "; ".join(errors), file=sys.stderr)
        return 2

    print("=" * 76)
    print("ETH 15m 回测 · 使用 BTC 15m 当前参数")
    print(f"参数: {_param_line(params)}")
    print(f"数据: {args.dataset_root}")
    print(f"区间: {args.start or '全部'} → {args.end or '全部'}")

    started = time.time()
    eth = _run("ETH", args, params)
    btc = None
    if not args.no_compare and args.compare_asset:
        btc = _run(args.compare_asset, args, params)

    _print_summary("ETH 15m", eth)
    if btc is not None:
        _print_summary(f"{args.compare_asset.upper()} 15m（同参数）", btc)

    out = Path(args.output_dir) if args.output_dir else settings.research_dir / "eth_15m_btc_params"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary_eth.json").write_text(
        json.dumps(eth.summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out / "trades_eth.json").write_text(
        json.dumps(eth.trades, ensure_ascii=False, default=str), encoding="utf-8"
    )
    if btc is not None:
        (out / "summary_btc.json").write_text(
            json.dumps(btc.summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        (out / "trades_btc.json").write_text(
            json.dumps(btc.trades, ensure_ascii=False, default=str), encoding="utf-8"
        )
    _write_report(out / "report.md", params, eth, btc, args)

    print(f"\n结果已写入 {out}")
    print(f"耗时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
