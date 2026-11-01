"""Offline research CLI.

    python -m src.research.cli dataset
    python -m src.research.cli backtest --start 2026-08-01 --end 2026-10-31
    python -m src.research.cli optimize --iterations 40 --objective profit_dd
    python -m src.research.cli agent --goal "在控制回撤的前提下最大化净收益"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from src import config
from src.research.backtest import BacktestParams, run_backtest
from src.research.dataset import dataset_summary, live_dataset_summary
from src.research.optimize import format_report, optimize


def _strategy_settings(settings, period: str):
    """The period's live settings, including saved runtime overrides."""
    strategy = settings.for_period(period)
    try:
        from src.persistence.db import DB
        from src.web import runtime_config

        if settings.db_path.exists():
            db = DB(settings.db_path)
            try:
                runtime_config.apply_overrides(strategy, db, period)
            finally:
                db.close()
    except Exception:  # noqa: BLE001 - the CLI must work without a database
        pass
    return strategy


def _params(args, settings) -> BacktestParams:
    """Explicit flags win; otherwise start from the live parameters for the
    requested period (so `--period 5m` uses the 5m defaults)."""
    strategy = _strategy_settings(settings, args.period)
    pick = lambda value, fallback: fallback if value is None else value  # noqa: E731
    return BacktestParams(
        entry_window_sec=pick(args.entry_window_sec, strategy.ENTRY_WINDOW_SEC),
        threshold=pick(args.threshold, strategy.ENTRY_THRESHOLD),
        confirm_sec=pick(args.confirm_sec, strategy.CONFIRM_SEC),
        max_entry_price=pick(args.max_entry_price, strategy.MAX_ENTRY_PRICE),
        min_entry_price=pick(args.min_entry_price, strategy.MIN_ENTRY_PRICE),
        stake=pick(args.stake, strategy.STAKE_USD),
        slippage=pick(args.slippage, 0.005),
        fee_rate=pick(args.fee_rate, strategy.TAKER_FEE_RATE),
    )


