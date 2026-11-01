"""LLM parameter-tuning agent.

The agent is deliberately boxed in by the same tools a human researcher
uses: it can read the dataset summary, run a backtest, run the deterministic
out-of-sample search, and validate a candidate. It cannot touch live trading
or invent metrics -- every number it sees comes from `backtest.py`.

Guardrails enforced here (not just in the prompt):

  * a hard cap on backtests per run (AGENT_MAX_BACKTESTS)
  * `finalize` is rejected unless the exact parameter set was already
    validated on a held-out date range
  * only keys in ``config.TUNABLE_SPECS`` may appear in the proposal
  * the final proposal is advisory -- applying it is a separate, explicit
    dashboard action

Requires ``pip install anthropic`` and an ANTHROPIC_API_KEY (or any other
credential source the SDK resolves). Without them the deterministic
optimiser remains available from the UI/CLI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.config import TUNABLE_SPECS, Settings
from src.infra.logging import get_logger
from src.research.backtest import BacktestParams, run_backtest
from src.research.dataset import dataset_summary, live_dataset_summary
from src.research.optimize import format_report, optimize

log = get_logger(__name__)

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def build_system_prompt(settings: Settings) -> str:
    period = settings.PERIOD
    window = settings.window_seconds
    stake = settings.STAKE_USD
    current = settings.strategy_params()
    if period == "5m":
        focus = f"""当前研究 **5 分钟** 市场（窗口 {window} 秒，单笔 {stake:.0f} USDC）。

5m 的特殊风险（来自文档）：
- 5m 核心规则（不设上限）平均入场 0.975–0.980，是**滑点陷阱**：官方费用很低，但 1 分钱滑点
  就会让 BTC 变成 -0.54%。
- 0.92 上限是最佳简单版本：最后 30 秒 BTC +1.04%（t=1.35）、最后 20 秒 +2.11%（t=1.61）、
  最后 10 秒 +5.88%（t=3.41，仅 109 笔）。最后 10–15 秒的样本很小，且 1 秒 K 线不是订单簿，
  实盘可能无法按回测价成交。
- 10 秒确认不适合 5m（30 秒窗口等 10 秒太久）；默认确认 0 秒。
- 因此 5m 的搜索空间是窗口 5–30 秒、确认 0–10 秒、上限 0.88–0.96。
- 对 5m 的结论要更保守：验证集为正、t ≥ 2、且 0.01 滑点下仍不为负，才值得考虑。"""
        baseline_note = "当前实盘 5m 参数（可被本次调优替换）"
    else:
        focus = f"""当前研究 **15 分钟** 市场（窗口 {window} 秒，单笔 {stake:.0f} USDC）。

15m 是文档中统计上最可靠的组合（8–10 月 ROI +4.27%，t=3.75），但注意：
- 8–10 月是事后挑选的强区间；完整 6 个月（上限 0.95）只有 +1.24%，5 月为负。
- 0.94 上限优于 0.95，因为 0.94–0.95 档的价差明显偏弱。
- 文档建议：冻结 15m 参数，用 11–12 月新数据做样本外验证，不要继续调参。
- 因此对 15m 的默认结论通常是 keep_baseline；除非样本外证据非常强。"""
        baseline_note = "当前实盘 15m 参数（文档推荐，通常应冻结）"

    return f"""你是 Polymarket「BTC 尾盘热门方」策略的量化研究代理。

{focus}

{baseline_note}：
- 入场窗口 {current['entry_window_sec']:.0f} 秒
- 触发阈值 {current['threshold']:.2f}
- 确认 {current['confirm_sec']:.0f} 秒
- 入场价上限 {current['max_entry_price']:.2f}
- 单笔 {stake:.0f} USDC，官方费率 {settings.TAKER_FEE_RATE:.3f}

## 策略机制（不可修改，只能调参数）

## 策略机制（不可修改，只能调参数）
每个 15 分钟 Up/Down 市场，在窗口最后 N 秒内：
1. 任意一侧的 CLOB 中间价首次 >= 触发阈值（默认 0.90）→ 记录方向与价格；
2. 等待 C 秒确认（默认 10 秒）；
3. 确认时该侧价格仍 >= 阈值，且入场价 < 入场价上限（默认 0.94）→ 以当时价格买入；
4. 买入后持有到结算：该侧获胜每股兑付 1.0，否则 0。
单笔固定 10 USDC。

