"""Entry point: wire the components together and run.

    python -m src.main

Runs every enabled strategy (15m and/or 5m) in one process, each with its own
window loop, broker, market service, risk manager and settings clone, sharing
the SQLite database, the CLOB/Gamma HTTP client and the dashboard.

  * Runtime(15m) — the frozen documented configuration, meant for live
  * Runtime(5m)  — the strategy tuned continuously by the research agent
  * ResearchService — backtest / optimiser / agent jobs on demand
  * FastAPI — dashboard on WEB_PORT (default 8013)
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from src import config
from src.config import Settings
from src.execution.broker import Broker, build_broker
from src.execution.clob_client import ClobClient
from src.infra.logging import configure_logging, get_logger
from src.market.market_service import MarketService
from src.market.market_ws import MarketWS, PriceCache
from src.market.pm_client import PolymarketClient
from src.persistence.db import DB
from src.research.service import ResearchService
from src.runtime import Runtime
from src.web import runtime_config

log = get_logger(__name__)


@dataclass
class AppState:
    settings: Settings                      # global / base settings
    db: DB
    strategies: dict[str, Settings]         # per-strategy clones
    runtimes: dict[str, Runtime] = field(default_factory=dict)
    research: ResearchService | None = None
    clob: Optional[ClobClient] = None

    def runtime(self, period: str) -> Optional[Runtime]:
        return self.runtimes.get(period)


def _start_dashboard(state: AppState):
    """Create the uvicorn server task on the running loop."""
    settings = state.settings
    if not settings.WEB_ENABLED:
        log.info("web.disabled")
        return None
    try:
        import contextlib

        import uvicorn

        from src.web.app import app as web_app
        from src.web.app import set_state

        set_state(state)

        class _Server(uvicorn.Server):
            # The bot owns SIGINT/SIGTERM and uses them for a graceful stop;
            # uvicorn's default handler would swallow them.
            def capture_signals(self):  # type: ignore[override]
                return contextlib.nullcontext()

        server = _Server(
            uvicorn.Config(
                web_app,
                host=settings.WEB_HOST,
                port=int(settings.WEB_PORT),
                log_level=settings.WEB_LOG_LEVEL,
                access_log=False,
                lifespan="off",
            )
        )
        task = asyncio.create_task(server.serve(), name="web-dashboard")

        def _done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("web.server_exited", error=str(exc))

        task.add_done_callback(_done)
        log.info("web.started", host=settings.WEB_HOST, port=settings.WEB_PORT,
                 auth=bool(settings.WEB_PASSWORD))
        if not settings.WEB_PASSWORD:
            log.warning("web.auth_disabled",
                        note="设置 WEB_PASSWORD 以保护面板（/config 与 /research 始终需要登录）")
        return server
    except Exception as exc:  # noqa: BLE001
        log.error("web.start_failed", error=str(exc))
        return None


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    def _stop(*_: object) -> None:
        event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError, AttributeError):
            # Windows: the default asyncio loop does not support signal
            # handlers, so fall back to the synchronous API.
            try:
                signal.signal(sig, lambda *a: loop.call_soon_threadsafe(_stop))
            except (ValueError, OSError):
                pass


async def run() -> int:
    settings = config.settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(settings.LOG_LEVEL, settings.data_dir)

    db = DB(settings.db_path)

    # Per-strategy settings clones, with that strategy's runtime overrides.
    strategies: dict[str, Settings] = {}
    for period, strategy in settings.strategy_settings().items():
        runtime_config.apply_overrides(strategy, db, period)
        runtime_config.apply_mode_override(strategy, db, period)
        strategies[period] = strategy

    if not strategies:
        log.error("boot.no_strategy", note="ENABLED_15M / ENABLED_5M 都是关闭状态")
        return 2

    fatal = False
    for period, strategy in strategies.items():
        errors = strategy.validation_errors()
        for message in errors:
            log.error("config.invalid", period=period, error=message)
        if errors:
            fatal = True
    if fatal:
        log.error("config.abort", note="策略参数不合法，拒绝启动")
        return 2

    log.info(
        "boot",
        app=settings.APP_NAME,
        strategies={
            period: {
                "mode": s.TRADING_MODE,
                "window_s": s.ENTRY_WINDOW_SEC,
                "threshold": s.ENTRY_THRESHOLD,
                "confirm_s": s.CONFIRM_SEC,
                "max_price": s.MAX_ENTRY_PRICE,
                "stake": s.STAKE_USD,
            }
            for period, s in strategies.items()
        },
        db=str(settings.db_path),
    )

    pm = PolymarketClient(gamma_api=settings.GAMMA_API, clob_host=settings.POLYMARKET_HOST)

    clob: Optional[ClobClient] = None
    if settings.has_credentials or settings.WALLET_PRIVATE_KEY:
        clob = ClobClient(
            host=settings.POLYMARKET_HOST,
            private_key=settings.WALLET_PRIVATE_KEY,
            api_key=settings.API_KEY,
            api_secret=settings.API_SECRET,
            api_passphrase=settings.API_PASSPHRASE,
            signature_type=settings.SIGNATURE_TYPE,
            funder_address=settings.FUNDER_ADDRESS,
        )
    clob_ready = clob is not None and clob.is_configured
    # Live was requested but the wallet credentials are missing: fail closed
    # into paper for that strategy instead of crash-looping the container.
    # The dashboard shows the fallback prominently.
    fallbacks: dict[str, str] = {}
    for period, strategy in strategies.items():
        if strategy.is_live and not clob_ready:
            fallbacks[period] = "missing_credentials"
            log.error(
                "strategy.mode_fallback",
                period=period, requested="live", actual="paper",
                note="未配置 CLOB 凭证，已降级为纸面；配置后可在面板切换",
            )
            strategy.TRADING_MODE = "paper"

    def _factory(strategy: Settings):
        def factory(mode: str) -> Broker:
            clone = strategy.model_copy(deep=True)
            clone.TRADING_MODE = mode
            return build_broker(clone, db, clob)
        return factory

    runtimes: dict[str, Runtime] = {}
    try:
        for period, strategy in strategies.items():
            broker = build_broker(strategy, db, clob)
            cache = PriceCache()
            ws = MarketWS(cache) if strategy.USE_MARKET_WS else None
            service = MarketService(pm, cache,
                                    autodetect_fee=strategy.FEE_RATE_AUTODETECT)
            runtime = Runtime(strategy, db, pm, service, broker, ws,
                              broker_factory=_factory(strategy))
            runtime.mode_fallback = fallbacks.get(period, "")
            runtimes[period] = runtime
    except Exception as exc:  # noqa: BLE001
        log.error("boot.broker_failed", error=str(exc))
        return 2

    research = ResearchService(settings, db, strategies)
    state = AppState(settings=settings, db=db, strategies=strategies,
                     runtimes=runtimes, research=research, clob=clob)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _install_signal_handlers(loop, stop_event)

    for period, runtime in runtimes.items():
        await runtime.start()
        log.info("strategy.started", period=period,
                 mode=runtime.broker.mode)

    server = _start_dashboard(state)

    # Prune the event journal once a day; it is only for the UI.
    async def _pruner() -> None:
        while True:
            try:
                db.prune_events(keep_days=14)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(24 * 3600)

    pruner = asyncio.create_task(_pruner(), name="event-pruner")

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("shutdown.begin")
        pruner.cancel()
        try:
            await research.stop()
        except Exception:  # noqa: BLE001
            pass
        for period, runtime in runtimes.items():
            try:
                await runtime.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown.runtime_stop_failed", period=period, error=str(exc))
        if server is not None:
            server.should_exit = True
            await asyncio.sleep(0.3)
        db.close()
        log.info("shutdown.done")
    return 0


def main() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
    started = time.time()
    try:
        code = asyncio.run(run())
    except KeyboardInterrupt:
        code = 0
    log.info("process.exit", code=code, elapsed_s=round(time.time() - started, 1))
    sys.exit(code)


if __name__ == "__main__":
    main()
