"""Deterministic parameter search with an out-of-sample split.

The docs are explicit that the August-October window was chosen in
hindsight and that a lot of threshold combinations were tested, so this
module refuses to declare a winner on in-sample numbers alone:

  * candidates are scored on a TRAIN period
  * only the top `keep` are evaluated on a held-out VALIDATE period
  * the recommendation must be positive out-of-sample as well

The agent (research/agent.py) can call this as one tool and then do its own
adaptive search on top.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.infra.logging import get_logger
from src.research.backtest import (
    BacktestParams,
    BacktestResult,
    metrics,
    run_backtest,
    score,
)
from src.research.dataset import list_days

log = get_logger(__name__)

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


@dataclass
class Bounds:
    low: float
    high: float
    step: float
    kind: str = "float"

    def sample(self, rng: random.Random) -> float:
        if self.kind == "int":
            values = list(range(int(self.low), int(self.high) + 1,
                                max(1, int(self.step))))
            return float(rng.choice(values))
        steps = int(round((self.high - self.low) / self.step))
        return round(self.low + rng.randint(0, max(0, steps)) * self.step, 6)


DEFAULT_SPACE: dict[str, Bounds] = {
    "entry_window_sec": Bounds(30, 120, 5, "int"),
    "threshold": Bounds(0.86, 0.95, 0.005),
    "confirm_sec": Bounds(0, 20, 1, "int"),
    "max_entry_price": Bounds(0.90, 0.97, 0.005),
}

# The 5m window is only 300 s long, so the search space is much earlier in
# the window and confirmation cannot afford a long wait. The doc's only
# profitable 5m pocket is the last 10-20 s with a 0.92 cap, so the grid
# centres on that region.
SPACE_5M: dict[str, Bounds] = {
    # Live observation: 5m markets often jump from <0.90 straight to
    # 0.985-0.9995 inside the last 30 seconds, so a 0.90-threshold rule only
    # sees entries above the 0.92 cap and skips nearly every window. The
    # search therefore has to look earlier and lower: trigger at 0.80-0.94,
    # over windows up to 90 s, with confirmation optionally in play.
    "entry_window_sec": Bounds(10, 90, 5, "int"),
    "threshold": Bounds(0.80, 0.94, 0.005),
    "confirm_sec": Bounds(0, 15, 1, "int"),
    "max_entry_price": Bounds(0.85, 0.96, 0.005),
}


def default_space(period: str) -> dict[str, Bounds]:
    return SPACE_5M if period == "5m" else DEFAULT_SPACE

PARAM_LABELS = {
    "entry_window_sec": "入场窗口(秒)",
    "threshold": "触发阈值",
    "confirm_sec": "确认秒数",
    "max_entry_price": "入场价上限",
    "min_entry_price": "入场价下限",
    "slippage": "滑点",
    "stake": "单笔金额",
}


@dataclass
class Candidate:
    params: BacktestParams
    train: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    train_score: float = float("-inf")
    validation_score: float = float("-inf")
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "params": self.params.to_dict(),
            "train": self.train,
            "validation": self.validation,
            "train_score": self.train_score,
            "validation_score": self.validation_score,
            "label": self.label,
        }


@dataclass
class OptimizeResult:
    objective: str
    space: dict[str, dict]
    train_range: dict
    validate_range: dict
    baseline: Optional[dict]
    leaderboard: list[dict]
    best: Optional[dict]
    elapsed_sec: float
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "objective": self.objective,
            "space": self.space,
            "train_range": self.train_range,
            "validate_range": self.validate_range,
            "baseline": self.baseline,
            "leaderboard": self.leaderboard,
            "best": self.best,
            "elapsed_sec": self.elapsed_sec,
            "note": self.note,
        }


def _space_size(space: dict[str, Bounds]) -> int:
    """Number of distinct parameter sets the search space can produce."""
    total = 1
    for bounds in space.values():
        if bounds.kind == "int":
            total *= len(range(int(bounds.low), int(bounds.high) + 1,
                               max(1, int(bounds.step))))
        else:
            total *= int(round((bounds.high - bounds.low) / bounds.step)) + 1
    return max(1, total)


def _shift_day(date: str, days: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _next_day(date: str) -> str:
    return _shift_day(date, 1)


def _prev_day(date: str) -> str:
    return _shift_day(date, -1)


def _derive_ranges(root, asset: str, period: str,
                   train_end: Optional[str], validate_start: Optional[str],
                   validate_end: Optional[str], start_date: Optional[str],
                   end_date: Optional[str]) -> tuple[tuple[str, str], tuple[str, str]]:
    days = [d for d, _ in list_days(root, asset, period, start_date, end_date)]
    if not days:
        raise ValueError("数据集为空")
    first, last = days[0], days[-1]

    if train_end or validate_start or validate_end:
        if train_end and validate_start:
            train_stop, validate_begin = train_end, validate_start
        elif train_end:
            train_stop, validate_begin = train_end, _next_day(train_end)
        elif validate_start:
            train_stop, validate_begin = _prev_day(validate_start), validate_start
        else:
            # Only the validation end was given: keep the default 70/30
            # split and shorten the validation leg.
            split_idx = max(1, int(len(days) * 0.7))
            split_idx = min(split_idx, len(days) - 1) if len(days) > 1 else len(days)
            train_stop = days[split_idx - 1]
            validate_begin = days[split_idx] if split_idx < len(days) else days[-1]

        validate_stop = validate_end or last
        if train_stop >= validate_begin:
            raise ValueError(
                f"训练集结束 {train_stop} 必须早于验证集开始 {validate_begin}"
            )
        if validate_stop < validate_begin:
            raise ValueError(
                f"验证集结束 {validate_stop} 早于验证集开始 {validate_begin}"
            )
        if train_stop < first or validate_stop > last:
            raise ValueError(
                f"日期超出数据范围 {first} → {last}"
            )
        return (first, train_stop), (validate_begin, validate_stop)

    # Default: chronological 70/30 split.
    split_idx = max(1, int(len(days) * 0.7))
    split_idx = min(split_idx, len(days) - 1) if len(days) > 1 else len(days)
    train_end_day = days[split_idx - 1]
    validate_start_day = days[split_idx] if split_idx < len(days) else days[-1]
    return (first, train_end_day), (validate_start_day, last)


def _params_with(base: BacktestParams, values: dict) -> BacktestParams:
    merged = base.to_dict()
    merged.update(values)
    return BacktestParams.from_dict(merged)


def optimize(
    root,
    asset: str,
    period: str,
    base: BacktestParams,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    train_end: Optional[str] = None,
    validate_start: Optional[str] = None,
    validate_end: Optional[str] = None,
    objective: str = "profit_dd",
    iterations: int = 24,
    keep: int = 4,
    min_trades: int = 20,
    keys: Optional[list[str]] = None,
    seed: int = 7,
    progress: Optional[ProgressFn] = None,
    should_cancel: Optional[CancelFn] = None,
    is_live: bool = False,
) -> OptimizeResult:
    started = time.time()
    # Hard ceiling: a caller asking for a million candidates would run for
    # hours and looks identical to a hang.
    iterations = max(1, min(int(iterations), 500))
    train_range, validate_range = _derive_ranges(
        root, asset, period, train_end, validate_start, validate_end,
        start_date, end_date,
    )
    space = {k: v for k, v in default_space(period).items()
             if keys is None or k in keys}
    rng = random.Random(seed)

    def _progress(frac: float, message: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, frac)), message)

    def _check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise RuntimeError("cancelled")

    def _run(params: BacktestParams, date_range: tuple[str, str]) -> dict:
        result = run_backtest(
            root, asset, period, date_range[0], date_range[1], params,
            should_cancel=should_cancel, is_live=is_live,
        )
        return result.summary

    # Baseline (the documented recommendation) for comparison.
    _progress(0.02, "回测基线参数")
    baseline_summary = _run(base, train_range)
    baseline = {
        "params": base.to_dict(),
        "train": baseline_summary,
        "train_score": score(baseline_summary, objective, min_trades),
        "label": "baseline",
    }

    # Candidate generation: baseline neighbourhood + random draws.
    candidates: list[dict] = []
    seen: set[tuple] = set()

    base_values = base.to_dict()

    def _add(values: dict, label: str) -> None:
        # A cap at or below the threshold can never fill (entry requires
        # threshold <= p < cap). With a wide grid this would burn a large
        # share of the random draws, so reject those combinations up front.
        merged = {**base_values, **values}
        if float(merged.get("max_entry_price", 1.0)) <= float(merged.get("threshold", 0.0)):
            return
        key = tuple(sorted((k, round(float(v), 6)) for k, v in values.items()))
        if key in seen:
            return
        seen.add(key)
        candidates.append({"values": values, "label": label})

    _add({}, "baseline")
    for name, bounds in space.items():
        for direction in (-1, 1):
            _add({name: _clamp(base.__dict__[name] + direction * bounds.step, bounds)},
                 f"{name}{'+' if direction > 0 else '-'}")

    # The space is finite. Asking for more candidates than it contains used
    # to spin forever (and no cancellation check ran in this loop), so cap
    # the target at the universe size and bound the number of draws.
    universe = 1 + 2 * len(space) + _space_size(space)
    target = max(1, min(int(iterations), universe))
    attempts = 0
    while len(candidates) < target and attempts < target * 20 + 100:
        attempts += 1
        _check_cancel()
        _add({name: bounds.sample(rng) for name, bounds in space.items()}, "random")
    if len(candidates) < target:
        log.info("optimize.space_exhausted", requested=iterations,
                 unique=len(candidates))

    evaluated: list[Candidate] = []
    total = len(candidates)
    for index, candidate in enumerate(candidates):
        _check_cancel()
        params = _params_with(base, candidate["values"])
        try:
            summary = _run(params, train_range)
        except ValueError as exc:
            log.debug("optimize.candidate_invalid", values=candidate["values"], error=str(exc))
            continue
        cand = Candidate(
            params=params,
            train=summary,
            train_score=score(summary, objective, min_trades),
            label=candidate["label"],
        )
        evaluated.append(cand)
        _progress(0.05 + 0.65 * (index + 1) / total,
                  f"训练集 {index + 1}/{total} · {candidate['label']}")

    evaluated.sort(key=lambda c: c.train_score, reverse=True)
    top = [c for c in evaluated if c.train_score > float("-inf")][:keep]
    baseline_candidate = next((c for c in evaluated if c.label == "baseline"), None)

    # The documented baseline is always validated, so the comparison the
    # recommendation rests on is visible even when it does not make the top K.
    validate_list = list(top)
    if baseline_candidate is not None and baseline_candidate not in validate_list:
        validate_list.append(baseline_candidate)

    for index, cand in enumerate(validate_list):
        _check_cancel()
        cand.validation = _run(cand.params, validate_range)
        cand.validation_score = score(cand.validation, objective, min_trades)
        _progress(0.72 + 0.25 * (index + 1) / max(1, len(validate_list)),
                  f"验证集 {index + 1}/{len(validate_list)}")

    # Prefer positive out-of-sample; break ties on the validation score. A
    # validation leg with a handful of trades is not evidence of an edge.
    min_validation_trades = max(5, min_trades // 4)

    def _rank(cand: Candidate) -> tuple:
        oos_trades = int(cand.validation.get("trades") or 0)
        oos_positive = (
            cand.validation.get("net_pnl", 0) > 0
            and oos_trades >= min_validation_trades
        )
        return (1 if oos_positive else 0, cand.validation_score, cand.train_score)

    validate_list.sort(key=_rank, reverse=True)
    best = validate_list[0] if validate_list else None
    note = ""
    if best is not None:
        if best.validation.get("net_pnl", 0) <= 0:
            note = ("候选在验证集上不赚钱：说明该参数空间在当前数据上已经饱和或过拟合，"
                    "应维持冻结参数，或先补更多样本外数据。")
        elif int(best.validation.get("trades") or 0) < min_validation_trades:
            note = ("验证集交易数过少，结论不具统计意义；建议延长验证区间后再判断。")
        elif best.label in ("threshold-", "threshold+", "max_entry_price-",
                            "max_entry_price+", "entry_window_sec-", "entry_window_sec+",
                            "confirm_sec-", "confirm_sec+"):
            note = ("最佳候选只是基线参数的一个最小步长偏移；这种量级的差异通常落在噪声内，"
                    "建议维持冻结参数并继续观察样本外表现。")
    _progress(1.0, "完成")

    # De-duplicate the leaderboard: validated candidates first, then the best
    # remaining train-only ones, each parameter set at most once.
    seen: set[str] = set()
    leaderboard: list[dict] = []
    for cand in validate_list + evaluated[: max(0, 10 - len(validate_list))]:
        key = json.dumps(cand.params.to_dict(), sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        leaderboard.append(cand.to_dict())

    return OptimizeResult(
        objective=objective,
        space={k: vars(v) for k, v in space.items()},
        train_range={"start": train_range[0], "end": train_range[1]},
        validate_range={"start": validate_range[0], "end": validate_range[1]},
        baseline=baseline,
        leaderboard=leaderboard,
        best=best.to_dict() if best else None,
        elapsed_sec=round(time.time() - started, 1),
        note=note,
    )


def _clamp(value: float, bounds: Bounds) -> float:
    value = max(bounds.low, min(bounds.high, value))
    if bounds.kind == "int":
        return float(round(value))
    steps = round((value - bounds.low) / bounds.step)
    return round(bounds.low + steps * bounds.step, 6)


def format_report(result: OptimizeResult) -> str:
    """Markdown report for the dashboard / stored run."""
    lines = ["# 参数优化报告", ""]
    lines.append(f"- 目标函数: `{result.objective}`")
    lines.append(f"- 训练集: {result.train_range['start']} → {result.train_range['end']}")
    lines.append(f"- 验证集: {result.validate_range['start']} → {result.validate_range['end']}")
    lines.append(f"- 耗时: {result.elapsed_sec}s")
    if result.note:
        lines.append("")
        lines.append(f"> {result.note}")
    lines.append("")
    lines.append("## 候选排行")
    lines.append("")
    lines.append("| 排名 | 来源 | 入场窗口 | 阈值 | 确认 | 上限 | 训练净利 | 训练t | 验证笔数 | 验证净利 | 验证t | 验证ROI |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for rank, item in enumerate(result.leaderboard[:10], 1):
        params = item["params"]
        train = item.get("train") or {}
        val = item.get("validation") or {}
        head = (
            f"| {rank} | {item.get('label','')} | {params['entry_window_sec']:.0f} | "
            f"{params['threshold']:.3f} | {params['confirm_sec']:.0f} | "
            f"{params['max_entry_price']:.3f} | "
            f"{train.get('net_pnl', 0):.2f} | {_fmt(train.get('t_stat'))} | "
        )
        if val:
            tail = (
                f"{int(val.get('trades') or 0)} | {_fmt(val.get('net_pnl'))} | "
                f"{_fmt(val.get('t_stat'))} | {(val.get('roi') or 0) * 100:.2f}% |"
            )
        else:
            tail = "-- | -- | -- | -- |"
        lines.append(head + tail)
    if result.baseline:
        train = result.baseline.get("train") or {}
        params = result.baseline.get("params") or {}
        lines.append("")
        lines.append(
            f"基线（{float(params.get('entry_window_sec', 0)):.0f}s / "
            f"{float(params.get('threshold', 0)):.3f} / "
            f"{float(params.get('confirm_sec', 0)):.0f}s / "
            f"{float(params.get('max_entry_price', 0)):.3f}）训练集净利 "
            f"{train.get('net_pnl', 0):.2f}，ROI {(train.get('roi') or 0) * 100:.2f}%"
        )
    return "\n".join(lines)


def _fmt(value) -> str:
    return "--" if value is None else f"{float(value):.2f}"
