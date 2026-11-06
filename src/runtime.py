"""Live orchestration: window lifecycle -> signal -> execution -> settlement.

One asyncio loop drives everything. Per 15-minute window it:

  1. discovers the market for the current slot (retrying until Gamma lists it)
  2. polls the CLOB midpoint from the final `WS_SUBSCRIBE_SEC` window onward
     and records the 1-second series (so the tuning agent can learn from live
     data); earlier display polls are dashboard-only
  3. feeds ticks into `LateFavoriteStrategy` during the last 60 s
  4. on a confirmed signal, runs the risk guards and sends one FAK buy
  5. holds the shares to resolution and books the PnL

No resting orders are ever left on the book, so a crash cannot strand an
order -- the worst case is an open position that resolves on its own.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

from src.config import Settings, strategy_key
from src.execution.broker import Broker, EntryFill, LimitFill, LimitOrder, resolve_fee_rate
from src.execution.clob_client import round_down
from src.infra.clock import fmt_duration, slot_start, utc_iso
from src.infra.logging import get_logger, set_event_hook
from src.market.market_service import MarketService, Quote
from src.market.market_ws import MarketWS
from src.market.models import MarketInfo, Side
from src.market.pm_client import PolymarketClient
from src.persistence.db import DB, compute_stats, equity_curve
from src.research.collector import LiveCollector
from src.strategy.late_favorite import (
    EntryDecision,
    LateFavoriteStrategy,
    Phase,
    StrategyParams,
    taker_fee,
    update_mid_crossing,
    trade_pnl,
)
from src.strategy.risk import RiskManager

log = get_logger(__name__)

# The strategy confirms at a sampled price; by the time the FAK is sent the
# book may have moved. Allow this much downward drift (probability points)
# before treating the signal as reversed -- a collapse between confirm and
# send means the market has re-priced the outcome, not that we got a bargain.
EXEC_REVERSAL_TOLERANCE = 0.02
# At execution time a top-of-book quote older than this is not trusted; the
# order is too sensitive to a stale last-second price.
EXEC_QUOTE_WS_MAX_AGE = 1.0


@dataclass
class ActiveWindow:
    slot: int
    end_ts: int
    market: Optional[MarketInfo] = None
    strategy: Optional[LateFavoriteStrategy] = None
    quote: Optional[Quote] = None
    quote_ts: float = 0.0
    next_discovery_ts: float = 0.0
    signal_recorded: bool = False
    entered: bool = False
    trade_id: int = 0
    last_sample_sec: int = -1
    # Mid-crossing filter state (window-scoped; MAX_MID_CROSSINGS = 0 skips).
    mid_cross_state: int = 0
    mid_cross_count: int = 0
    mid_cross_bucket: int = -1
    entry_window_logged: bool = False
    ws_watched: bool = False
    discovery_error: str = ""
    discovery_task: Optional[asyncio.Task] = None
    discovery_failures: int = 0

    @property
    def entry_start_ts(self) -> float:
        if self.strategy is None:
            return self.end_ts - 60
        return self.strategy.entry_start_ts


class Runtime:
    def __init__(
        self,
        settings: Settings,
        db: DB,
        pm: PolymarketClient,
        market_svc: MarketService,
        broker: Broker,
        ws: Optional[MarketWS] = None,
        broker_factory: Optional[Callable[[str], Broker]] = None,
        ws_shared: bool = False,
        pm_shared: bool = False,
    ) -> None:
        self.settings = settings
        self.db = db
        self.pm = pm
        self.market_svc = market_svc
        self.broker = broker
        self.ws = ws
        # All strategies of one period share a single MarketWS in main.py;
        # stopping one runtime must not tear down the others' connection.
        self.ws_shared = ws_shared
        self.pm_shared = pm_shared
        # Builds a broker for a new mode at runtime (None = switching disabled).
        self._broker_factory = broker_factory
        # Set when live was requested but credentials were missing at boot.
        self.mode_fallback = ""
        self.mode_note = ""
        self.key = strategy_key(settings.ASSET, settings.PERIOD)
        self.asset = settings.ASSET.upper()
        self.period = settings.PERIOD
        self.risk = RiskManager(db, settings, period=settings.PERIOD,
                                mode=broker.mode, asset=self.asset, scope=self.key)
        self.collector: Optional[LiveCollector] = (
            LiveCollector(settings.collect_dir, settings.ASSET, settings.PERIOD)
            if settings.DATA_COLLECT_ENABLED else None
        )
        self.active: Optional[ActiveWindow] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws_started = False
        self._clock: Callable[[], float] = time.time
        self._started_at = self._now()
        self._last_settle_poll = 0.0
        self._last_balance: Optional[float] = None
        self._last_balance_ts = 0.0
        self._last_heartbeat = 0.0
        self._cycle_count = 0
        self._last_error = ""
        self._last_error_ts = 0.0
        self._settle_warned: set[int] = set()
        self._balance_task: Optional[asyncio.Task] = None
        self._settle_task: Optional[asyncio.Task] = None
        # The dashboard polls /api/status every 2 s on this same event loop;
        # recomputing stats over the whole trade history each time would put
        # an unbounded query in front of the last seconds of a window.
        self._stats_cache: Optional[tuple[float, str, dict, list]] = None
        set_event_hook(self._event_sink)

    def _now(self) -> float:
        """Indirection so tests can drive a simulated clock."""
        return self._clock()

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Background task with its exception surfaced in the log.

        None of these may block the tick loop, but a silently dying task
        (settlement, discovery) would be just as bad, so every failure is
        logged instead of waiting for the GC warning.
        """
        task = asyncio.create_task(coro, name=name)

        def _done(finished: asyncio.Task) -> None:
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                log.error("runtime.task_failed", task=name, error=str(exc))

        task.add_done_callback(_done)
        return task

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "ENABLED", True))

    async def _ensure_ws(self) -> None:
        if self.ws is not None and not self._ws_started:
            await self.ws.start()
            self._ws_started = True

    async def set_enabled(self, enabled: bool) -> None:
        """Turn this strategy on/off without restarting the process.

        Disabling stops new discoveries and entries but settlement keeps
        running, so an open position is never stranded. Enabling starts the
        shared market WebSocket if needed and opens a fresh window on the
        next tick.
        """
        enabled = bool(enabled)
        self.settings.ENABLED = enabled
        if enabled:
            await self._ensure_ws()
            # Re-open the current slot so a strategy enabled mid-window does
            # not keep the market object it may have discovered earlier.
            if self.active is not None:
                self._finalize_window(self.active)
                self.active = None
            log.warning("runtime.enabled", strategy=self.key)
            return
        if self.active is not None:
            self._finalize_window(self.active)
            self.active = None
        if self.ws is not None:
            self.ws.unwatch(self.key)
        log.warning("runtime.disabled", strategy=self.key,
                    note="已有持仓仍会结算")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = self._now()
        if self.enabled:
            await self._ensure_ws()
        self._task = asyncio.create_task(self._loop(), name=f"trading-{self.key}")
        log.info(
            "runtime.started",
            strategy=self.key,
            enabled=self.enabled,
            mode=self.settings.TRADING_MODE,
            asset=self.settings.ASSET,
            period=self.settings.PERIOD,
            threshold=self.settings.ENTRY_THRESHOLD,
            confirm_s=self.settings.CONFIRM_SEC,
            max_price=self.settings.MAX_ENTRY_PRICE,
            paper_notional=self.settings.ENTRY_NOTIONAL_USD_PAPER,
            live_notional=self.settings.ENTRY_NOTIONAL_USD_LIVE,
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._balance_task, self._settle_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self.active is not None and self.active.discovery_task is not None:
            if not self.active.discovery_task.done():
                self.active.discovery_task.cancel()
        if self.ws is not None:
            self.ws.unwatch(self.key)
            if not self.ws_shared:
                await self.ws.stop()
        if not self.pm_shared:
            await self.pm.close()
        await self.broker.close()
        log.info("runtime.stopped", strategy=self.key)

    # ── main loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            started = self._now()
            try:
                await self._tick(started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._last_error_ts = self._now()
                log.exception("runtime.tick_failed", error=str(exc))
            elapsed = self._now() - started
            sleep_s = max(0.05, self._sleep_target(self._now()) - elapsed)
            await asyncio.sleep(sleep_s)

    def _ws_lead(self, aw: ActiveWindow) -> float:
        """Seconds before window end when quoting/WS should start."""
        lead = float(self.settings.WS_SUBSCRIBE_SEC or 0.0)
        if aw.strategy is not None:
            # A long entry+confirm window must start earlier than the global
            # default (HYPE 15m: 105s + 10s + 5s buffer).
            lead = max(
                lead,
                float(aw.strategy.params.entry_window_sec)
                + float(aw.strategy.params.confirm_sec) + 5.0,
            )
        return lead

    def _pre_entry_poll_interval(self) -> float:
        """Quote cadence before the entry window.

        Filter-enabled strategies need samples from the window open, but the
        crossing counter only needs one point per ``MID_CROSS_SAMPLE_SEC``:
        1s for 5m, 5s for 15m. Those early samples come from REST; the WS is
        still subscribed only for the final entry window.
        """
        interval = self.settings.POLL_INTERVAL_MS / 1000.0
        if self.settings.MAX_MID_CROSSINGS > 0:
            interval = max(interval, float(self.settings.MID_CROSS_SAMPLE_SEC))
        return interval

    def _sleep_target(self, now: float) -> float:
        if not self.enabled:
            return 2.0
        aw = self.active
        if aw is None or aw.market is None:
            return 1.0
        if now >= aw.end_ts:
            return 0.5
        # Before the short WS window only the dashboard needs prices; a
        # filter strategy also needs one early sample per crossing bucket.
        if now < aw.end_ts - self._ws_lead(aw):
            if self.settings.MAX_MID_CROSSINGS > 0:
                return self._pre_entry_poll_interval()
            return self.settings.DISPLAY_POLL_INTERVAL_MS / 1000.0
        if now >= aw.entry_start_ts:
            return self.settings.ENTRY_POLL_INTERVAL_MS / 1000.0
        return self._pre_entry_poll_interval()

    async def _tick(self, now: float) -> None:
        self._cycle_count += 1
        # A disabled strategy still settles any position opened before it was
        # turned off; it just does not discover markets or open new ones.
        self._maybe_settle(now)
        if not self.enabled:
            return

        period = self.settings.window_seconds
        slot = slot_start(now, period)
        if self.active is None or self.active.slot != slot:
            await self._switch_window(slot, now)
        aw = self.active
        assert aw is not None

        # Settlement and the heartbeat must keep running even while the next
        # window's market is still missing from Gamma. Both do network I/O, so
        # they are scheduled rather than awaited: the last 60 seconds of a
        # window must never queue behind a Gamma or balance request.
        self._maybe_refresh_balance()
        self._log_heartbeat(now)

        if aw.market is None:
            # Discovery is a network call with its own retries; never let it
            # block the tick loop that has to hit the last-60-second window.
            if (aw.discovery_task is None or aw.discovery_task.done()) \
                    and now >= aw.next_discovery_ts:
                aw.discovery_task = self._spawn(
                    self._discover(aw, now), f"discover-{aw.slot}"
                )
            return

        await self._maybe_refresh_quote(aw, now)

    # ── window lifecycle ──────────────────────────────────────────────────

    async def _switch_window(self, slot: int, now: float) -> None:
        previous = self.active
        if previous is not None:
            self._finalize_window(previous)
        period = self.settings.window_seconds
        end_ts = slot + period
        params = StrategyParams(**self.settings.strategy_params())
        aw = ActiveWindow(
            slot=slot,
            end_ts=end_ts,
            strategy=LateFavoriteStrategy(slot, end_ts, params),
        )
        self.active = aw
        log.info(
            "window.opened",
            strategy=self.key, slot=slot, start_utc=utc_iso(slot),
            end_utc=utc_iso(end_ts),
            remaining=fmt_duration(end_ts - now),
        )
        # Kicked off in the background so a slow Gamma call cannot delay the
        # first price tick (or a boot that lands inside the entry window).
        aw.discovery_task = self._spawn(self._discover(aw, now), f"discover-{slot}")

    def _finalize_window(self, aw: ActiveWindow) -> None:
        """Flush remaining samples and record a no-trade signal if needed."""
        if self.ws is not None and aw.ws_watched:
            self.ws.unwatch(self.key)
            aw.ws_watched = False
        state = aw.strategy.state if aw.strategy else None
        samples = self.collector.sample_count(aw.slot) if self.collector else 0
        if not aw.signal_recorded:
            reason = state.skip_reason if state and state.skip_reason else "no_trigger"
            self._record_signal(aw, decision="skip", skip_reason=reason)
        if self.collector is not None:
            resolved = None
            row = self.db.get_window(aw.slot, self.settings.PERIOD, self.asset)
            if row and row.get("resolved"):
                resolved = Side.parse(row["resolved"])
            self.collector.flush_window(aw.slot, aw.market, resolved)
        log.info("window.closed", strategy=self.key, slot=aw.slot,
                 phase=state.phase.value if state else "n/a",
                 skip_reason=state.skip_reason if state else "",
                 entered=aw.entered, samples=samples)

    async def _discover(self, aw: ActiveWindow, now: float) -> None:
        try:
            market = await asyncio.wait_for(
                self.market_svc.discover(
                    self.settings.ASSET, self.settings.PERIOD, aw.slot, force=True
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            aw.discovery_error = "discovery timeout"
            market = None
        except Exception as exc:  # noqa: BLE001
            aw.discovery_error = str(exc)
            market = None
        if market is None:
            aw.discovery_failures += 1
            aw.next_discovery_ts = self._now() + 3.0
            if aw.discovery_failures in (1, 5) or aw.discovery_failures % 20 == 0:
                log.warning("window.discovery_failed", slot=aw.slot,
                            attempts=aw.discovery_failures,
                            error=aw.discovery_error or "market not listed yet",
                            remaining=fmt_duration(aw.end_ts - self._now()))
            # A successful balance/CLOB call can reset the failure counter
            # inside the HTTP client even when Gamma itself is wedged, so
            # also rebuild the client after a long run of discovery misses.
            if aw.discovery_failures % 20 == 0:
                await self.pm.reset_http()
            return
        if self.active is not aw:
            # The window rolled over while Gamma was answering. Applying this
            # market now would repoint the WS watch set at expired tokens.
            log.debug("window.stale_discovery_ignored", slot=aw.slot)
            return
        aw.market = market
        aw.discovery_error = ""
        aw.next_discovery_ts = 0.0
        aw.discovery_failures = 0
        self.db.upsert_window(market)
        # Do not subscribe yet: the shared market stream is expensive and
        # only the collection/entry window needs it. _maybe_refresh_quote
        # subscribes when the window enters that phase.
        log.info(
            "window.market_ready",
            strategy=self.key, slot=aw.slot, slug=market.slug,
            condition_id=market.condition_id[:12],
            remaining=fmt_duration(aw.end_ts - self._now()),
            tick_size=market.tick_size,
        )

    # ── quotes / sampling ─────────────────────────────────────────────────

    async def _maybe_refresh_quote(self, aw: ActiveWindow, now: float) -> None:
        if aw.market is None:
            return
        # The strategy only needs the final WS window. Before it, poll at
        # the slow display interval so the dashboard still has prices but
        # no strategy/collector work happens.
        ws_from = aw.end_ts - self._ws_lead(aw)
        display_only = now < ws_from
        if display_only:
            # Filter strategies need one early-window sample per crossing
            # bucket; taking it via REST is far cheaper than keeping the WS
            # subscribed to every token for the whole window (the server
            # closes slow consumers with 1013 and forces a resubscribe).
            interval = (
                self._pre_entry_poll_interval()
                if self.settings.MAX_MID_CROSSINGS > 0
                else self.settings.DISPLAY_POLL_INTERVAL_MS / 1000.0
            )
        elif now >= aw.entry_start_ts:
            interval = self.settings.ENTRY_POLL_INTERVAL_MS / 1000.0
        else:
            interval = self._pre_entry_poll_interval()
        if not display_only and self.ws is not None and not aw.ws_watched:
            self.ws.watch([aw.market.token_up, aw.market.token_down], owner=self.key)
            aw.ws_watched = True
        if now - aw.quote_ts < interval:
            return

        try:
            quote = await asyncio.wait_for(
                self.market_svc.fetch_quote(
                    aw.market,
                    mids_only=display_only and self.settings.MAX_MID_CROSSINGS > 0,
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            log.warning("market.quote_timeout", slot=aw.slot)
            aw.quote_ts = self._now() - interval + 1.0  # retry on the next tick
            return
        aw.quote = quote
        aw.quote_ts = self._now()

        if display_only:
            # Prices for the dashboard only: not fed to the strategy (which
            # ignores pre-window ticks anyway). Filter strategies still take
            # one sample per bucket for the crossing count and the collector.
            if self.settings.MAX_MID_CROSSINGS > 0:
                self._sample_quote(aw, quote)
            return

        self._sample_quote(aw, quote)

        if aw.strategy is None or aw.strategy.state.done:
            return

        decision = aw.strategy.on_tick(aw.quote_ts, quote.up_mid, quote.down_mid)
        if decision is not None:
            await self._execute(aw, decision, quote, aw.quote_ts)

        state = aw.strategy.state
        if state.phase.value == "SKIPPED" and not aw.signal_recorded:
            self._record_signal(aw, decision="skip", skip_reason=state.skip_reason)
            log.info("signal.skipped", slot=aw.slot, reason=state.skip_reason,
                     trigger_side=state.trigger_side.value if state.trigger_side else None,
                     trigger_price=state.trigger_price or None,
                     peak_up=round(state.peak_up, 4), peak_down=round(state.peak_down, 4))
        elif state.phase.value == "CONFIRMING" and not aw.entry_window_logged:
            aw.entry_window_logged = True
            log.info("signal.armed", slot=aw.slot,
                     side=state.trigger_side.value if state.trigger_side else None,
                     price=round(state.trigger_price, 4),
                     confirm_in_s=self.settings.CONFIRM_SEC)

    def _sample_quote(self, aw: ActiveWindow, quote: Quote) -> None:
        """Feed one quote to the collector and the crossing counter."""
        if self.collector is not None:
            second = int(aw.quote_ts)
            if second != aw.last_sample_sec:
                aw.last_sample_sec = second
                self.collector.add_sample(aw.slot, aw.quote_ts,
                                          quote.up_mid, quote.down_mid)
        self._update_mid_crossings(aw, quote, aw.quote_ts)

    def _update_mid_crossings(self, aw: ActiveWindow, quote: Quote,
                              ts: float) -> None:
        """Sample the UP mid at most once per bucket and count 0.5 flips.

        The counter is window-scoped. ``MAX_MID_CROSSINGS = 0`` makes this a
        no-op, so strategies without the filter keep the cheap tail-only
        quote/WS window.
        """
        settings = self.settings
        if settings.MAX_MID_CROSSINGS <= 0:
            return
        up = quote.up_mid
        if up is None and quote.down_mid is not None:
            up = 1.0 - float(quote.down_mid)
        if up is None or up != up:  # None / NaN
            return
        step = max(1, int(settings.MID_CROSS_SAMPLE_SEC))
        bucket = (int(ts) - int(aw.slot)) // step
        if bucket < 0 or bucket <= aw.mid_cross_bucket:
            return
        aw.mid_cross_bucket = bucket
        state, count = update_mid_crossing(
            aw.mid_cross_state, aw.mid_cross_count, float(up),
            float(settings.MID_CROSS_BAND),
        )
        aw.mid_cross_state = state
        aw.mid_cross_count = count

    # ── execution ─────────────────────────────────────────────────────────

    async def _execute(self, aw: ActiveWindow, decision: EntryDecision,
                       quote: Quote, now: float) -> None:
        settings = self.settings
        market = aw.market
        assert market is not None
        mode = self.broker.mode

        if self.db.trade_exists(aw.slot, mode, self.settings.PERIOD, self.asset):
            aw.signal_recorded = True
            return

        # The confirmed favorite is only a favourite if the market has not
        # been flip-flopping around 0.5 all window. This is a risk filter,
        # not a signal: it skips entries that look like the live losses.
        if settings.MAX_MID_CROSSINGS > 0 \
                and aw.mid_cross_count > settings.MAX_MID_CROSSINGS:
            self._record_signal(
                aw, decision="skip", skip_reason="too_many_mid_crossings",
                decision_obj=decision, confirm_price=decision.price,
            )
            if aw.strategy is not None:
                aw.strategy.state.phase = Phase.SKIPPED
                aw.strategy.state.skip_reason = "too_many_mid_crossings"
            log.info(
                "exec.aborted_mid_crossings", slot=aw.slot,
                side=decision.side.value, crossings=aw.mid_cross_count,
                max_crossings=settings.MAX_MID_CROSSINGS,
                band=settings.MID_CROSS_BAND,
                sample_sec=settings.MID_CROSS_SAMPLE_SEC,
            )
            return

        # Re-read the book immediately before sending: the confirm tick can be
        # up to one poll interval old and the last seconds are volatile.
        try:
            fresh = await asyncio.wait_for(
                self.market_svc.fetch_quote(
                    market, with_books=True,
                    ws_max_age=EXEC_QUOTE_WS_MAX_AGE,
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            log.warning("exec.quote_refresh_timeout", slot=aw.slot,
                        note="using the confirm-tick quote")
            fresh = quote
        aw.quote = fresh
        aw.quote_ts = self._now()
        current = fresh.price(decision.side, settings.SIGNAL_PRICE_SOURCE)

        tolerance = EXEC_REVERSAL_TOLERANCE
        if current is None:
            self._record_signal(aw, decision="skip", skip_reason="exec_price_missing",
                                decision_obj=decision)
            return
        if current >= settings.MAX_ENTRY_PRICE:
            self._record_signal(aw, decision="skip", skip_reason="exec_price_above_cap",
                                decision_obj=decision, confirm_price=current)
            log.warning("exec.aborted_price_above_cap", slot=aw.slot,
                        signal=decision.price, current=current,
                        cap=settings.MAX_ENTRY_PRICE)
            aw.signal_recorded = True
            return
        if current < settings.ENTRY_THRESHOLD - tolerance:
            self._record_signal(aw, decision="skip", skip_reason="exec_price_reversed",
                                decision_obj=decision, confirm_price=current)
            log.warning("exec.aborted_price_reversed", slot=aw.slot,
                        signal=decision.price, current=current,
                        threshold=settings.ENTRY_THRESHOLD)
            aw.signal_recorded = True
            return

        spread = fresh.spread(decision.side)
        if settings.MAX_SPREAD_AT_ENTRY > 0 and spread is not None \
                and spread > settings.MAX_SPREAD_AT_ENTRY:
            self._record_signal(aw, decision="skip", skip_reason="spread_too_wide",
                                decision_obj=decision, confirm_price=current)
            log.warning("exec.aborted_spread", slot=aw.slot, spread=spread,
                        max_spread=settings.MAX_SPREAD_AT_ENTRY)
            aw.signal_recorded = True
            return

        limit = settings.entry_limit_for(decision.price)
        # Paper and live have independent budgets (default 10 / 5 USDC).
        notional = round(float(settings.entry_notional_for(mode)), 2)
        # GTD/FAK orders at the CLOB are share-sized for limit orders; derive
        # the share count from the USDC budget at the maximum price we would
        # pay, so a full fill never costs more than the configured notional.
        shares = round_down(notional / limit, 0.0001) if limit > 0 else 0.0
        if shares <= 0:
            self._record_signal(aw, decision="skip", skip_reason="invalid_order_size",
                                decision_obj=decision, confirm_price=current)
            aw.signal_recorded = True
            return
        ask = fresh.ask(decision.side)

        # Prefer a recent cached balance: a slow balance call must not delay
        # the order while the window is closing.
        if self._last_balance is not None and now - self._last_balance_ts < 15:
            balance = self._last_balance
        else:
            try:
                balance = await asyncio.wait_for(self.broker.balance(), timeout=4.0)
            except Exception:  # noqa: BLE001
                balance = self._last_balance
            self._last_balance = balance
            self._last_balance_ts = self._now()
        risk = self.risk.evaluate(now, notional, balance, mode)
        if not risk.allowed:
            self._record_signal(aw, decision="skip", skip_reason=f"risk:{risk.reason}",
                                decision_obj=decision, confirm_price=current,
                                extra={"risk_detail": risk.detail})
            log.warning("exec.blocked_by_risk", slot=aw.slot, reason=risk.reason,
                        detail=risk.detail)
            aw.signal_recorded = True
            return

        book = fresh.book(decision.side)
        depth = book.depth_at_or_below(limit) if book is not None else None
        if settings.MIN_BOOK_DEPTH_USD > 0 and depth is not None \
                and depth < settings.MIN_BOOK_DEPTH_USD:
            self._record_signal(aw, decision="skip", skip_reason="insufficient_depth",
                                decision_obj=decision, confirm_price=current,
                                extra={"depth": depth})
            aw.signal_recorded = True
            return

        use_gtd = settings.ENTRY_ORDER_TYPE == "gtd" and shares >= 5
        if settings.ENTRY_ORDER_TYPE == "gtd" and not use_gtd:
            log.warning("exec.gtd_below_min", slot=aw.slot, shares=shares,
                        note="GTD 挂单最少 5 股，本次改用 FAK")

        log.info(
            "exec.sending", slot=aw.slot, side=decision.side.value,
            signal_price=round(decision.price, 4),
            exec_price=round(current, 4), limit=limit,
            shares=shares, notional=notional, ask=ask,
            order_type="gtd" if use_gtd else "fak",
            spread=spread, depth=depth, mode=mode,
            mid_crossings=aw.mid_cross_count,
        )

        entry_info: dict = {
            "order_type": "gtd" if use_gtd else "fak",
            "entry_notional_usd": notional,
            "planned_shares": round(shares, 4),
        }
        fill: Optional[EntryFill] = None

        if use_gtd:
            # Rest at the confirmed mid, not at the slippage limit: a buy at
            # the ask would cross and pay taker fees. The FAK fallback uses
            # the full signal+slippage limit.
            rest_price = round_down(decision.price, market.tick_size)
            order = await self.broker.place_limit_buy(
                market, decision.side, shares, rest_price
            )
            if order is None:
                entry_info["fallback"] = "place_failed"
                if settings.ENTRY_FALLBACK_FAK and aw.end_ts - self._now() > 3.0:
                    fill = await self._entry_fak(market, decision, fresh, limit, notional)
                else:
                    self._record_signal(aw, decision="skip", skip_reason="gtd_place_failed",
                                        decision_obj=decision, confirm_price=current,
                                        extra={"limit": limit, "ask": ask})
                    aw.signal_recorded = True
                    return
            else:
                state = await self._wait_limit_fill(order, aw, market)
                entry_info.update({
                    "gtd_price": order.price,
                    "gtd_filled": round(state.filled, 4),
                    "maker": order.maker,
                    "gtd_order_id": order.order_id,
                })
                if state.filled > 0:
                    # Cancel whatever is still resting; we only keep the fill.
                    cancelled = await self.broker.cancel(order)
                    if not cancelled and state.filled < order.shares - 1e-9:
                        # A live remainder could fill after this point and
                        # never be recorded. Stop the strategy and let a
                        # human check the book.
                        log.error("exec.gtd_remainder_cancel_failed",
                                  order_id=order.order_id[:16],
                                  filled=round(state.filled, 4),
                                  shares=order.shares)
                        self.risk.engage_killswitch(
                            f"{self.settings.PERIOD} GTD 部分成交后剩余挂单取消失败，请人工核对"
                        )
                    filled = min(state.filled, order.shares)
                    price = state.avg_price or order.price
                    rate = (settings.MAKER_FEE_RATE if order.maker
                            else resolve_fee_rate(settings, market))
                    fill = EntryFill(
                        True, shares=filled, cost=filled * price, avg_price=price,
                        fee=taker_fee(filled, price, rate), fee_rate=rate,
                        order_id=order.order_id,
                        partial=filled < order.shares - 1e-9,
                        extra={"source": "gtd", "maker": order.maker},
                    )
                else:
                    cancelled = await self.broker.cancel(order)
                    if not cancelled:
                        # Sending a FAK while the GTD may still be live could
                        # double the position.
                        log.error("exec.gtd_cancel_failed_before_fallback",
                                  order_id=order.order_id[:16])
                        self._record_signal(aw, decision="skip",
                                            skip_reason="gtd_cancel_failed",
                                            decision_obj=decision, confirm_price=current,
                                            extra={"gtd_price": order.price, **entry_info})
                        aw.signal_recorded = True
                        self.risk.set_pause(60, "gtd_cancel_failed")
                        return
                    remaining = aw.end_ts - self._now()
                    if settings.ENTRY_FALLBACK_FAK and remaining > 3.0:
                        entry_info["fallback"] = "fak"
                        fill = await self._entry_fak(market, decision, fresh, limit, notional)
                    else:
                        self._record_signal(aw, decision="skip", skip_reason="gtd_unfilled",
                                            decision_obj=decision, confirm_price=current,
                                            extra={"limit": limit, "ask": ask,
                                                   "gtd_price": order.price})
                        log.info("exec.gtd_unfilled", slot=aw.slot,
                                 price=order.price, shares=shares,
                                 remaining=round(remaining, 1))
                        aw.signal_recorded = True
                        return
        else:
            fill = await self._entry_fak(market, decision, fresh, limit, notional)

        if fill is None:
            self._record_signal(aw, decision="skip", skip_reason="execution_missing",
                                decision_obj=decision, confirm_price=current)
            aw.signal_recorded = True
            return

        if not fill.ok:
            # A POST can time out *after* the exchange accepted it, so "error"
            # does not prove there is no position. Check the wallet before
            # pretending nothing happened.
            recovered = await self._reconcile_unknown_fill(aw, decision, limit)
            if recovered is None:
                if fill.error == "ask_above_limit":
                    # Normal for the FAK leg: the book moved away. Not an
                    # execution failure, so no pause.
                    self._record_signal(aw, decision="skip",
                                        skip_reason="ask_above_limit",
                                        decision_obj=decision, confirm_price=current,
                                        extra={"limit": limit, "ask": ask,
                                               **entry_info})
                    aw.signal_recorded = True
                    return
                self._record_signal(aw, decision="skip",
                                    skip_reason=f"execution_failed:{fill.error}",
                                    decision_obj=decision, confirm_price=current,
                                    extra={"limit": limit, "spread": spread,
                                           "depth": depth, "ask": ask, **entry_info})
                log.error("exec.failed", slot=aw.slot, error=fill.error,
                          side=decision.side.value, limit=limit)
                aw.signal_recorded = True
                self.risk.set_pause(60, "execution_failed")
                return
            fill = recovered
            log.error(
                "exec.recovered_from_unknown_fill",
                slot=aw.slot, side=decision.side.value,
                shares=round(fill.shares, 4), note="钱包已出现持仓；已暂停该策略等待人工核对",
            )
            self.risk.engage_killswitch(
                f"{self.settings.PERIOD} 下单返回错误但钱包出现持仓，请人工核对后恢复"
            )

        fee = fill.fee
        if fee is None or fee < 0:
            fee = taker_fee(fill.shares, fill.avg_price, fill.fee_rate)
        # Keep the top of the book on the trade row: it is the only data that
        # can later separate "our signal price was off" from "the book was
        # thin", i.e. recalibrate the backtest's slippage assumption.
        book_at_entry = [
            {"price": level.price, "size": level.size}
            for level in (book.asks[:5] if book is not None else [])
        ]
        extra = {
            "trigger_price": decision.trigger_price,
            "confirm_delay_sec": decision.confirm_delay_sec,
            "confirm_price_delta": decision.confirm_price_delta,
            "mid_crossings": aw.mid_cross_count,
            "mid_cross_band": settings.MID_CROSS_BAND,
            "mid_cross_sample_sec": settings.MID_CROSS_SAMPLE_SEC,
            "limit_price": limit,
            "spread_at_entry": spread,
            "depth_at_limit": depth,
            "best_bid": fresh.bid(decision.side),
            "best_ask": fresh.ask(decision.side),
            "book_asks": book_at_entry,
            "balance_before": balance,
            "partial": fill.partial,
            **entry_info,
            **fill.extra,
        }
        trade_id = self.db.create_trade(
            slot=aw.slot,
            period=self.settings.PERIOD,
            asset=self.asset,
            mode=mode,
            side=decision.side.value,
            token_id=market.token_for(decision.side),
            condition_id=market.condition_id,
            signal_price=decision.price,
            entry_price=fill.avg_price,
            shares=fill.shares,
            cost=fill.cost,
            fee=fee,
            fee_rate=fill.fee_rate,
            stake=notional,
            order_id=fill.order_id,
            status="open",
            entry_ts=now,
            extra=extra,
        )
        aw.entered = True
        aw.trade_id = trade_id
        aw.signal_recorded = True
        self._record_signal(aw, decision="enter", decision_obj=decision,
                            confirm_price=current,
                            extra={"trade_id": trade_id, "fill_price": fill.avg_price,
                                   "shares": fill.shares, "cost": fill.cost,
                                   "fee": fee, "limit": limit, "spread": spread})
        log.info(
            "exec.filled", slot=aw.slot, trade_id=trade_id, mode=mode,
            side=decision.side.value, shares=round(fill.shares, 4),
            cost=round(fill.cost, 4), avg_price=round(fill.avg_price, 5),
            fee=round(fee, 4), fee_rate=fill.fee_rate,
            slippage_vs_signal=round(fill.avg_price - decision.price, 5),
            order_id=fill.order_id[:16],
        )

    async def _entry_fak(self, market: MarketInfo, decision: EntryDecision,
                         quote: Quote, limit: float, notional: float) -> EntryFill:
        """Taker entry spending `notional` USDC, crossing no higher than `limit`."""
        ask = quote.ask(decision.side)
        if ask is not None and ask > limit + 1e-9:
            return EntryFill(False, error="ask_above_limit")
        return await self.broker.buy(
            market=market, side=decision.side, signal_price=decision.price,
            stake=round(float(notional), 2), limit_price=limit, quote=quote,
        )

    async def _wait_limit_fill(self, order, aw: ActiveWindow,
                               market: MarketInfo) -> "LimitFill":
        """Poll a resting order until filled or the wait budget expires.

        This blocks the strategy's own loop for at most GTD_FILL_TIMEOUT_S.
        That is acceptable: the strategy is already in ENTERED for this
        window, and each strategy has its own loop.
        """
        import time as _time

        settings = self.settings
        budget = min(float(settings.GTD_FILL_TIMEOUT_S),
                     max(0.5, aw.end_ts - self._now() - 2.0))
        deadline = _time.monotonic() + budget
        state = LimitFill()
        while True:
            quote = None
            if self.broker.mode == "paper":
                try:
                    quote = await asyncio.wait_for(
                        self.market_svc.fetch_quote(market), timeout=6.0
                    )
                except Exception:  # noqa: BLE001
                    quote = None
            try:
                state = await self.broker.limit_fill(order, quote)
            except Exception as exc:  # noqa: BLE001
                log.warning("exec.limit_poll_failed", error=str(exc),
                            order_id=getattr(order, "order_id", "")[:16])
                state = LimitFill()
            if state.filled >= order.shares - 1e-9 or state.done:
                return state
            if _time.monotonic() >= deadline:
                return state
            await asyncio.sleep(0.4)

    async def _reconcile_unknown_fill(
        self, aw: ActiveWindow, decision: EntryDecision, limit: float
    ) -> Optional[EntryFill]:
        """Did a failed order actually fill? Paper mode cannot lose an order."""
        if self.broker.mode != "live" or aw.market is None:
            return None
        clob = getattr(self.broker, "_clob", None)
        if clob is None:
            return None
        token_id = aw.market.token_for(decision.side)
        try:
            shares = await asyncio.wait_for(clob.get_token_balance(token_id), timeout=5.0)
        except Exception:  # noqa: BLE001
            return None
        if shares is None or shares <= 0.01:
            return None
        price = min(limit, self.settings.EXEC_LIMIT_MAX)
        fee_rate = resolve_fee_rate(self.settings, aw.market)
        cost = shares * price
        return EntryFill(
            True, shares=shares, cost=cost, avg_price=price,
            fee=taker_fee(shares, price, fee_rate), fee_rate=fee_rate,
            order_id="reconciled", extra={"source": "balance_reconcile"},
        )

    def set_mode(self, mode: str) -> tuple[bool, str]:
        """Switch this strategy between paper and live at runtime."""
        mode = (mode or "").strip().lower()
        if mode not in ("paper", "live"):
            return False, "模式必须是 paper 或 live"
        if mode == self.broker.mode:
            return True, ""
        if self.db.open_trades(period=self.settings.PERIOD, asset=self.asset):
            return False, "该策略还有未结算持仓，等结算后再切换"
        if self._broker_factory is None:
            return False, "当前进程未配置切换能力"
        try:
            broker = self._broker_factory(mode)
        except Exception as exc:  # noqa: BLE001
            return False, f"无法切换到 {mode}: {exc}"
        old = self.broker.mode
        self.settings.TRADING_MODE = mode
        self.broker = broker
        self.risk = RiskManager(
            self.db, self.settings, period=self.settings.PERIOD, mode=mode,
            asset=self.asset, scope=self.key,
        )
        self._last_balance = None
        self._last_balance_ts = 0.0
        self._stats_cache = None
        self.mode_fallback = ""
        self.mode_note = ""
        log.warning("runtime.mode_changed", strategy=self.key,
                    old=old, new=mode)
        return True, ""

    # ── settlement ────────────────────────────────────────────────────────

    def _maybe_settle(self, now: float) -> None:
        if now - self._last_settle_poll < self.settings.RESOLUTION_POLL_S:
            return
        if self._settle_task is not None and not self._settle_task.done():
            return
        self._last_settle_poll = now
        self._settle_task = self._spawn(self._settle_open_trades(now), "settle-open-trades")

    async def _settle_open_trades(self, now: float) -> None:
        # Deliberately not filtered by mode: if the operator switches
        # paper/live, any position opened under the previous mode must still
        # settle instead of being stranded forever.
        for trade in self.db.open_trades(period=self.settings.PERIOD,
                                         asset=self.asset):
            slot = int(trade["slot"])
            end_ts = slot + self.settings.window_seconds
            if now < end_ts + 5:
                continue
            condition_id = trade.get("condition_id") or ""
            slug = ""
            row = self.db.get_window(slot, self.settings.PERIOD, self.asset)
            if row:
                slug = row.get("slug") or ""
                condition_id = condition_id or row.get("condition_id") or ""
            try:
                resolved = await self.pm.get_market_resolution(condition_id, slug)
            except Exception as exc:  # noqa: BLE001
                log.debug("settle.resolution_query_failed", slot=slot, error=str(exc))
                resolved = None

            if resolved is None:
                if now - end_ts > self.settings.SETTLE_TIMEOUT_S \
                        and slot not in self._settle_warned:
                    self._settle_warned.add(slot)
                    log.warning("settle.timeout", slot=slot, trade_id=trade["id"],
                                age_s=round(now - end_ts))
                    # A closed market priced ["0","0"] is voided, not
                    # unresolved: the stake is refunded and only the fee is
                    # lost. Without this the trade would stay "open" forever
                    # and keep its capital committed in the paper balance.
                    try:
                        is_void = await self.pm.get_market_void(condition_id, slug)
                    except Exception:  # noqa: BLE001
                        is_void = False
                    if is_void:
                        fee = float(trade.get("fee") or 0)
                        cost = float(trade.get("cost") or 0)
                        self.db.settle_trade(
                            int(trade["id"]), resolved_side="VOID",
                            pnl=-fee, payout=cost, status="void",
                        )
                        log.warning("trade.voided", slot=slot, trade_id=trade["id"],
                                    pnl=round(-fee, 4),
                                    note="市场作废，本金返还，仅损失手续费")
                        self._settle_warned.discard(slot)
                continue

            won = str(trade.get("side")) == resolved.value
            pnl = trade_pnl(
                float(trade.get("shares") or 0),
                float(trade.get("cost") or 0),
                float(trade.get("fee") or 0),
                won,
            )
            payout = float(trade.get("shares") or 0) if won else 0.0
            self.db.settle_trade(int(trade["id"]), resolved_side=resolved,
                                 pnl=pnl, payout=payout)
            self.db.set_window_resolution(slot, self.settings.PERIOD,
                                          self.asset, resolved, now)
            if trade.get("mode") == "live" and won:
                # Winning conditional tokens stay in the wallet until they are
                # redeemed on polymarket.com; surface it instead of pretending
                # the balance already moved.
                self.db.set_redeem_status(int(trade["id"]), "needed")
            if self.collector is not None:
                market = self.market_svc.get_market(slot)
                self.collector.flush_window(slot, market, resolved)
            log.info(
                "trade.settled",
                slot=slot, trade_id=trade["id"], mode=trade.get("mode"),
                side=trade.get("side"), resolved=resolved.value,
                entry_price=round(float(trade.get("entry_price") or 0), 5),
                shares=round(float(trade.get("shares") or 0), 4),
                pnl=round(pnl, 4), won=won,
            )
            self._settle_warned.discard(slot)

    # ── helpers ───────────────────────────────────────────────────────────

    def _record_signal(
        self,
        aw: ActiveWindow,
        *,
        decision: str,
        skip_reason: str = "",
        decision_obj: Optional[EntryDecision] = None,
        confirm_price: Optional[float] = None,
        extra: dict | None = None,
    ) -> None:
        state = aw.strategy.state if aw.strategy else None
        side = decision_obj.side if decision_obj else (state.trigger_side if state else None)
        trigger_ts = decision_obj.trigger_ts if decision_obj else (state.trigger_ts if state else None)
        trigger_price = (
            decision_obj.trigger_price if decision_obj
            else (state.trigger_price if state else None)
        )
        confirm_ts = decision_obj.confirm_ts if decision_obj else None
        if confirm_price is None and decision_obj is not None:
            confirm_price = decision_obj.price
        try:
            self.db.add_signal(
                slot=aw.slot,
                period=self.settings.PERIOD,
                asset=self.asset,
                mode=self.broker.mode,
                decision=decision,
                side=side.value if side else "",
                trigger_ts=trigger_ts,
                trigger_price=trigger_price,
                trigger_remaining=(aw.end_ts - trigger_ts) if trigger_ts else None,
                confirm_due_ts=state.confirm_due_ts if state else None,
                confirm_ts=confirm_ts,
                confirm_price=confirm_price,
                skip_reason=skip_reason,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("signal.persist_failed", error=str(exc))
        if decision == "skip":
            log.info("signal.skip_recorded", slot=aw.slot, reason=skip_reason)
        aw.signal_recorded = True

    def _maybe_refresh_balance(self) -> None:
        if self._balance_task is not None and not self._balance_task.done():
            return
        if self._now() - self._last_balance_ts < 30:
            return
        self._balance_task = self._spawn(self._refresh_balance(), "balance-refresh")

    async def _refresh_balance(self) -> None:
        try:
            value = await asyncio.wait_for(self.broker.balance(), timeout=6.0)
            # None means "query failed", not "wallet is empty"; keep the last
            # good value so the minimum-balance guard stays meaningful.
            if value is not None:
                self._last_balance = value
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._last_balance_ts = self._now()

    def _log_heartbeat(self, now: float) -> None:
        if now - self._last_heartbeat < self.settings.HEARTBEAT_S:
            return
        self._last_heartbeat = now
        aw = self.active
        state = aw.strategy.state if aw and aw.strategy else None
        log.info(
            "heartbeat",
            strategy=self.key,
            mode=self.broker.mode,
            slot=aw.slot if aw else None,
            remaining=fmt_duration(aw.end_ts - now) if aw else None,
            phase=state.phase.value if state else None,
            up=round(aw.quote.up_mid, 4) if aw and aw.quote and aw.quote.up_mid else None,
            down=round(aw.quote.down_mid, 4) if aw and aw.quote and aw.quote.down_mid else None,
            ws=self.ws.connected if self.ws else None,
            balance=round(self._last_balance, 2) if self._last_balance else None,
            cycles=self._cycle_count,
        )

    def _event_sink(self, level: str, event: str, data: dict) -> None:
        self.db.add_event(level, event, data)

    # ── status for the dashboard ──────────────────────────────────────────

    def status(self, *, recent_limit: int = 20) -> dict:
        now = self._now()
        aw = self.active
        mode = self.broker.mode
        trades = self.db.recent_trades(limit=200, mode=mode,
                                       period=self.settings.PERIOD,
                                       asset=self.asset)
        cache = self._stats_cache
        if cache is None or cache[1] != mode or now - cache[0] > 5.0:
            settled = self.db.all_settled_trades(
                mode=mode, period=self.settings.PERIOD, asset=self.asset
            )
            stats = compute_stats(settled)
            equity = equity_curve(settled)[-500:]
            self._stats_cache = (now, mode, stats, equity)
        else:
            stats, equity = cache[2], cache[3]
        window = None
        strategy = None
        quote = None
        if aw is not None:
            state = aw.strategy.state if aw.strategy else None
            window = {
                "slot": aw.slot,
                "end_ts": aw.end_ts,
                "start_utc": utc_iso(aw.slot),
                "end_utc": utc_iso(aw.end_ts),
                "remaining_s": max(0.0, aw.end_ts - now),
                "entry_start_ts": aw.entry_start_ts,
                "in_entry_window": aw.entry_start_ts <= now < aw.end_ts,
                "samples": self.collector.sample_count(aw.slot) if self.collector else 0,
                "market": (
                    {
                        "slug": aw.market.slug,
                        "condition_id": aw.market.condition_id,
                        "token_up": aw.market.token_up,
                        "token_down": aw.market.token_down,
                        "tick_size": aw.market.tick_size,
                        "volume": aw.market.volume,
                    }
                    if aw.market
                    else None
                ),
                "discovery_error": aw.discovery_error,
                "phase": state.phase.value if state else None,
                "entered": aw.entered,
            }
            strategy = aw.strategy.snapshot(now) if aw.strategy else None
            quote = aw.quote.as_dict() if aw.quote else None

        return {
            "now": now,
            "app": {
                "name": self.settings.APP_NAME,
                "mode": mode,
                "paper": self.settings.is_paper,
                "mode_fallback": self.mode_fallback,
                "mode_note": self.mode_note,
                "key": self.key,
                "asset": self.settings.ASSET,
                "period": self.settings.PERIOD,
                "enabled": self.enabled,
                "uptime_s": now - self._started_at,
                "cycles": self._cycle_count,
                "ws_connected": bool(self.ws.connected) if self.ws else False,
                "last_error": self._last_error,
                "last_error_age_s": (now - self._last_error_ts) if self._last_error_ts else None,
            },
            "params": {
                "entry_window_sec": self.settings.ENTRY_WINDOW_SEC,
                "threshold": self.settings.ENTRY_THRESHOLD,
                "confirm_sec": self.settings.CONFIRM_SEC,
                "max_entry_price": self.settings.MAX_ENTRY_PRICE,
                "min_entry_price": self.settings.MIN_ENTRY_PRICE,
                "max_mid_crossings": self.settings.MAX_MID_CROSSINGS,
                "mid_cross_band": self.settings.MID_CROSS_BAND,
                "mid_cross_sample_sec": self.settings.MID_CROSS_SAMPLE_SEC,
                "stake_usd": self.settings.STAKE_USD,
                "entry_notional_usd": float(self.settings.entry_notional_for(mode)),
                "entry_notional_paper": float(self.settings.ENTRY_NOTIONAL_USD_PAPER),
                "entry_notional_live": float(self.settings.ENTRY_NOTIONAL_USD_LIVE),
                "order_type": self.settings.ENTRY_ORDER_TYPE,
                "gtd_timeout_s": self.settings.GTD_FILL_TIMEOUT_S,
                "maker_fee_rate": self.settings.MAKER_FEE_RATE,
                "exec_slippage": self.settings.EXEC_SLIPPAGE,
                "fee_rate": self.settings.TAKER_FEE_RATE,
                "max_spread": self.settings.MAX_SPREAD_AT_ENTRY,
            },
            "window": window,
            "strategy": strategy,
            "quote": quote,
            "risk": self.risk.snapshot(now),
            "balance": self._last_balance,
            "balance_age_s": (now - self._last_balance_ts) if self._last_balance_ts else None,
            "stats": stats,
            "equity": equity,
            "recent_trades": trades[:recent_limit],
            "recent_signals": self.db.recent_signals(
                limit=recent_limit, mode=mode, period=self.settings.PERIOD,
                asset=self.asset,
            ),
            "unredeemed": self.db.unredeemed_trades(self.settings.PERIOD,
                                                    self.asset),
            "dataset": {
                "collect_enabled": self.settings.DATA_COLLECT_ENABLED,
                "collect_dir": str(self.settings.collect_dir),
            },
        }
