"""Hold-to-resolution backtest for the late-window favorite rule.

This is a faithful port of ``strategy2_15m_late_favorite.py`` from the
research export, including its cost model:

    entry_fill = entry_price + slippage
    shares     = stake / entry_fill
    fee        = shares * fee_rate * entry_fill * (1 - entry_fill)
    pnl        = shares * payout - stake - fee        (payout 1.0 / 0.0)

and its definition of the signal (first crossing inside the entry window,
optional confirmation, price cap). The one thing it does not share with the
live engine is the tick loop -- `evaluate_window_array` is the same rule
evaluated over a sampled series, and `tests/test_strategy_parity.py` pins
the two implementations together.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

from src.infra.logging import get_logger
from src.research.dataset import horizon_seconds, list_days, load_series
from src.strategy.late_favorite import (
    StrategyParams,
    evaluate_window_array,
    taker_fee,
)

log = get_logger(__name__)

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


@dataclass
class BacktestParams:
    entry_window_sec: float = 60.0
    threshold: float = 0.90
    confirm_sec: float = 10.0
    max_entry_price: float = 0.94
    min_entry_price: float = 0.0
    stake: float = 10.0
    slippage: float = 0.005
    fee_rate: float = 0.07
    fee_model: str = "official"     # official | none

    def strategy_params(self) -> StrategyParams:
        return StrategyParams(
            entry_window_sec=self.entry_window_sec,
            threshold=self.threshold,
            confirm_sec=self.confirm_sec,
            max_entry_price=self.max_entry_price,
            min_entry_price=self.min_entry_price,
        )

    def to_dict(self) -> dict:
        return {
            "entry_window_sec": self.entry_window_sec,
            "threshold": self.threshold,
            "confirm_sec": self.confirm_sec,
            "max_entry_price": self.max_entry_price,
            "min_entry_price": self.min_entry_price,
            "stake": self.stake,
            "slippage": self.slippage,
            "fee_rate": self.fee_rate,
            "fee_model": self.fee_model,
        }

    @staticmethod
    def from_dict(data: dict) -> "BacktestParams":
        allowed = {f for f in BacktestParams.__dataclass_fields__}
        return BacktestParams(**{k: v for k, v in (data or {}).items() if k in allowed})


@dataclass
class BacktestResult:
    params: BacktestParams
    summary: dict
    trades: list[dict] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)
    tables: dict[str, list[dict]] = field(default_factory=dict)
    equity: list[dict] = field(default_factory=list)

    def to_dict(self, include_trades: bool = False) -> dict:
        out = {
            "params": self.params.to_dict(),
            "summary": self.summary,
            "skips": self.skips,
            "tables": self.tables,
            "equity": self.equity[-1000:],
        }
        if include_trades:
            out["trades"] = self.trades
        return out


def _utc_iso(ts: float | None) -> str:
    if ts is None or not math.isfinite(ts):
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_backtest(
    root,
    asset: str,
    period: str,
    start: Optional[str],
    end: Optional[str],
    params: BacktestParams,
    *,
    progress: Optional[ProgressFn] = None,
    should_cancel: Optional[CancelFn] = None,
    is_live: bool = False,
) -> BacktestResult:
    strategy_params = params.strategy_params()
    errors = strategy_params.validation_errors()
    if errors:
        raise ValueError("; ".join(errors))

    days = list_days(root, asset, period, start, end)
    if not days:
        raise ValueError(f"数据集为空: {root}/asset={asset.lower()}/period={period}")

    started = time.time()
    trades: list[dict] = []
    skips: dict[str, int] = {}
    markets_seen = 0
    markets = load_series(
        root, asset, period, start, end,
        horizon_seconds(params.entry_window_sec, params.confirm_sec),
        is_live=is_live,
    )

    for market in markets:
        if should_cancel is not None and should_cancel():
            raise RuntimeError("cancelled")
        markets_seen += 1
        if progress is not None and markets_seen % 200 == 0:
            progress(min(0.99, markets_seen / max(1, len(days) * 96)),
                     f"{market.date} · 已扫描 {markets_seen} 个市场")

        if market.end_ts is None:
            skips["no_meta"] = skips.get("no_meta", 0) + 1
            continue
        if not market.resolved:
            skips["unresolved"] = skips.get("unresolved", 0) + 1
            continue

        signal = evaluate_window_array(
            market.times.tolist(), market.up.tolist(), market.down.tolist(),
            float(market.end_ts), strategy_params,
        )
        if not signal.ok:
            skips[signal.reason] = skips.get(signal.reason, 0) + 1
            continue

        entry_fill = signal.entry_price + params.slippage
        if not math.isfinite(entry_fill) or entry_fill <= 0 or entry_fill >= 1:
            skips["bad_fill"] = skips.get("bad_fill", 0) + 1
            continue

        shares = params.stake / entry_fill
        won = market.resolved == signal.side.value
        payout = 1.0 if won else 0.0
        fee = taker_fee(shares, entry_fill, params.fee_rate) \
            if params.fee_model == "official" else 0.0
        gross = shares * payout
        pnl = gross - params.stake - fee

        trades.append({
            "slug": market.slug,
            "date": market.date,
            "month": market.date[:7],
            "side": signal.side.value,
            "start_ts": market.end_ts - (300 if period == "5m" else 900),
            "end_ts": market.end_ts,
            "entry_ts": signal.entry_ts,
            "entry_utc": _utc_iso(signal.entry_ts),
            "entry_price": signal.entry_price,
            "entry_fill": entry_fill,
            "first_cross_price": signal.first_cross_price,
            "first_cross_ts": signal.first_cross_ts,
            "confirm_delay_sec": signal.confirm_delay_sec,
            "confirm_price_delta": signal.confirm_price_delta,
            "time_remaining_sec": signal.time_remaining_sec,
            "resolved": market.resolved,
            "payout": payout,
            "shares": shares,
            "stake": params.stake,
            "gross_proceeds": gross,
            "fees": fee,
            "pnl": pnl,
            "return_pct": pnl / params.stake if params.stake else 0.0,
        })

    if progress is not None:
        progress(0.99, "汇总统计")

    summary = build_summary(
        trades, skips, params, elapsed=time.time() - started,
        days=[d for d, _ in days], markets_seen=markets_seen, period=period,
        root=str(root), asset=asset,
    )
    tables = build_tables(trades)
    equity = build_equity(trades)
    if progress is not None:
        progress(1.0, "完成")
    return BacktestResult(
        params=params, summary=summary, trades=trades, skips=skips,
        tables=tables, equity=equity,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "stake": 0.0, "gross_pnl": 0.0, "fees": 0.0,
            "net_pnl": 0.0, "roi": 0.0, "win_rate": 0.0, "avg_entry_price": 0.0,
            "price_edge": 0.0, "avg_pnl": 0.0, "max_drawdown": 0.0,
            "t_stat": None, "breakeven_win_rate": None, "profit_factor": None,
        }
    ordered = sorted(trades, key=lambda t: t.get("entry_ts") or 0)
    stake = sum(float(t["stake"]) for t in ordered)
    gross = sum(float(t["gross_proceeds"]) - float(t["stake"]) for t in ordered)
    fees = sum(float(t["fees"]) for t in ordered)
    pnls = [float(t["pnl"]) for t in ordered]
    net = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n
    avg_price = sum(float(t["entry_price"]) for t in ordered) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    mean = net / n
    if n > 1:
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(var)
        t_stat = mean / (std / math.sqrt(n)) if std > 0 else None
    else:
        t_stat = None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    gross_loss = abs(sum(losses))
    return {
        "trades": n,
        "stake": stake,
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "roi": net / stake if stake else 0.0,
        "win_rate": win_rate,
        "avg_entry_price": avg_price,
        "price_edge": win_rate - avg_price,
        "avg_pnl": mean,
        "max_drawdown": max_dd,
        "t_stat": t_stat,
        "breakeven_win_rate": (
            abs(avg_loss) / (avg_win + abs(avg_loss)) if (avg_win + abs(avg_loss)) > 0 else None
        ),
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
    }


def build_summary(trades: list[dict], skips: dict[str, int], params: BacktestParams,
                  *, elapsed: float, days: list[str], markets_seen: int,
                  period: str, root: str, asset: str = "BTC") -> dict:
    m = metrics(trades)
    daily = {}
    for trade in trades:
        daily[trade["date"]] = daily.get(trade["date"], 0.0) + float(trade["pnl"])
    win_days = (sum(1 for v in daily.values() if v > 0) / len(daily)) if daily else None
    months = {}
    for trade in trades:
        months[trade["month"]] = months.get(trade["month"], 0.0) + float(trade["pnl"])
    positive_months = sum(1 for v in months.values() if v > 0)
    losing_streak = 0
    max_losing_streak = 0
    for trade in sorted(trades, key=lambda t: t.get("entry_ts") or 0):
        if float(trade["pnl"]) < 0:
            losing_streak += 1
            max_losing_streak = max(max_losing_streak, losing_streak)
        else:
            losing_streak = 0
    tail = {}
    net = m["net_pnl"]
    if net:
        for k in (1, 5, 10):
            if len(trades) >= k:
                tail[f"top{k}_share"] = (
                    sum(sorted((float(t["pnl"]) for t in trades), reverse=True)[:k]) / net
                )
    return {
        **m,
        "asset": asset.upper(),
        "period": period,
        "dataset_root": root,
        "date_min": days[0] if days else None,
        "date_max": days[-1] if days else None,
        "dates_processed": len(days),
        "markets_scanned": markets_seen,
        "skip_counts": dict(sorted(skips.items(), key=lambda kv: -kv[1])),
        "win_days": win_days,
        "avg_trades_per_day": (len(trades) / len(daily)) if daily else None,
        "months": dict(sorted(months.items())),
        "positive_months": positive_months,
        "total_months": len(months),
        "max_losing_streak": max_losing_streak,
        "tail": tail,
        "elapsed_sec": round(elapsed, 2),
        "params": params.to_dict(),
        "generated_at": _utc_iso(time.time()),
    }


def _aggregate(trades: list[dict], key: str) -> list[dict]:
    if not trades:
        return []
    groups: dict[str, list[dict]] = {}
    for trade in trades:
        groups.setdefault(str(trade.get(key)), []).append(trade)
    rows = []
    for name, group in sorted(groups.items()):
        m = metrics(group)
        rows.append({
            key: name,
            "trades": m["trades"],
            "win_rate": m["win_rate"],
            "avg_entry_price": m["avg_entry_price"],
            "price_edge": m["price_edge"],
            "net_pnl": m["net_pnl"],
            "roi": m["roi"],
            "t_stat": m["t_stat"],
        })
    return rows


def _price_bucket(price: float) -> str:
    edges = [0.0, 0.90, 0.92, 0.94, 0.96, 0.98, 1.0]
    for low, high in zip(edges, edges[1:]):
        if low <= price < high:
            return f"{low:.2f}-{high:.2f}"
    return "other"


def _time_bucket(remaining: float) -> str:
    edges = [(0, 20, "0-20s"), (20, 40, "20-40s"), (40, 60, "40-60s"),
             (60, 120, "60-120s"), (120, 300, "120-300s"), (300, 600, "300s+")]
    for low, high, label in edges:
        if low <= remaining < high:
            return label
    return "600s+"


def build_tables(trades: list[dict]) -> dict[str, list[dict]]:
    enriched = []
    for trade in trades:
        item = dict(trade)
        item["price_bucket"] = _price_bucket(float(item["entry_price"]))
        item["time_bucket"] = _time_bucket(float(item["time_remaining_sec"]))
        enriched.append(item)
    return {
        "by_month": _aggregate(enriched, "month"),
        "by_side": _aggregate(enriched, "side"),
        "by_price_bucket": _aggregate(enriched, "price_bucket"),
        "by_time_bucket": _aggregate(enriched, "time_bucket"),
    }


def build_equity(trades: list[dict]) -> list[dict]:
    ordered = sorted(trades, key=lambda t: t.get("entry_ts") or 0)
    out = []
    cumulative = 0.0
    peak = 0.0
    for trade in ordered:
        cumulative += float(trade["pnl"])
        peak = max(peak, cumulative)
        out.append({
            "ts": trade.get("entry_ts"),
            "entry_utc": trade.get("entry_utc"),
            "pnl": float(trade["pnl"]),
            "cumulative": cumulative,
            "drawdown": cumulative - peak,
        })
    return out


# ---------------------------------------------------------------------------
# Scoring (shared by the optimiser and the agent)
# ---------------------------------------------------------------------------


def score(summary: dict, objective: str = "profit_dd", min_trades: int = 20) -> float:
    """Scalar objective. Hard-rejects candidates with too few trades."""
    trades = int(summary.get("trades") or 0)
    if trades < min_trades:
        return float("-inf")
    net = float(summary.get("net_pnl") or 0.0)
    roi = float(summary.get("roi") or 0.0)
    dd = abs(float(summary.get("max_drawdown") or 0.0))
    t_stat = summary.get("t_stat")
    if objective == "pnl":
        return net
    if objective == "roi":
        return roi
    if objective == "tstat":
        return float(t_stat) if t_stat is not None else float("-inf")
    # profit_dd: net profit with an explicit penalty for the drawdown it took.
    return net - 0.5 * dd
