"""Background research jobs (backtest / optimiser / agent).

One job at a time: the backtest is CPU-bound and the trading loop shares
this process, so queueing keeps the last seconds of a window responsive.
Runs are persisted in `research_runs` so the dashboard can show history
across restarts.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import PERIODS, Settings
from src.infra.logging import get_logger
from src.persistence.db import DB
from src.research.backtest import BacktestParams, run_backtest
from src.research.optimize import format_report as format_optimize_report
from src.research.optimize import optimize

log = get_logger(__name__)


@dataclass
class Job:
    run_id: int
    kind: str
    period: str = "15m"
    status: str = "running"
    progress: float = 0.0
    message: str = ""
    result: Optional[dict] = None
    report: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "period": self.period,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "result": self.result,
            "report": self.report,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": (self.finished_at or time.time()) - self.started_at,
        }


class ResearchService:
    def __init__(self, settings: Settings, db: DB,
                 strategies: Optional[dict[str, Settings]] = None) -> None:
        self.settings = settings
        self.db = db
        # One Settings clone per period, shared with the trading runtimes so
        # research and live/config always refer to the same parameters. The
        # research pages remain BTC-scoped.
        self.strategies = strategies or {
            period: settings.for_strategy("BTC", period) for period in PERIODS
        }
        self._job: Optional[Job] = None
        self._task: Optional[asyncio.Task] = None
        # A killed process leaves its row "running"; reconcile it so the UI
        # does not poll a job that no longer exists.
        try:
            db.fail_stale_research_runs()
        except Exception:  # noqa: BLE001
            pass

    # ── status ────────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        return self._job is not None and self._job.status == "running"

    def status(self, run_id: Optional[int] = None) -> dict:
        job = self._job
        if job is None or (run_id is not None and job.run_id != run_id):
            return {"busy": False, "job": None}
        return {"busy": self.busy, "job": job.to_dict()}

    def cancel(self) -> None:
        if self._job is not None and self._job.status == "running":
            self._job.cancel_requested = True
            self._job.message = "正在取消…"
            log.warning("research.cancel_requested", run_id=self._job.run_id)

    async def stop(self) -> None:
        """Cancel the in-flight job (process shutdown)."""
        self.cancel()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ── start ─────────────────────────────────────────────────────────────

    def _guard(self) -> None:
        if self.busy:
            raise RuntimeError("已有研究任务在运行，请等待完成或先取消")

    def _strategy(self, period: str) -> Settings:
        return self.strategies.get(period) or self.settings.for_period(period)

    def _backtest_params(self, payload: dict, period: str) -> BacktestParams:
        strategy = self._strategy(period)

        def pick(key: str, attr: str, default: float) -> float:
            value = payload.get(key)
            if value is None:
                value = getattr(strategy, attr, default)
            return float(value)

        return BacktestParams(
            entry_window_sec=pick("entry_window_sec", "ENTRY_WINDOW_SEC", 60),
            threshold=pick("threshold", "ENTRY_THRESHOLD", 0.90),
            confirm_sec=pick("confirm_sec", "CONFIRM_SEC", 10),
            max_entry_price=pick("max_entry_price", "MAX_ENTRY_PRICE", 0.94),
            min_entry_price=pick("min_entry_price", "MIN_ENTRY_PRICE", 0.0),
            stake=pick("stake", "STAKE_USD", 10.0),
            slippage=float(payload.get("slippage") or 0.005),
            fee_rate=float(payload.get("fee_rate") or strategy.TAKER_FEE_RATE),
        )

    def _new_job(self, kind: str, params: dict, period: str = "15m") -> Job:
        run_id = self.db.create_research_run(kind, params, period=period)
        self._job = Job(run_id=run_id, kind=kind, period=period)
        return self._job

    async def start_backtest(self, payload: dict) -> int:
        self._guard()
        period = payload.get("period", "15m")
        params = self._backtest_params(payload, period)
        job = self._new_job("backtest", {**payload, "params": params.to_dict()}, period)
        root = self._root(payload.get("dataset", "history"))
        self._task = asyncio.create_task(
            self._run_backtest_job(job, root, params,
                                   payload.get("start_date"), payload.get("end_date"),
                                   is_live=payload.get("dataset") == "live"),
            name=f"research-backtest-{job.run_id}",
        )
        return job.run_id

    async def start_optimize(self, payload: dict) -> int:
        self._guard()
        period = payload.get("period", "15m")
        base = self._backtest_params(payload, period)
        job = self._new_job("optimize", {**payload, "params": base.to_dict()}, period)
        root = self._root(payload.get("dataset", "history"))
        self._task = asyncio.create_task(
            self._run_optimize_job(job, root, base, payload), name=f"research-opt-{job.run_id}"
        )
        return job.run_id

    async def start_agent(self, payload: dict) -> int:
        self._guard()
        job = self._new_job("agent", payload, payload.get("period", "15m"))
        self._task = asyncio.create_task(
            self._run_agent_job(job, payload), name=f"research-agent-{job.run_id}"
        )
        return job.run_id

    # ── workers ───────────────────────────────────────────────────────────

    def _root(self, dataset: str):
        return self.settings.dataset_root if dataset == "history" else self.settings.collect_dir

    def _cancelled(self) -> bool:
        return bool(self._job and self._job.cancel_requested)

    async def _run_backtest_job(self, job: Job, root, params: BacktestParams,
                                start: Optional[str], end: Optional[str],
                                is_live: bool = False) -> None:
        try:
            def progress(frac: float, message: str) -> None:
                job.progress = frac
                job.message = message

            strategy = self._strategy(job.period)
            result = await asyncio.to_thread(
                run_backtest, root, strategy.ASSET, strategy.PERIOD,
                start, end, params, progress=progress, should_cancel=self._cancelled,
                is_live=is_live,
            )
            job.result = result.to_dict()
            job.report = _backtest_report(result)
            job.progress = 1.0
            job.status = "done"
            self.db.finish_research_run(job.run_id, status="done",
                                        summary=result.summary, report=job.report)
            log.info("research.backtest_done", run_id=job.run_id,
                     trades=result.summary.get("trades"),
                     net=result.summary.get("net_pnl"))
        except asyncio.CancelledError:
            self._finish_cancelled(job)
            raise
        except RuntimeError as exc:
            self._finish_cancelled_or_error(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._finish_error(job, exc)
        finally:
            job.finished_at = time.time()

    async def _run_optimize_job(self, job: Job, root, base: BacktestParams,
                                payload: dict) -> None:
        try:
            def progress(frac: float, message: str) -> None:
                job.progress = frac
                job.message = message

            strategy = self._strategy(job.period)
            result = await asyncio.to_thread(
                optimize, root, strategy.ASSET, strategy.PERIOD, base,
                start_date=payload.get("start_date"),
                end_date=payload.get("end_date"),
                train_end=payload.get("train_end"),
                validate_start=payload.get("validate_start"),
                validate_end=payload.get("validate_end"),
                objective=payload.get("objective", "profit_dd"),
                iterations=int(payload.get("iterations", 24)),
                min_trades=int(payload.get("min_trades", 20)),
                progress=progress,
                should_cancel=self._cancelled,
                is_live=payload.get("dataset") == "live",
            )
            job.result = result.to_dict()
            job.report = format_optimize_report(result)
            job.progress = 1.0
            job.status = "done"
            summary = {
                "best": result.best["params"] if result.best else None,
                "best_validation": (result.best or {}).get("validation"),
            }
            self.db.finish_research_run(job.run_id, status="done", summary=summary,
                                        report=job.report)
            log.info("research.optimize_done", run_id=job.run_id,
                     best=summary["best"])
        except asyncio.CancelledError:
            self._finish_cancelled(job)
            raise
        except RuntimeError as exc:
            self._finish_cancelled_or_error(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._finish_error(job, exc)
        finally:
            job.finished_at = time.time()

    async def _run_agent_job(self, job: Job, payload: dict) -> None:
        try:
            from src.research.agent import TuningAgent

            def progress(message: str) -> None:
                job.message = message

            agent = TuningAgent(self._strategy(job.period), progress=progress,
                                should_cancel=self._cancelled)
            result = await agent.run(
                goal=payload.get("goal", "在控制回撤的前提下最大化净收益"),
                start_date=payload.get("start_date"),
                end_date=payload.get("end_date"),
                iterations=int(payload.get("iterations", 6)),
                model=payload.get("model") or None,
            )
            if not result.ok:
                raise RuntimeError(result.error or "agent failed")
            job.result = {
                "proposal": result.proposal,
                "usage": result.usage,
                "transcript": result.transcript,
            }
            job.report = result.report
            job.progress = 1.0
            job.status = "done"
            self.db.finish_research_run(job.run_id, status="done",
                                        summary=result.proposal, report=result.report)
            log.info("research.agent_done", run_id=job.run_id,
                     decision=result.proposal.get("decision"),
                     tokens=result.usage)
        except asyncio.CancelledError:
            self._finish_cancelled(job)
            raise
        except RuntimeError as exc:
            self._finish_cancelled_or_error(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._finish_error(job, exc)
        finally:
            job.finished_at = time.time()

    def _finish_cancelled(self, job: Job) -> None:
        job.status = "cancelled"
        job.error = "已取消"
        job.finished_at = time.time()
        self.db.finish_research_run(job.run_id, status="cancelled", error="cancelled")

    def _finish_cancelled_or_error(self, job: Job, message: str) -> None:
        if "cancel" in message.lower():
            job.status = "cancelled"
            job.error = "已取消"
            self.db.finish_research_run(job.run_id, status="cancelled", error="cancelled")
        else:
            self._finish_error(job, RuntimeError(message))

    def _finish_error(self, job: Job, exc: Exception) -> None:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        self.db.finish_research_run(job.run_id, status="error", error=job.error)
        log.exception("research.job_failed", run_id=job.run_id, error=str(exc))


def _backtest_report(result) -> str:
    s = result.summary
    lines = [
        "# 回测报告",
        "",
        f"- 数据: {s.get('date_min')} → {s.get('date_max')}（{s.get('dates_processed')} 天，"
        f"{s.get('markets_scanned')} 个市场）",
        f"- 参数: 窗口 {result.params.entry_window_sec:.0f}s / 阈值 {result.params.threshold:.2f}"
        f" / 确认 {result.params.confirm_sec:.0f}s / 上限 {result.params.max_entry_price:.2f}"
        f" / 滑点 {result.params.slippage:.3f}",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 交易数 | {s.get('trades', 0)} |",
        f"| 胜率 | {(s.get('win_rate') or 0) * 100:.2f}% |",
        f"| 平均入场价 | {(s.get('avg_entry_price') or 0):.4f} |",
        f"| 价差 | {(s.get('price_edge') or 0) * 100:+.2f}pp |",
        f"| 净收益 | {(s.get('net_pnl') or 0):+.2f} U |",
        f"| ROI | {(s.get('roi') or 0) * 100:+.2f}% |",
        f"| t 值 | {_num(s.get('t_stat'))} |",
        f"| 最大回撤 | {(s.get('max_drawdown') or 0):+.2f} U |",
        f"| 正收益月份 | {s.get('positive_months', 0)}/{s.get('total_months', 0)} |",
    ]
    skip = s.get("skip_counts") or {}
    if skip:
        lines += ["", "## 跳过原因", "", "| 原因 | 数量 |", "|---|---:|"]
        for reason, count in skip.items():
            lines.append(f"| {reason} | {count} |")
    months = s.get("months") or {}
    if months:
        lines += ["", "## 分月", "", "| 月份 | 净收益 |", "|---|---:|"]
        for month, value in months.items():
            lines.append(f"| {month} | {value:+.2f} |")
    return "\n".join(lines)


def _num(value: Any) -> str:
    return "--" if value is None else f"{float(value):.2f}"
