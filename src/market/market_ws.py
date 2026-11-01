"""Polymarket CLOB market-data WebSocket.

Subscribes to the public market channel and maintains per-token:
  * ``last_trade``  — most recent executed price
  * ``best_bid`` / ``best_ask`` — top of book when the server sends it
  * ``book``        — full snapshot when a ``book`` event arrives

The REST poller in ``market_service`` remains the authoritative midpoint
source for the strategy signal (that is the series the backtest used); the
WebSocket is used for live dashboard prices and for a fast order book read
at entry time.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets
from websockets.protocol import State as WsState

from src.infra.logging import get_logger
from src.market.models import BookLevel, OrderBook

log = get_logger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class PriceCache:
    last_trade: dict[str, float] = field(default_factory=dict)
    best_bid: dict[str, float] = field(default_factory=dict)
    best_ask: dict[str, float] = field(default_factory=dict)
    book: dict[str, OrderBook] = field(default_factory=dict)
    updated_at: dict[str, float] = field(default_factory=dict)

    def mid(self, token_id: str) -> Optional[float]:
        bid = self.best_bid.get(token_id)
        ask = self.best_ask.get(token_id)
        if bid is not None and ask is not None and ask > 0:
            return (bid + ask) / 2.0
        return None

    def clear(self, token_ids: list[str]) -> None:
        for token_id in token_ids:
            for table in (self.last_trade, self.best_bid, self.best_ask, self.book,
                          self.updated_at):
                table.pop(token_id, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_trade": dict(self.last_trade),
            "best_bid": dict(self.best_bid),
            "best_ask": dict(self.best_ask),
        }


class MarketWS:
    """Reconnecting market-channel consumer."""

    def __init__(self, cache: PriceCache) -> None:
        self.cache = cache
        self._tokens: set[str] = set()
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        self._connected = False
        self._last_message_ts = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_age(self) -> float | None:
        if not self._last_message_ts:
            return None
        import time

        return time.time() - self._last_message_ts

    def watch(self, token_ids: list[str]) -> None:
        """Replace the watch set with the current window's tokens.

        The cache is pruned for dropped tokens and events for unknown tokens
        are ignored, so memory and bandwidth stay bounded to one window no
        matter how many windows the process has seen.
        """
        wanted = {t for t in token_ids if t}
        if wanted == self._tokens:
            return
        dropped = self._tokens - wanted
        if dropped:
            self.cache.clear(sorted(dropped))
        self._tokens = wanted
        if self._connected and self._ws is not None:
            asyncio.create_task(self._safe_send(self._ws, sorted(wanted)))

    # Backwards-compatible alias.
    subscribe = watch

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="market-ws")
        log.info("market_ws.started", url=MARKET_WS_URL)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._connected = False
        log.info("market_ws.stopped")

    # ── internals ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        delay = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    MARKET_WS_URL, ping_interval=10, ping_timeout=20, close_timeout=5
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = 1.0
                    log.info("market_ws.connected")
                    if self._tokens:
                        await self._safe_send(ws, sorted(self._tokens))
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("market_ws.error", error=str(exc), retry_in_s=delay)
            finally:
                self._connected = False
                self._ws = None
            if not self._running:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _safe_send(self, ws: Any, token_ids: list[str]) -> None:
        payload = {
            "type": "market",
            "assets_ids": token_ids,
            "custom_feature_enabled": True,
        }
        async with self._send_lock:
            try:
                if getattr(ws, "state", None) == WsState.OPEN:
                    await ws.send(json.dumps(payload))
                    log.info("market_ws.subscribed", tokens=len(token_ids))
            except Exception as exc:  # noqa: BLE001
                log.debug("market_ws.subscribe_failed", error=str(exc))

    def _handle(self, raw: str | bytes) -> None:
        import time

        self._last_message_ts = time.time()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        # The initial snapshot arrives as a JSON array of book objects.
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    self._handle_event(item)
            return
        if isinstance(payload, dict):
            self._handle_event(payload)

    def _handle_event(self, msg: dict) -> None:
        event_type = msg.get("event_type") or msg.get("type") or ""
        asset_id = str(msg.get("asset_id") or msg.get("token") or "")

        if event_type in ("book", "price_change") and msg.get("bids") is not None:
            if self._watches(asset_id):
                self._ingest_book(asset_id, msg)

        if event_type == "tick_size_change":
            return

        if event_type == "last_trade_price" and self._watches(asset_id):
            self._set(self.cache.last_trade, asset_id, msg.get("price"))

        for change in msg.get("price_changes") or []:
            if not isinstance(change, dict):
                continue
            token = str(change.get("asset_id") or asset_id)
            if not self._watches(token):
                continue
            self._set(self.cache.best_bid, token, change.get("best_bid"))
            self._set(self.cache.best_ask, token, change.get("best_ask"))
            if change.get("price") is not None:
                self._set(self.cache.last_trade, token, change.get("price"))

        if self._watches(asset_id):
            for key, table in (("best_bid", self.cache.best_bid),
                               ("best_ask", self.cache.best_ask)):
                if msg.get(key) is not None:
                    self._set(table, asset_id, msg.get(key))

    def _watches(self, token_id: str) -> bool:
        # An empty watch set means "before the first subscription"; accept
        # everything then so a reconnect snapshot is not discarded.
        return not self._tokens or token_id in self._tokens

    def _ingest_book(self, token_id: str, msg: dict) -> None:
        if not token_id:
            return
        book = OrderBook(token_id=token_id)
        for raw in msg.get("bids") or []:
            try:
                book.bids.append(BookLevel(float(raw["price"]), float(raw["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        for raw in msg.get("asks") or []:
            try:
                book.asks.append(BookLevel(float(raw["price"]), float(raw["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        book.bids.sort(key=lambda level: level.price, reverse=True)
        book.asks.sort(key=lambda level: level.price)
        if book.bids or book.asks:
            self.cache.book[token_id] = book
            if book.best_bid is not None:
                self.cache.best_bid[token_id] = book.best_bid
            if book.best_ask is not None:
                self.cache.best_ask[token_id] = book.best_ask

    @staticmethod
    def _set(table: dict[str, float], token_id: str, value: Any) -> None:
        if not token_id or value in (None, ""):
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if 0.0 <= number <= 1.0:
            table[token_id] = number
