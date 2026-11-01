"""FastAPI dashboard, served in-process on the bot's event loop.

Pages
    /            both strategies: live window, prices, signal state, PnL
    /trades      trade + signal history (filter by strategy)
    /config      per-strategy parameters and controls
    /research    backtest, parameter sweep, LLM tuning agent (per strategy)
    /login

Read APIs are public only when WEB_PASSWORD is empty; every write endpoint
always requires a session.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import Settings
from src.infra.logging import get_logger
from src.persistence.db import DB, compute_stats, equity_curve
from src.web import auth, runtime_config

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="PolyTail", docs_url=None, redoc_url=None, openapi_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _asset_version() -> str:
    digest = hashlib.md5()
    try:
        for name in ("style.css", "app.js"):
            path = STATIC_DIR / name
            if path.exists():
                digest.update(path.read_bytes())
    except Exception:  # noqa: BLE001
        return "1"
    return digest.hexdigest()[:10] or "1"


ASSET_VERSION = _asset_version()


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# State binding (the bot runs as python -m src.main; never import it here)
# ---------------------------------------------------------------------------

_bot_state: Any = None


def set_state(state: Any) -> None:
    global _bot_state
    _bot_state = state


def _state() -> Any:
    if _bot_state is None:
        raise HTTPException(status_code=503, detail="bot state not attached")
    return _bot_state


def _settings() -> Settings:
    return _state().settings


def _db() -> DB:
    return _state().db


def _runtimes() -> dict:
    return _state().runtimes


def _runtime(period: Optional[str]) -> Any:
    runtimes = _runtimes()
    if not period:
        # Default to the 15m strategy when only one exists.
        period = "15m" if "15m" in runtimes else next(iter(runtimes), "")
    runtime = runtimes.get(period)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"策略 {period} 未启用")
    return runtime


def _strategies_payload() -> list[dict]:
    return [
        {"period": period, "label": f"BTC {period}", "mode": rt.broker.mode}
        for period, rt in _runtimes().items()
    ]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


async def _require_auth(request: Request) -> None:
    settings = _settings()
    if not auth.auth_required(settings):
        return
    if auth.current_session(request) is None:
        raise HTTPException(status_code=401, detail="需要登录")


async def _require_login(request: Request) -> str:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录后才能修改配置")
    return user


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _nav(active: str) -> str:
    items = [
        ("/", "总览", "dashboard"),
        ("/trades", "交易记录", "trades"),
        ("/config", "参数", "config"),
        ("/research", "研究 / Agent", "research"),
    ]
    links = []
    for href, label, key in items:
        cls = "nav-link active" if key == active else "nav-link"
        links.append(f'<a class="{cls}" href="{href}">{label}</a>')
    return '<nav class="nav">' + "".join(links) + "</nav>"


def _render(name: str, *, active: str = "", **ctx: Any) -> HTMLResponse:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return HTMLResponse(f"<h1>{name} not found</h1>", status_code=500)
    html = path.read_text(encoding="utf-8")
    html = html.replace("__ASSET_VERSION__", ASSET_VERSION)
    html = html.replace("{{NAV}}", _nav(active))
    for key, value in ctx.items():
        html = html.replace("{{" + key + "}}", str(value))
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Any:
    if auth.auth_required(_settings()) and auth.current_session(request) is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return _render("dashboard.html", active="dashboard")


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request) -> Any:
    if auth.auth_required(_settings()) and auth.current_session(request) is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return _render("trades.html", active="trades")


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request) -> Any:
    if auth.current_session(request) is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return _render("config.html", active="config")


@app.get("/research", response_class=HTMLResponse)
async def research_page(request: Request) -> Any:
    if auth.current_session(request) is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return _render("research.html", active="research")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    if auth.current_session(request) is not None:
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=302)
    return _render("login.html")


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

@app.get("/api/auth/me")
async def api_auth_me(request: Request) -> dict:
    session = auth.current_session(request)
    settings = _settings()
    return {
        "authenticated": session is not None,
        "user": session.user if session else None,
        "password_configured": auth.credentials_configured(settings),
    }


@app.post("/api/auth/login")
async def api_auth_login(payload: LoginPayload, request: Request, response: Response) -> dict:
    settings = _settings()
    ip = auth.client_ip(request)
    if auth.login_blocked(ip):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    if not auth.credentials_configured(settings):
        raise HTTPException(status_code=400, detail="未配置 WEB_PASSWORD，无法登录")
    if not auth.verify_credentials(settings, payload.username, payload.password):
        auth.record_login_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    auth.reset_login_failures(ip)
    token = auth.create_session(settings, payload.username or settings.WEB_USERNAME)
    auth.set_session_cookie(response, token, request, settings.WEB_SESSION_TTL_S)
    return {"ok": True}


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request, response: Response) -> dict:
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/health")
async def api_health() -> dict:
    state = _state()
    runtimes = state.runtimes
    now = time.time()
    return {
        "ok": bool(runtimes),
        "strategies": {
            period: {
                "mode": rt.broker.mode,
                "uptime_s": now - rt._started_at,
            }
            for period, rt in runtimes.items()
        },
        "ts": now,
    }


# ---------------------------------------------------------------------------
# Data APIs
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(_: None = Depends(_require_auth)) -> dict:
    state = _state()
    return {
        "now": time.time(),
        "app": {
            "name": state.settings.APP_NAME,
            "asset": state.settings.ASSET,
            "password_configured": auth.credentials_configured(state.settings),
        },
        "strategies": [rt.status() for rt in state.runtimes.values()],
    }


@app.get("/api/trades")
async def api_trades(
    limit: int = Query(200, ge=1, le=2000),
    mode: Optional[str] = Query(None, pattern="^(paper|live)$"),
    period: Optional[str] = Query(None, pattern="^(15m|5m)$"),
    _: None = Depends(_require_auth),
) -> dict:
    db = _db()
    trades = db.recent_trades(limit=limit, mode=mode, period=period)
    settled = db.all_settled_trades(mode=mode, period=period)
    return {
        "trades": trades,
        "stats": compute_stats(settled),
        "equity": equity_curve(settled),
        "unredeemed": db.unredeemed_trades(period),
    }


@app.get("/api/signals")
async def api_signals(
    limit: int = Query(200, ge=1, le=2000),
    mode: Optional[str] = Query(None, pattern="^(paper|live)$"),
    period: Optional[str] = Query(None, pattern="^(15m|5m)$"),
    _: None = Depends(_require_auth),
) -> dict:
    return {"signals": _db().recent_signals(limit=limit, mode=mode, period=period)}


@app.get("/api/events")
async def api_events(
    limit: int = Query(200, ge=1, le=2000),
    min_level: str = Query("INFO"),
    _: None = Depends(_require_auth),
) -> dict:
    return {"events": _db().recent_events(limit=limit, min_level=min_level)}


@app.get("/api/wallet")
async def api_wallet(_: str = Depends(_require_login)) -> dict:
    state = _state()
    balances = {}
    for period, rt in state.runtimes.items():
        try:
            balances[period] = await rt.broker.balance()
        except Exception:  # noqa: BLE001
            balances[period] = None
    return {
        "modes": {period: rt.broker.mode for period, rt in state.runtimes.items()},
        "balances": balances,
        "funder": getattr(state.clob, "funder_address", "") if state.clob else "",
        "unredeemed": state.db.unredeemed_trades(),
    }


# ---------------------------------------------------------------------------
# Config API (scoped per strategy)
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def api_get_config(
    strategy: Optional[str] = Query(None, pattern="^(15m|5m)$"),
    _: str = Depends(_require_login),
) -> dict:
    runtime = _runtime(strategy)
    return runtime_config.payload(runtime.settings, _db(), runtime.settings.PERIOD)


class ConfigPayload(BaseModel):
    strategy: Optional[str] = None
    values: dict[str, Any]


@app.put("/api/config")
async def api_update_config(
    payload: ConfigPayload,
    _: str = Depends(_require_login),
) -> dict:
    runtime = _runtime(payload.strategy)
    period = runtime.settings.PERIOD
    ok, errors, applied = runtime_config.update(runtime.settings, _db(), period,
                                                payload.values)
    if not ok:
        raise HTTPException(status_code=400, detail={"errors": errors})
    log.info("config.updated", period=period, keys=list(applied), values=applied)
    _db().add_event("INFO", "config.updated", {"period": period, "values": applied})
    return {"ok": True,
            "values": runtime_config.payload(runtime.settings, _db(), period)["values"]}


@app.post("/api/config/reset")
async def api_reset_config(
    strategy: Optional[str] = Query(None, pattern="^(15m|5m)$"),
    _: str = Depends(_require_login),
) -> dict:
    runtime = _runtime(strategy)
    period = runtime.settings.PERIOD
    runtime_config.reset(runtime.settings, _db(), period)
    _db().add_event("INFO", "config.reset", {"period": period})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Controls (per strategy, or all)
# ---------------------------------------------------------------------------

class ControlPayload(BaseModel):
    strategy: str = "all"
    reason: str = ""


def _targets(strategy: str) -> list:
    runtimes = _runtimes()
    if strategy in ("", "all", None):
        return list(runtimes.values())
    runtime = runtimes.get(strategy)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy} 未启用")
    return [runtime]


@app.post("/api/control/killswitch")
async def api_killswitch(
    payload: ControlPayload,
    _: str = Depends(_require_login),
) -> dict:
    state = _state()
    result = {}
    for rt in _targets(payload.strategy):
        info = rt.risk.engage_killswitch(payload.reason or "手动停止")
        state.db.add_event("WARNING", "control.killswitch",
                           {"period": rt.settings.PERIOD, **info})
        result[rt.settings.PERIOD] = info
    return {"ok": True, "killswitch": result}


@app.post("/api/control/release")
async def api_release(
    payload: ControlPayload,
    _: str = Depends(_require_login),
) -> dict:
    state = _state()
    result = {}
    for rt in _targets(payload.strategy):
        info = rt.risk.release_killswitch()
        state.db.add_event("INFO", "control.release",
                           {"period": rt.settings.PERIOD, **info})
        result[rt.settings.PERIOD] = info
    return {"ok": True, "killswitch": result}


class PausePayload(BaseModel):
    strategy: str = "all"
    minutes: int = Field(30, ge=1, le=7 * 24 * 60)
    reason: str = "手动暂停"


@app.post("/api/control/pause")
async def api_pause(payload: PausePayload, _: str = Depends(_require_login)) -> dict:
    state = _state()
    result = {}
    for rt in _targets(payload.strategy):
        rt.risk.set_pause(payload.minutes * 60, payload.reason)
        state.db.add_event("WARNING", "control.pause",
                           {"period": rt.settings.PERIOD,
                            "minutes": payload.minutes, "reason": payload.reason})
        result[rt.settings.PERIOD] = rt.risk.pause_until
    return {"ok": True, "pause_until": result}


@app.post("/api/control/resume")
async def api_resume(payload: ControlPayload, _: str = Depends(_require_login)) -> dict:
    for rt in _targets(payload.strategy):
        rt.risk.clear_pause()
    return {"ok": True}


@app.post("/api/trades/{trade_id}/redeemed")
async def api_mark_redeemed(trade_id: int, _: str = Depends(_require_login)) -> dict:
    db = _db()
    trade = db.get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="trade not found")
    db.set_redeem_status(trade_id, "done")
    db.add_event("INFO", "trade.redeemed", {"trade_id": trade_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Research API
# ---------------------------------------------------------------------------

class BacktestRequest(BaseModel):
    period: str = "15m"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    dataset: str = "history"          # history | live
    # None = use the selected strategy's current live parameters.
    entry_window_sec: Optional[float] = None
    threshold: Optional[float] = None
    confirm_sec: Optional[float] = None
    max_entry_price: Optional[float] = None
    min_entry_price: Optional[float] = None
    stake: Optional[float] = None
    slippage: Optional[float] = None
    fee_rate: Optional[float] = None


class OptimizeRequest(BacktestRequest):
    objective: str = "profit_dd"
    iterations: int = Field(24, ge=4, le=200)
    train_end: Optional[str] = None
    validate_start: Optional[str] = None
    validate_end: Optional[str] = None
    min_trades: int = Field(20, ge=1)


class AgentRequest(BaseModel):
    period: str = "5m"
    goal: str = "在控制回撤的前提下最大化净收益"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    iterations: int = Field(6, ge=1, le=20)
    model: Optional[str] = None


@app.get("/api/research/summary")
async def api_research_summary(
    period: Optional[str] = Query(None, pattern="^(15m|5m)$"),
    _: str = Depends(_require_login),
) -> dict:
    from src.research.dataset import dataset_summary, live_dataset_summary

    state = _state()
    settings = state.settings
    result_period = period or "15m"
    out: dict[str, Any] = {"period": result_period,
                           "strategies": _strategies_payload()}
    # Reading the dataset summary touches a few files; keep it off the event
    # loop so a page load can never delay a last-second entry.
    try:
        out["history"] = await asyncio.to_thread(
            dataset_summary, settings.dataset_root, settings.ASSET, result_period
        )
    except Exception as exc:  # noqa: BLE001
        out["history"] = {"error": str(exc)}
    try:
        out["live"] = await asyncio.to_thread(
            live_dataset_summary, settings.collect_dir, settings.ASSET, result_period
        )
    except Exception as exc:  # noqa: BLE001
        out["live"] = {"error": str(exc)}
    out["agent_ready"] = bool(settings.ANTHROPIC_API_KEY)
    out["model"] = settings.AGENT_MODEL
    out["runs"] = state.db.recent_research_runs(limit=15)
    return out


@app.post("/api/research/backtest")
async def api_research_backtest(payload: BacktestRequest,
                                _: str = Depends(_require_login)) -> dict:
    run_id = await _state().research.start_backtest(payload.model_dump())
    return {"ok": True, "run_id": run_id}


@app.post("/api/research/optimize")
async def api_research_optimize(payload: OptimizeRequest,
                                _: str = Depends(_require_login)) -> dict:
    run_id = await _state().research.start_optimize(payload.model_dump())
    return {"ok": True, "run_id": run_id}


@app.post("/api/research/agent")
async def api_research_agent(payload: AgentRequest,
                             _: str = Depends(_require_login)) -> dict:
    run_id = await _state().research.start_agent(payload.model_dump())
    return {"ok": True, "run_id": run_id}


@app.post("/api/research/cancel")
async def api_research_cancel(_: str = Depends(_require_login)) -> dict:
    _state().research.cancel()
    return {"ok": True}


@app.get("/api/research/runs")
async def api_research_runs(
    limit: int = Query(20, ge=1, le=100),
    period: Optional[str] = Query(None, pattern="^(15m|5m)$"),
    _: str = Depends(_require_login),
) -> dict:
    return {"runs": _db().recent_research_runs(limit=limit, period=period)}


@app.get("/api/research/runs/{run_id}")
async def api_research_run(run_id: int, _: str = Depends(_require_login)) -> dict:
    db = _db()
    run = db.get_research_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    live = _state().research.status(run_id)
    return {"run": run, "live": live}


class ApplyPayload(BaseModel):
    strategy: str = "15m"
    values: dict[str, Any]


@app.post("/api/research/apply")
async def api_research_apply(payload: ApplyPayload,
                             _: str = Depends(_require_login)) -> dict:
    runtime = _runtime(payload.strategy)
    period = runtime.settings.PERIOD
    ok, errors, applied = runtime_config.update(runtime.settings, _db(), period,
                                                payload.values)
    if not ok:
        raise HTTPException(status_code=400, detail={"errors": errors})
    _db().add_event("INFO", "research.applied",
                    {"period": period, "values": applied})
    return {"ok": True, "applied": applied, "period": period}


@app.exception_handler(HTTPException)
async def _http_exception(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})
