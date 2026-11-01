"""The tuning agent's hard guardrails (no API key required)."""
from __future__ import annotations

import pytest

import json

from src.config import Settings
from src.research.agent import TuningAgent, _params_key
from src.research.backtest import BacktestParams


@pytest.fixture()
def agent(tmp_path):
    settings = Settings(DATA_DIR=str(tmp_path), DATASET_ROOT=str(tmp_path),
                        COLLECT_DIR=None if False else "")
    return TuningAgent(settings)


async def test_finalize_requires_out_of_sample_validation(agent):
    output, is_error = await agent._execute_tool("finalize", {
        "decision": "apply_candidate",
        "parameters": {"ENTRY_THRESHOLD": 0.92},
        "rationale": "looks good",
    })
    assert is_error is True
    assert "validate_params" in output
    assert agent._finalized is None


async def test_keep_baseline_needs_no_validation(agent):
    output, is_error = await agent._execute_tool("finalize", {
        "decision": "keep_baseline",
        "parameters": {},
        "rationale": "样本外不成立",
    })
    assert is_error is False
    assert agent._finalized is not None
    assert agent._finalized["decision"] == "keep_baseline"


async def test_finalize_rejects_non_tunable_keys(agent):
    output, is_error = await agent._execute_tool("finalize", {
        "decision": "keep_baseline",
        "parameters": {"STAKE_USD": 10, "WALLET_PRIVATE_KEY": "nope"},
        "rationale": "x",
    })
    assert is_error is True
    assert "WALLET_PRIVATE_KEY" in output


async def test_tool_input_validation(agent):
    output, is_error = await agent._execute_tool("run_backtest", {
        "dataset": "history", "threshold": "not-a-number",
    })
    assert is_error is True
    assert "threshold" in output

    output, is_error = await agent._execute_tool("validate_params", {
        "dataset": "history", "entry_window_sec": 60,
        "threshold": 0.9, "confirm_sec": 10, "max_entry_price": 0.94,
        "unexpected": 1,
    })
    assert is_error is True
    assert "unexpected" in output


async def test_unknown_tool(agent):
    output, is_error = await agent._execute_tool("drop_database", {})
    assert is_error is True
    assert "未知工具" in output


async def test_validate_on_already_seen_range_is_not_oos(agent):
    # Simulates a backtest the agent already ran on that range.
    agent._note_range("2026-01-01", "2026-01-31")
    output, is_error = await agent._execute_tool("validate_params", {
        "dataset": "history",
        "start_date": "2026-01-10", "end_date": "2026-01-20",
        "entry_window_sec": 60, "threshold": 0.90,
        "confirm_sec": 10, "max_entry_price": 0.94,
    })
    assert is_error is False
    payload = json.loads(output)
    assert payload["validated"] is False
    assert "重叠" in payload["reason"]


async def test_finalize_accepts_optimizer_validated_candidate(agent):
    params = BacktestParams(entry_window_sec=60, threshold=0.895,
                            confirm_sec=10, max_entry_price=0.94)
    agent._search_validated[_params_key(params)] = {
        "source": "optimizer_validation", "trades": 81, "net_pnl": 50.9,
    }
    output, is_error = await agent._execute_tool("finalize", {
        "decision": "apply_candidate",
        "parameters": {"ENTRY_WINDOW_SEC": 60, "ENTRY_THRESHOLD": 0.895,
                       "CONFIRM_SEC": 10, "MAX_ENTRY_PRICE": 0.94,
                       "MIN_ENTRY_PRICE": 0},
        "rationale": "验证集为正",
    })
    assert is_error is False, output
    assert agent._finalized["evidence"]["trades"] == 81


async def test_finalize_rejects_thin_validation(agent):
    params = BacktestParams(entry_window_sec=60, threshold=0.895,
                            confirm_sec=10, max_entry_price=0.94)
    agent._validated[_params_key(params)] = {"source": "validate_params", "trades": 2}
    output, is_error = await agent._execute_tool("finalize", {
        "decision": "apply_candidate",
        "parameters": {"ENTRY_WINDOW_SEC": 60, "ENTRY_THRESHOLD": 0.895,
                       "CONFIRM_SEC": 10, "MAX_ENTRY_PRICE": 0.94},
        "rationale": "x",
    })
    assert is_error is True
    assert "少于 5" in output


async def test_backtest_budget_is_capped(tmp_path):
    settings = Settings(DATA_DIR=str(tmp_path), DATASET_ROOT=str(tmp_path),
                        AGENT_MAX_BACKTESTS=1)
    agent = TuningAgent(settings)
    agent._consume_backtest_budget()
    with pytest.raises(RuntimeError):
        agent._consume_backtest_budget()