官方 taker 费用：fee = 份额 × 费率 × p × (1-p)，只在入场收取（结算价 p=1/0 无费用）。
盈亏：pnl = 份额 × 赔付 − 成本 − 费用。

## 关键经济学
- 在价格 p 买入并持有到结算，**盈亏平衡胜率恰好等于 p**。
- 策略的全部优势来自「实际胜率 − 平均入场价」这个价差（price edge）。
- 因此：提高上限会提高入场价、降低价差；确认机制会减少交易数但提高胜率。

## 已有回测结论（2026-08 至 2026-10，BTC 15m，10U/笔，官方费用 + 0.005 滑点）
- 60 秒窗口 / 0.90 阈值 / 10 秒确认 / 上限 0.94：248 笔，胜率 97.18%，平均入场 0.9224，
  价差 +4.94pp，净利 +106.01U，ROI +4.27%，t=3.75，8/9/10 月全部为正。
- 上限 0.95：390 笔，ROI +2.90%，t=2.97。
- 0.94–0.95 这一档价差明显偏弱，所以 0.94 优于 0.95。
- 完整 6 个月（上限 0.95）只有 +1.24%，t=1.85，5 月为负 → 8–10 月是事后挑选的强区间，
  不能当作未来预期。
- 5m 版本整体是滑点陷阱，不在本次范围内。

## 你的目标
在**样本外**仍然站得住的前提下，尽可能提高净收益。用户会给你目标与日期范围。

## 必须遵守的研究纪律
1. 任何候选都必须在一个**未被用于挑选它**的日期区间上验证；只报告训练集结果的候选无效。
2. 至少 20 笔交易才有统计意义；交易数过少时明确说「样本不足」。
3. t 值 < 2 的改进视为不显著，不要仅凭 ROI 排名。
4. 同时关注 0.005 和 0.01 两种滑点假设：0.01 下转负的方案不可接受。
5. 参数越多越容易过拟合。优先选择在多个片段的月份上都稳定的少数参数。
6. 如果样本外收益为负，**正确结论是维持冻结参数**，而不是继续调参。请在 finalize 里给出
   baseline 参数并说明原因。

## 工作流程（建议）
1. `dataset_overview` 看待用数据范围与规模；
2. `run_backtest` 用文档基线跑一次训练区间，建立参照；
3. `run_search` 在训练区间做确定性搜索（它会自动保留样本外验证集）；
4. 对排名靠前且机制合理的 1–3 组参数调用 `validate_params`，区间必须包含验证集日期；
5. `finalize` 给出最终建议。选择 `apply_candidate` 时需要证据，二者其一：
   - 参数来自 `run_search` 的排行榜，且该条目已在搜索自带的验证集上评估（≥5 笔）；
   - 或者你用 `validate_params` 在一个**本次会话从未回测/搜索过的日期区间**上验证过
     （≥5 笔）。在已用过的区间上重复验证不算样本外，会被明确拒绝。
   如果找不到这样的区间，正确结论是 `keep_baseline`。