def _print_summary(summary: dict) -> None:
    print("=" * 72)
    print(f"市场        : {summary.get('markets_scanned')}  交易: {summary.get('trades')}")
    print(f"日期        : {summary.get('date_min')} → {summary.get('date_max')}")
    print(f"胜率        : {(summary.get('win_rate') or 0) * 100:.2f}%")
    print(f"平均入场    : {(summary.get('avg_entry_price') or 0):.4f}")
    print(f"价差        : {(summary.get('price_edge') or 0) * 100:+.2f}pp")
    print(f"净收益      : {(summary.get('net_pnl') or 0):+.2f} U")
    print(f"ROI         : {(summary.get('roi') or 0) * 100:+.2f}%")
    print(f"t 值        : {summary.get('t_stat')}")
    print(f"最大回撤    : {(summary.get('max_drawdown') or 0):+.2f} U")
    print(f"正收益月份  : {summary.get('positive_months')}/{summary.get('total_months')}")
    if summary.get("skip_counts"):
        print(f"跳过        : {summary['skip_counts']}")


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        # The default cp936 console mangles the Chinese report.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    settings = config.settings()
    parser = argparse.ArgumentParser(description="PolyTail BTC15 research tools")
    parser.add_argument("--asset", default=settings.ASSET)
    parser.add_argument("--period", default=settings.PERIOD, choices=["15m", "5m"])
    parser.add_argument("--dataset-root", default=str(settings.dataset_root))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("dataset", help="show available data")

    def add_common(p):
        # Allow --period both before and after the subcommand. SUPPRESS keeps
        # the global value when the flag is omitted here.
        p.add_argument("--period", choices=["15m", "5m"], default=argparse.SUPPRESS)
        p.add_argument("--start", default=None)
        p.add_argument("--end", default=None)
        # None = use the period's current live parameters.
        p.add_argument("--entry-window-sec", type=float, default=None)
        p.add_argument("--threshold", type=float, default=None)
        p.add_argument("--confirm-sec", type=float, default=None)
        p.add_argument("--max-entry-price", type=float, default=None)
        p.add_argument("--min-entry-price", type=float, default=None)
        p.add_argument("--stake", type=float, default=None)
        p.add_argument("--slippage", type=float, default=0.005)
        p.add_argument("--fee-rate", type=float, default=None)
        p.add_argument("--output-dir", default=None)

    bt = sub.add_parser("backtest", help="run one parameter set")
    add_common(bt)

    opt = sub.add_parser("optimize", help="deterministic out-of-sample search")
    add_common(opt)
    opt.add_argument("--objective", default="profit_dd",
                     choices=["profit_dd", "pnl", "roi", "tstat"])
    opt.add_argument("--iterations", type=int, default=40)
    opt.add_argument("--min-trades", type=int, default=20)
    opt.add_argument("--train-end", default=None)
    opt.add_argument("--validate-start", default=None)
    opt.add_argument("--validate-end", default=None)

    ag = sub.add_parser("agent", help="LLM parameter-tuning agent")
    ag.add_argument("--period", choices=["15m", "5m"], default=argparse.SUPPRESS)
    ag.add_argument("--goal", default="在控制回撤的前提下最大化净收益")
    ag.add_argument("--start", default=None)
    ag.add_argument("--end", default=None)
    ag.add_argument("--iterations", type=int, default=6)
    ag.add_argument("--model", default=None)

    val = sub.add_parser(
        "validate",
        help="frozen-rule out-of-sample check using the CURRENT runtime params",
    )
    val.add_argument("--period", choices=["15m", "5m"], default=argparse.SUPPRESS)
    val.add_argument("--start", required=True, help="新数据开始日期 YYYY-MM-DD")
    val.add_argument("--end", required=True, help="新数据结束日期 YYYY-MM-DD")
    val.add_argument("--slippage", type=float, default=0.005)
    val.add_argument("--min-trades", type=int, default=30)

    args = parser.parse_args(argv)

    if args.command == "dataset":
        print(json.dumps({
            "history": dataset_summary(args.dataset_root, args.asset, args.period),
            "live": live_dataset_summary(settings.collect_dir, args.asset, args.period),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "backtest":
        params = _params(args, settings)
        result = run_backtest(args.dataset_root, args.asset, args.period,
                              args.start, args.end, params)
        _print_summary(result.summary)
        out = Path(args.output_dir) if args.output_dir else settings.research_dir / "cli_backtest"
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(
            json.dumps(result.summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (out / "trades.json").write_text(
            json.dumps(result.trades, ensure_ascii=False, default=str), encoding="utf-8"
        )
        print(f"结果已写入 {out}")
        return 0

    if args.command == "optimize":
        params = _params(args, settings)
        result = optimize(
            args.dataset_root, args.asset, args.period, params,
            start_date=args.start, end_date=args.end,
            objective=args.objective, iterations=args.iterations,
            min_trades=args.min_trades, train_end=args.train_end,
            validate_end=args.validate_end,
            progress=lambda f, m: print(f"\r{m:<60s} {f*100:5.1f}%", end="", flush=True),
        )
        print()
        print(format_report(result))
        out = Path(args.output_dir) if args.output_dir else settings.research_dir / "cli_optimize"
        out.mkdir(parents=True, exist_ok=True)
        (out / "optimize.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"结果已写入 {out}")
        return 0

    if args.command == "validate":
        # The strategy doc's step 1: freeze the rule, then test it on data the
        # rule was never chosen on. No parameter search happens here.
        strategy = _strategy_settings(settings, args.period)
        params = BacktestParams(
            entry_window_sec=strategy.ENTRY_WINDOW_SEC,
            threshold=strategy.ENTRY_THRESHOLD,
            confirm_sec=strategy.CONFIRM_SEC,
            max_entry_price=strategy.MAX_ENTRY_PRICE,
            min_entry_price=strategy.MIN_ENTRY_PRICE,
            stake=strategy.STAKE_USD,
            slippage=args.slippage,
            fee_rate=strategy.TAKER_FEE_RATE,
        )
        result = run_backtest(args.dataset_root, args.asset, args.period,
                              args.start, args.end, params)
        _print_summary(result.summary)
        summary = result.summary
        checks = [
            ("样本量", summary.get("trades", 0) >= args.min_trades,
             f"{summary.get('trades', 0)} 笔（要求 ≥ {args.min_trades}）"),
            ("净收益为正", summary.get("net_pnl", 0) > 0,
             f"{summary.get('net_pnl', 0):+.2f} U"),
            ("t 值 ≥ 1.5", (summary.get("t_stat") or 0) >= 1.5,
             f"{summary.get('t_stat')}"),
            ("价差为正", (summary.get("price_edge") or 0) > 0,
             f"{(summary.get('price_edge') or 0) * 100:+.2f}pp"),
        ]
        print("\n样本外检验（冻结参数，不做任何调参）：")
        failed = 0
        for name, ok, detail in checks:
            print(f"  [{'通过' if ok else '未通过'}] {name}: {detail}")
            failed += 0 if ok else 1
        if failed == 0:
            print("\n结论：冻结规则在新数据上成立，可以继续按当前参数运行。")
        else:
            print("\n结论：冻结规则在新数据上未全部通过。按文档建议，"
                  "此时不应继续调参，而应缩小仓位或暂停并复核假设。")
        return 0 if failed == 0 else 1

    if args.command == "agent":
        from src.research.agent import TuningAgent

        strategy = _strategy_settings(settings, args.period)
        result = asyncio.run(TuningAgent(
            strategy, progress=lambda m: print(f"[agent] {m}")
        ).run(args.goal, args.start, args.end, args.iterations, args.model))
        if not result.ok:
            print(f"Agent 失败: {result.error}", file=sys.stderr)
            return 1
        print(result.report)
        out = settings.research_dir / "agent"
        out.mkdir(parents=True, exist_ok=True)
        stamp = int(result.proposal.get("at", 0))
        (out / f"proposal_{stamp}.json").write_text(
            json.dumps(result.proposal, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / f"report_{stamp}.md").write_text(result.report, encoding="utf-8")
        print(f"报告已写入 {out}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