## 输出语言
始终使用中文。数字保留合理精度（金额 2 位、价格 4 位、百分比 2 位）。
"""


TOOLS: list[dict[str, Any]] = [
    {
        "name": "dataset_overview",
        "description": "查看历史数据集与本机实盘采集数据的日期范围、天数和市场数量。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "run_backtest",
        "description": (
            "在指定日期区间回测一组参数，返回胜率、平均入场价、价差、净收益、ROI、"
            "t 值、最大回撤、交易数与跳过原因。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": ["history", "live"]},
                "start_date": {"type": "string", "description": "YYYY-MM-DD，留空表示最早"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD，留空表示最新"},
                "entry_window_sec": {"type": "number"},
                "threshold": {"type": "number"},
                "confirm_sec": {"type": "number"},
                "max_entry_price": {"type": "number"},
                "min_entry_price": {"type": "number"},
                "stake": {"type": "number"},
                "slippage": {"type": "number"},
                "fee_rate": {"type": "number"},
            },
            "required": ["dataset"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_search",
        "description": (
            "在训练集上做确定性随机搜索，并自动把前几名放到验证集上评估。"
            "返回排行榜（含训练与验证指标）。比手工逐个 run_backtest 更省调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": ["history", "live"]},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "objective": {
                    "type": "string",
                    "enum": ["profit_dd", "pnl", "roi", "tstat"],
                },
                "iterations": {"type": "integer"},
                "min_trades": {"type": "integer"},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只搜索这些参数；省略表示全部",
                },
            },
            "required": ["dataset"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_params",
        "description": (
            "在指定区间（应为样本外）验证一组完整参数。finalize 前必须对完全相同的"
            "参数调用过本工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": ["history", "live"]},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "entry_window_sec": {"type": "number"},
                "threshold": {"type": "number"},
                "confirm_sec": {"type": "number"},
                "max_entry_price": {"type": "number"},
                "min_entry_price": {"type": "number"},
                "slippage": {"type": "number"},
                "fee_rate": {"type": "number"},
            },
            "required": ["dataset", "entry_window_sec", "threshold", "confirm_sec",
                         "max_entry_price"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finalize",
        "description": (
            "提交最终建议并结束本次研究。parameters 只能包含可调参数"
            f"（{', '.join(sorted(TUNABLE_SPECS))}）。"
            "apply_candidate 需要样本外证据：该参数要么来自 run_search 的验证集结果，"
            "要么已被 validate_params 在一个全新的日期区间验证过（≥5 笔）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["apply_candidate", "keep_baseline"],
                },
                "parameters": {"type": "object"},
                "rationale": {"type": "string"},
                "risks": {"type": "string"},
                "oos_summary": {"type": "string"},
            },
            "required": ["decision", "parameters", "rationale"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class AgentResult:
    ok: bool
    report: str = ""
    proposal: dict = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str = ""


class TuningAgent:
    def __init__(
        self,
        settings: Settings,
        *,
        progress: Optional[ProgressFn] = None,
        should_cancel: Optional[CancelFn] = None,
    ) -> None:
        self.settings = settings
        self._progress = progress or (lambda message: None)
        self._should_cancel = should_cancel or (lambda: False)
        self._backtests = 0
        # params hash -> evidence record for a genuine out-of-sample check
        self._validated: dict[str, dict] = {}
        # candidates the optimizer already validated on its held-out split
        self._search_validated: dict[str, dict] = {}
        # date ranges this session has already looked at; validating on one of
        # them is in-sample and does not count
        self._seen_ranges: list[tuple[str, str]] = []
        self._finalized: Optional[dict] = None
        self._transcript: list[dict] = []

    # ── public ────────────────────────────────────────────────────────────

    async def run(self, goal: str, start_date: Optional[str] = None,
                  end_date: Optional[str] = None, iterations: int = 6,
                  model: Optional[str] = None) -> AgentResult:
        try:
            import anthropic
        except ImportError:
            return AgentResult(
                ok=False,
                error="未安装 anthropic 包：pip install '.[agent]'（或 pip install anthropic）",
            )

        model = model or self.settings.AGENT_MODEL
        # The SDK resolves credentials itself (API key, auth token, or an
        # `ant auth login` profile); only pass an explicit key when configured.
        api_key = self.settings.ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY") or None
        # A research turn can be long (thinking + tool calls); 3 minutes is
        # generous without letting a stuck request hold the job for the SDK's
        # 10-minute default.
        client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=2, timeout=180.0
        ) if api_key else anthropic.AsyncAnthropic(max_retries=2, timeout=180.0)

        task = {
            "goal": goal,
            "start_date": start_date,
            "end_date": end_date,
            "max_iterations": iterations,
            "tunable_keys": sorted(TUNABLE_SPECS),
            "current_live_params": {
                **self.settings.strategy_params(),
                "stake": self.settings.STAKE_USD,
                "slippage": self.settings.EXEC_SLIPPAGE,
                "fee_rate": self.settings.TAKER_FEE_RATE,
            },
        }

        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": (
                "请开始研究。任务参数如下（JSON）：\n"
                + json.dumps(task, ensure_ascii=False, indent=2)
                + "\n\n先调用 dataset_overview，再按建议流程执行。"
            ),
        }]

        total_usage = {"input_tokens": 0, "output_tokens": 0}
        started = time.time()

        for turn in range(max(1, iterations)):
            if self._should_cancel():
                return AgentResult(ok=False, error="cancelled", transcript=self._transcript,
                                   usage=total_usage)
            self._progress(f"Agent 第 {turn + 1} 轮 · 请求模型")
            try:
                async with client.messages.stream(
                    model=model,
                    max_tokens=self.settings.AGENT_MAX_TOKENS,
                    system=[{
                        "type": "text",
                        "text": build_system_prompt(self.settings),
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=TOOLS,
                    messages=messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                ) as stream:
                    response = await stream.get_final_message()
            except anthropic.APIConnectionError as exc:
                return AgentResult(ok=False, error=f"无法连接 Anthropic API: {exc}",
                                   transcript=self._transcript, usage=total_usage)
            except anthropic.AuthenticationError:
                return AgentResult(ok=False, error="ANTHROPIC_API_KEY 无效或缺失",
                                   transcript=self._transcript, usage=total_usage)
            except anthropic.APIStatusError as exc:
                return AgentResult(ok=False, error=f"Anthropic API 错误 {exc.status_code}: {exc.message}",
                                   transcript=self._transcript, usage=total_usage)

            if getattr(response, "usage", None):
                total_usage["input_tokens"] += int(response.usage.input_tokens or 0) + int(
                    getattr(response.usage, "cache_read_input_tokens", 0) or 0
                )
                total_usage["output_tokens"] += int(response.usage.output_tokens or 0)

            if response.stop_reason == "refusal":
                return AgentResult(ok=False, error="模型拒绝了该请求", transcript=self._transcript,
                                   usage=total_usage)

            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [block for block in response.content if block.type == "tool_use"]
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if text:
                self._transcript.append({"role": "assistant", "text": text})

            if not tool_uses:
                break

            results = []
            for block in tool_uses:
                if self._should_cancel():
                    return AgentResult(ok=False, error="cancelled",
                                       transcript=self._transcript, usage=total_usage)
                self._progress(f"执行工具 {block.name}")
                output, is_error = await self._execute_tool(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                })
                self._transcript.append({
                    "role": "tool",
                    "name": block.name,
                    "input": block.input,
                    "output": output[:4000],
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": results})

            if self._finalized is not None:
                break

        if self._finalized is None:
            return AgentResult(
                ok=False,
                error="Agent 未在轮次上限内给出 finalize 结论。可提高轮次或直接使用确定性搜索。",
                transcript=self._transcript, usage=total_usage,
            )

        report = self._build_report(model, started, total_usage)
        return AgentResult(
            ok=True,
            report=report,
            proposal=self._finalized,
            transcript=self._transcript,
            usage=total_usage,
        )

    # ── tool execution ────────────────────────────────────────────────────

    async def _execute_tool(self, name: str, raw: dict) -> tuple[str, bool]:
        try:
            payload = self._validate_input(name, raw)
        except ValueError as exc:
            return f"参数校验失败: {exc}", True

        try:
            if name == "dataset_overview":
                return json.dumps(self._dataset_overview(), ensure_ascii=False), False
            if name == "run_backtest":
                return await self._run_backtest(payload), False
            if name == "run_search":
                return await self._run_search(payload), False
            if name == "validate_params":
                return await self._validate_params(payload), False
            if name == "finalize":
                return self._finalize(payload)
            return f"未知工具: {name}", True
        except Exception as exc:  # noqa: BLE001
            log.warning("agent.tool_failed", tool=name, error=str(exc))
            return f"工具执行失败: {type(exc).__name__}: {exc}", True

    def _validate_input(self, name: str, raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("tool input must be an object")
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            raise ValueError(f"未知工具: {name}")
        schema = tool["input_schema"]
        props = schema.get("properties", {})
        unknown = [key for key in raw if key not in props]
        if unknown:
            raise ValueError(f"未知字段: {unknown}")
        for key in schema.get("required", []):
            if key not in raw:
                raise ValueError(f"缺少必填字段: {key}")
        out: dict[str, Any] = {}
        for key, value in raw.items():
            kind = props[key].get("type")
            if kind == "number":
                try:
                    out[key] = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} 必须是数字") from None
            elif kind == "integer":
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} 必须是整数") from None
            elif kind == "string":
                if not isinstance(value, str):
                    raise ValueError(f"{key} 必须是字符串")
                out[key] = value
            elif kind == "array":
                if not isinstance(value, list):
                    raise ValueError(f"{key} 必须是数组")
                out[key] = value
            elif kind == "object":
                if not isinstance(value, dict):
                    raise ValueError(f"{key} 必须是对象")
                out[key] = value
            else:
                out[key] = value
        return out

    def _root(self, dataset: str):
        return self.settings.dataset_root if dataset == "history" else self.settings.collect_dir

    def _dataset_overview(self) -> dict:
        history = dataset_summary(self.settings.dataset_root, self.settings.ASSET,
                                  self.settings.PERIOD)
        live = live_dataset_summary(self.settings.collect_dir, self.settings.ASSET,
                                    self.settings.PERIOD)
        return {"history": history, "live": live,
                "baseline": {"entry_window_sec": 60, "threshold": 0.90, "confirm_sec": 10,
                             "max_entry_price": 0.94, "stake": 10.0,
                             "slippage": 0.005, "fee_rate": 0.07}}

    async def _run_backtest(self, payload: dict) -> str:
        self._consume_backtest_budget()
        live = self.settings.strategy_params()
        params = BacktestParams(
            entry_window_sec=payload.get("entry_window_sec", live["entry_window_sec"]),
            threshold=payload.get("threshold", live["threshold"]),
            confirm_sec=payload.get("confirm_sec", live["confirm_sec"]),
            max_entry_price=payload.get("max_entry_price", live["max_entry_price"]),
            min_entry_price=payload.get("min_entry_price", live["min_entry_price"]),
            stake=payload.get("stake", self.settings.STAKE_USD),
            slippage=payload.get("slippage", self.settings.EXEC_SLIPPAGE),
            fee_rate=payload.get("fee_rate", self.settings.TAKER_FEE_RATE),
        )
        errors = params.strategy_params().validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
        result = await asyncio.to_thread(
            run_backtest, self._root(payload["dataset"]), self.settings.ASSET,
            self.settings.PERIOD, payload.get("start_date"), payload.get("end_date"), params,
            is_live=payload["dataset"] == "live",
            should_cancel=self._should_cancel,
        )
        # Only a bounded range counts as "seen"; a full-range run does not
        # poison every later validation range.
        if payload.get("start_date") and payload.get("end_date"):
            self._note_range(payload["start_date"], payload["end_date"])
        return json.dumps({"params": params.to_dict(), "summary": _compact(result.summary)},
                          ensure_ascii=False)

    async def _run_search(self, payload: dict) -> str:
        self._consume_backtest_budget()
        keys = payload.get("keys")
        if keys:
            bad = [k for k in keys if k not in {"entry_window_sec", "threshold",
                                                "confirm_sec", "max_entry_price"}]
            if bad:
                raise ValueError(f"不支持搜索的字段: {bad}")
        base = BacktestParams()
        result = await asyncio.to_thread(
            optimize, self._root(payload["dataset"]), self.settings.ASSET,
            self.settings.PERIOD, base,
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            objective=payload.get("objective", "profit_dd"),
            iterations=max(1, min(int(payload.get("iterations", 24)), 200)),
            min_trades=int(payload.get("min_trades", 20)),
            keys=keys,
            is_live=payload["dataset"] == "live",
            should_cancel=self._should_cancel,
        )
        # The optimiser evaluates the top candidates on a held-out split; a
        # candidate with enough validation trades is legitimately OOS-tested
        # and may be finalized without a third date range.
        self._note_range(result.train_range["start"], result.train_range["end"])
        self._note_range(result.validate_range["start"], result.validate_range["end"])
        leaderboard = []
        for item in result.leaderboard[:8]:
            validation = _compact(item.get("validation") or {})
            if int(validation.get("trades") or 0) >= 5:
                candidate = BacktestParams.from_dict(item["params"])
                self._search_validated[_params_key(candidate)] = {
                    "source": "optimizer_validation",
                    "range": result.validate_range,
                    "trades": validation.get("trades"),
                    "net_pnl": validation.get("net_pnl"),
                    "t_stat": validation.get("t_stat"),
                }
            leaderboard.append({
                "params": item["params"],
                "label": item.get("label"),
                "train": _compact(item.get("train") or {}),
                "validation": validation,
            })
        return json.dumps({
            "train_range": result.train_range,
            "validate_range": result.validate_range,
            "objective": result.objective,
            "leaderboard": leaderboard,
            "best": result.best["params"] if result.best else None,
            "note": result.note,
            "report": format_report(result),
        }, ensure_ascii=False)

    async def _validate_params(self, payload: dict) -> str:
        self._consume_backtest_budget()
        params = BacktestParams(
            entry_window_sec=payload["entry_window_sec"],
            threshold=payload["threshold"],
            confirm_sec=payload["confirm_sec"],
            max_entry_price=payload["max_entry_price"],
            min_entry_price=payload.get(
                "min_entry_price", self.settings.MIN_ENTRY_PRICE
            ),
            stake=self.settings.STAKE_USD,
            slippage=payload.get("slippage", self.settings.EXEC_SLIPPAGE),
            fee_rate=payload.get("fee_rate", self.settings.TAKER_FEE_RATE),
        )
        errors = params.strategy_params().validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
        start = payload.get("start_date")
        end = payload.get("end_date")
        if self._overlaps_seen(start, end):
            return json.dumps({
                "validated": False,
                "reason": "该日期区间与本次会话已经回测/搜索过的区间重叠，不能算样本外。"
                          "请换一段从未使用过的日期再验证。",
                "seen_ranges": self._seen_ranges,
            }, ensure_ascii=False)
        result = await asyncio.to_thread(
            run_backtest, self._root(payload["dataset"]), self.settings.ASSET,
            self.settings.PERIOD, start, end, params,
            is_live=payload["dataset"] == "live",
            should_cancel=self._should_cancel,
        )
        summary = _compact(result.summary)
        trades = int(summary.get("trades") or 0)
        key = _params_key(params)
        record = {
            "source": "validate_params",
            "range": {"start": start, "end": end},
            "trades": trades,
            "net_pnl": summary.get("net_pnl"),
            "t_stat": summary.get("t_stat"),
        }
        if trades >= 5:
            self._validated[key] = record
        self._note_range(start, end)
        return json.dumps({"params": params.to_dict(), "summary": summary,
                           "validated": trades >= 5, "evidence": record},
                          ensure_ascii=False)

    def _finalize(self, payload: dict) -> tuple[str, bool]:
        raw_params = payload.get("parameters") or {}
        # Accept either the documented UPPER_CASE keys or lowercase ones.
        params = {str(key).upper(): value for key, value in raw_params.items()}
        unknown = [key for key in params if key not in TUNABLE_SPECS]
        if unknown:
            return f"parameters 含不可调字段: {unknown}", True

        normalized: dict[str, Any] = {}
        for key, value in params.items():
            try:
                normalized[key] = float(value)
            except (TypeError, ValueError):
                return f"parameters.{key} 必须是数字", True

        evidence: Optional[dict] = None
        if payload.get("decision") == "apply_candidate":
            key = _params_key(BacktestParams(
                entry_window_sec=float(normalized.get("ENTRY_WINDOW_SEC", 60)),
                threshold=float(normalized.get("ENTRY_THRESHOLD", 0.90)),
                confirm_sec=float(normalized.get("CONFIRM_SEC", 10)),
                max_entry_price=float(normalized.get("MAX_ENTRY_PRICE", 0.94)),
                min_entry_price=float(normalized.get("MIN_ENTRY_PRICE", 0.0)),
            ))
            evidence = self._validated.get(key) or self._search_validated.get(key)
            if evidence is None:
                return (
                    "拒绝 finalize：这组参数没有可用于样本外验证的证据。"
                    "validate_params 的区间必须与本次会话已回测过的区间不重叠；"
                    "或者使用 run_search 返回的、已在验证集上评估过的候选。",
                    True,
                )
            if int(evidence.get("trades") or 0) < 5:
                return (
                    "拒绝 finalize：样本外交易数少于 5，证据不足。",
                    True,
                )

        self._finalized = {
            "decision": payload.get("decision", "keep_baseline"),
            "parameters": normalized,
            "rationale": payload.get("rationale", ""),
            "risks": payload.get("risks", ""),
            "oos_summary": payload.get("oos_summary", ""),
            "evidence": evidence,
            "at": time.time(),
        }
        return "已记录最终建议。", False

    @staticmethod
    def _norm_range(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
        return (start or "0000-00-00", end or "9999-12-31")

    def _note_range(self, start: Optional[str], end: Optional[str]) -> None:
        self._seen_ranges.append(self._norm_range(start, end))

    def _overlaps_seen(self, start: Optional[str], end: Optional[str]) -> bool:
        s, e = self._norm_range(start, end)
        return any(a <= e and s <= b for a, b in self._seen_ranges)

    def _consume_backtest_budget(self) -> None:
        self._backtests += 1
        if self._backtests > self.settings.AGENT_MAX_BACKTESTS:
            raise RuntimeError(
                f"回测次数超过上限 {self.settings.AGENT_MAX_BACKTESTS}"
            )

    # ── report ────────────────────────────────────────────────────────────

    def _build_report(self, model: str, started: float, usage: dict) -> str:
        assert self._finalized is not None
        proposal = self._finalized
        lines = [
            "# Agent 参数调优报告",
            "",
            f"- 模型: `{model}`",
            f"- 耗时: {round(time.time() - started, 1)}s",
            f"- tokens: 输入 {usage.get('input_tokens', 0):,} / 输出 {usage.get('output_tokens', 0):,}",
            f"- 回测次数: {self._backtests}",
            "",
            f"## 结论: {proposal['decision']}",
            "",
            "| 参数 | 建议值 |",
            "|---|---:|",
        ]
        for key, value in sorted(proposal.get("parameters", {}).items()):
            lines.append(f"| {key} | {value} |")
        lines += [
            "",
            "## 理由",
            "",
            proposal.get("rationale") or "（未提供）",
            "",
            "## 样本外摘要",
            "",
            proposal.get("oos_summary") or "（未提供）",
            "",
            "## 风险",
            "",
            proposal.get("risks") or "（未提供）",
            "",
            "## 研究过程",
            "",
        ]
        for item in self._transcript:
            if item.get("role") == "assistant" and item.get("text"):
                lines.append(f"**Agent:** {item['text']}")
                lines.append("")
            elif item.get("role") == "tool":
                status = "失败" if item.get("is_error") else "成功"
                lines.append(
                    f"- `{item['name']}`（{status}）"
                    f"参数 `{json.dumps(item.get('input'), ensure_ascii=False)}`"
                )
        lines.append("")
        lines.append("> 该建议只修改运行时参数，不会自动上实盘。请在「参数」页确认后再应用。")
        return "\n".join(lines)


def _params_key(params: BacktestParams) -> str:
    """Identity of a strategy parameter set.

    Deliberately excludes slippage / fee rate: those are cost assumptions
    used inside a run, not parameters the finalize guardrail is about, and
    including them made an otherwise-identical validated candidate fail to
    match at finalize time.
    """
    canonical = json.dumps({
        "entry_window_sec": round(float(params.entry_window_sec), 4),
        "threshold": round(float(params.threshold), 4),
        "confirm_sec": round(float(params.confirm_sec), 4),
        "max_entry_price": round(float(params.max_entry_price), 4),
        "min_entry_price": round(float(params.min_entry_price), 4),
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _compact(summary: dict) -> dict:
    keys = [
        "trades", "win_rate", "avg_entry_price", "price_edge", "net_pnl", "roi",
        "t_stat", "max_drawdown", "breakeven_win_rate", "profit_factor",
        "dates_processed", "markets_scanned", "skip_counts", "months",
        "positive_months", "total_months", "avg_trades_per_day",
    ]
    out = {key: summary.get(key) for key in keys if key in summary}
    if out.get("months"):
        out["months"] = {k: round(float(v), 2) for k, v in out["months"].items()}
    return out
