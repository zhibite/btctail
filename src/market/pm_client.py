"""Polymarket Gamma + CLOB public REST client.

Only read-only endpoints live here; order placement is in
``src/execution/clob_client.py``.

The discovery shapes mirror the production-proven implementation in the
sibling ``02.polygtd`` bot: Gamma's recurring up/down markets are looked up
by event slug, and the Up/Down token ids are mapped from the market's
``outcomes`` array instead of assuming a fixed positional order.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from src.infra.logging import get_logger
from src.market.models import BookLevel, MarketInfo, OrderBook, Side

log = get_logger(__name__)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "resolved")


def _load_json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _parse_ts(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 1e12 else value
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
        return value / 1000.0 if value > 1e12 else value
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class PolymarketClient:
    """Async wrapper over Gamma (market metadata) and CLOB (prices/books)."""

    def __init__(
        self,
        *,
        gamma_api: str = "https://gamma-api.polymarket.com",
        clob_host: str = "https://clob.polymarket.com",
    ) -> None:
        self.gamma_api = gamma_api.rstrip("/")
        self.clob_host = clob_host.rstrip("/")
        self._http: Optional[httpx.AsyncClient] = None
        self._resolution_cache: dict[str, tuple[float, dict]] = {}
        self._fee_cache: dict[str, tuple[float, float]] = {}

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"User-Agent": "polytail-btc15/0.1"},
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _get(self, url: str, params: dict | None = None, timeout: float = 8.0,
                   retries: int = 2) -> Any:
        client = await self._client()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await client.get(url, params=params, timeout=timeout)
                if resp.status_code == 429 and attempt < retries:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < retries:
                    # Keep this short: discovery runs inside the trading loop.
                    await asyncio.sleep(0.15 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed: {last_exc}") from last_exc

    # ── Discovery ─────────────────────────────────────────────────────────

    async def get_event_markets(self, slug: str) -> list[dict]:
        """All markets inside the Gamma event with this slug (usually 1)."""
        try:
            data = await self._get(f"{self.gamma_api}/events", {"slug": slug},
                                   timeout=6.0, retries=1)
        except Exception as exc:  # noqa: BLE001
            log.debug("gamma.events_failed", slug=slug, error=str(exc))
            return []
        events = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        markets: list[dict] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if slug and slug not in str(event.get("slug") or ""):
                continue
            for market in event.get("markets") or []:
                if isinstance(market, dict):
                    market["_event_slug"] = event.get("slug") or slug
                    markets.append(market)
        return markets

    @staticmethod
    def parse_market(market: dict, asset: str, period: str, slot: int) -> Optional[MarketInfo]:
        """Normalise a Gamma market object into MarketInfo."""
        token_ids = _load_json_list(market.get("clobTokenIds"))
        outcomes = _load_json_list(market.get("outcomes"))
        if len(token_ids) < 2:
            log.warning("gamma.market_missing_tokens", slug=market.get("slug"))
            return None

        def index_of(label: str) -> Optional[int]:
            aliases = ("up", "yes") if label == "up" else ("down", "no")
            for idx, outcome in enumerate(outcomes):
                if str(outcome).strip().lower() in aliases:
                    return idx
            return None

        idx_up = index_of("up")
        idx_down = index_of("down")
        if idx_up is None or idx_down is None:
            if outcomes:
                log.warning("gamma.unexpected_outcomes", outcomes=outcomes,
                            slug=market.get("slug"))
                return None
            # Gamma omitted `outcomes`; the recurring series is documented as
            # [Up, Down] and every observed payload has followed it.
            log.debug("gamma.outcomes_missing", slug=market.get("slug"),
                      note="assuming clobTokenIds[0]=Up, [1]=Down")
            idx_up, idx_down = 0, 1

        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        if not condition_id:
            return None

        end_ts = _parse_ts(market.get("endDate") or market.get("end_date"))
        if end_ts is None:
            end_ts = slot + (300 if period == "5m" else 900)

        fee_rate = None
        for key in ("feeRate", "fee_rate"):
            if market.get(key) not in (None, "", "null"):
                try:
                    fee_rate = float(market[key])
                    break
                except (TypeError, ValueError):
                    pass

        try:
            volume = float(market.get("volume") or market.get("volumeNum") or 0.0)
        except (TypeError, ValueError):
            volume = 0.0

        return MarketInfo(
            asset=asset.upper(),
            period=period,
            slot=slot,
            slug=str(market.get("_event_slug") or market.get("slug") or ""),
            condition_id=condition_id,
            token_up=str(token_ids[idx_up]),
            token_down=str(token_ids[idx_down]),
            end_ts=int(end_ts),
            tick_size=0.01,
            neg_risk=_as_bool(market.get("negRisk")),
            fee_rate=fee_rate,
            volume=volume,
        )

    async def discover_slot(self, asset: str, period: str, slot: int) -> Optional[MarketInfo]:
        """Fetch the market for a specific slot (`{asset}-updown-{period}-{slot}`)."""
        slug = f"{asset.lower()}-updown-{period}-{slot}"
        markets = await self.get_event_markets(slug)
        for market in markets:
            info = self.parse_market(market, asset, period, slot)
            if info is not None:
                return info
        return None

    # ── Prices ────────────────────────────────────────────────────────────

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        if not token_id:
            return None
        try:
            data = await self._get(
                f"{self.clob_host}/midpoint", {"token_id": token_id}, timeout=3.0, retries=0
            )
            value = float(data.get("mid"))
            return value if 0.0 < value < 1.0 else None
        except Exception:  # noqa: BLE001
            return None

    async def get_midpoints(self, token_ids: list[str]) -> dict[str, float]:
        """Midpoints for several tokens, resilient to individual failures."""
        results = await asyncio.gather(
            *(self.get_midpoint(t) for t in token_ids), return_exceptions=True
        )
        out: dict[str, float] = {}
        for token_id, result in zip(token_ids, results):
            if isinstance(result, float):
                out[token_id] = result
        return out

    async def get_last_trade_prices(self, token_ids: list[str]) -> dict[str, float]:
        if not token_ids:
            return {}
        try:
            data = await self._get(
                f"{self.clob_host}/last-trades-prices",
                {"token_ids": ",".join(token_ids)},
                timeout=4.0,
                retries=1,
            )
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, float] = {}
        if isinstance(data, dict):
            for token_id, value in data.items():
                if token_id not in token_ids:
                    continue
                try:
                    out[token_id] = float(value)
                except (TypeError, ValueError):
                    continue
        elif isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                token_id = entry.get("token_id") or entry.get("asset_id")
                if not token_id or token_id not in token_ids:
                    continue
                try:
                    out[str(token_id)] = float(entry.get("price") or 0.0)
                except (TypeError, ValueError):
                    continue
        return out

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        try:
            data = await self._get(
                f"{self.clob_host}/book", {"token_id": token_id}, timeout=4.0, retries=1
            )
        except Exception:  # noqa: BLE001
            return None
        return self._parse_book(token_id, data)

    async def get_order_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        results = await asyncio.gather(
            *(self.get_order_book(t) for t in token_ids), return_exceptions=True
        )
        out: dict[str, OrderBook] = {}
        for token_id, result in zip(token_ids, results):
            if isinstance(result, OrderBook):
                out[token_id] = result
        return out

    @staticmethod
    def _parse_book(token_id: str, data: Any) -> Optional[OrderBook]:
        if not isinstance(data, dict):
            return None
        book = OrderBook(token_id=token_id, ts=time.time())
        for raw in data.get("bids") or []:
            try:
                book.bids.append(BookLevel(float(raw["price"]), float(raw["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        for raw in data.get("asks") or []:
            try:
                book.asks.append(BookLevel(float(raw["price"]), float(raw["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        book.bids.sort(key=lambda level: level.price, reverse=True)
        book.asks.sort(key=lambda level: level.price)
        return book

    async def get_tick_size(self, token_id: str) -> float:
        try:
            data = await self._get(
                f"{self.clob_host}/tick-size", {"token_id": token_id}, timeout=4.0, retries=0
            )
            value = float(data.get("minimum_tick_size") or 0.01)
            return value if value > 0 else 0.01
        except Exception:  # noqa: BLE001
            return 0.01

    def _prune_caches(self, now: float) -> None:
        # Tokens change every window; without this both maps grow for the
        # life of the process. Entries are cheap but there is no reason to
        # keep hundreds of thousands of them.
        if len(self._fee_cache) > 2000:
            for token_id in [t for t, (ts, _) in self._fee_cache.items()
                             if now - ts > 3600]:
                self._fee_cache.pop(token_id, None)
        if len(self._resolution_cache) > 5000:
            for condition_id in [c for c, (ts, _) in self._resolution_cache.items()
                                 if now - ts > 6 * 3600]:
                self._resolution_cache.pop(condition_id, None)

    async def get_fee_rate(self, token_id: str) -> Optional[float]:
        """Effective taker fee rate for a token.

        Polymarket reports ``base_fee`` in basis points on the CLOB. Crypto
        markets are currently 700 bps (0.07). A 0/missing value is not
        evidence of a zero fee for a category that charges one, so the
        caller falls back to its configured rate in that case.
        """
        if not token_id:
            return None
        cached = self._fee_cache.get(token_id)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        try:
            data = await self._get(
                f"{self.clob_host}/fee-rate", {"token_id": token_id}, timeout=4.0, retries=0
            )
            bps = data.get("base_fee")
            if bps is None:
                bps = data.get("fee_rate_bps")
            if bps is None:
                return None
            value = float(bps) / 10_000.0
            if value <= 0 or value >= 0.5:
                return None
            now = time.time()
            self._prune_caches(now)
            self._fee_cache[token_id] = (now, value)
            return value
        except Exception:  # noqa: BLE001
            return None

    # ── Resolution ────────────────────────────────────────────────────────

    @staticmethod
    def parse_resolution(market: dict | None) -> Optional[Side]:
        """UP/DOWN winner, or None when the market is not resolved yet.

        Accepts the three shapes Gamma has used: explicit ``resolved``, UMA
        ``umaResolutionStatus == resolved``, or a closed market whose two
        outcome prices are terminal (exactly 0 and 1). A voided market's
        ["0","0"] is explicitly *not* a resolution.
        """
        if not isinstance(market, dict):
            return None
        prices = _load_json_list(market.get("outcomePrices"))
        try:
            nums = [float(p) for p in prices]
        except (TypeError, ValueError):
            nums = []
        terminal = len(nums) >= 2 and sorted(nums[:2]) == [0.0, 1.0]
        uma = str(market.get("umaResolutionStatus") or "").strip().lower()
        resolved = _as_bool(market.get("resolved")) or (
            terminal and (_as_bool(market.get("closed")) or uma == "resolved")
        )
        if not resolved:
            return None

        outcomes = _load_json_list(market.get("outcomes"))
        up_idx = None
        if len(outcomes) == len(nums):
            for idx, label in enumerate(outcomes):
                if str(label).strip().lower() in ("up", "yes"):
                    up_idx = idx
                    break
        if up_idx is None:
            up_idx = 0
        if up_idx >= len(nums):
            return None
        return Side.UP if nums[up_idx] >= 0.5 else Side.DOWN

    async def get_market_resolution(
        self, condition_id: str, slug: str, *, max_age_s: float = 30.0
    ) -> Optional[Side]:
        cached = self._resolution_cache.get(condition_id)
        if cached and time.time() - cached[0] < max_age_s:
            return cached[1]  # type: ignore[return-value]

        market: dict | None = None
        # 1) direct condition-id lookup
        try:
            data = await self._get(
                f"{self.gamma_api}/markets", {"condition_ids": [condition_id]}, timeout=6.0,
                retries=0,
            )
            if isinstance(data, list) and data:
                market = data[0]
            elif isinstance(data, dict):
                market = data
        except Exception:  # noqa: BLE001
            pass

        side = self.parse_resolution(market)
        if side is None:
            # 2) resolved recurring markets often disappear from /markets but
            #    remain visible on the event payload.
            try:
                for candidate in await self.get_event_markets(slug):
                    cid = str(candidate.get("conditionId") or "").lower()
                    if cid and cid != condition_id.lower():
                        continue
                    found = self.parse_resolution(candidate)
                    if found is not None:
                        side = found
                        break
            except Exception:  # noqa: BLE001
                pass

        if side is not None:
            now = time.time()
            self._prune_caches(now)
            self._resolution_cache[condition_id] = (now, side)
        return side

    async def get_market_void(self, condition_id: str, slug: str = "") -> bool:
        """True when the market closed without a winner (["0","0"]).

        Polymarket refunds voided markets; a trade on one should be settled
        as a void (stake returned, fee kept) rather than left open forever.
        """
        market: dict | None = None
        try:
            data = await self._get(
                f"{self.gamma_api}/markets", {"condition_ids": [condition_id]},
                timeout=6.0, retries=0,
            )
            if isinstance(data, list) and data:
                market = data[0]
            elif isinstance(data, dict):
                market = data
        except Exception:  # noqa: BLE001
            pass
        if market is None and slug:
            for candidate in await self.get_event_markets(slug):
                cid = str(candidate.get("conditionId") or "").lower()
                if cid and cid != condition_id.lower():
                    continue
                market = candidate
                break
        if not isinstance(market, dict):
            return False
        if not _as_bool(market.get("closed")):
            return False
        prices = _load_json_list(market.get("outcomePrices"))
        try:
            nums = [float(p) for p in prices]
        except (TypeError, ValueError):
            return False
        return len(nums) >= 2 and sorted(nums[:2]) == [0.0, 0.0]

    async def get_clob_market(self, condition_id: str) -> Optional[dict]:
        try:
            return await self._get(f"{self.clob_host}/markets/{condition_id}", timeout=6.0,
                                   retries=0)
        except Exception:  # noqa: BLE001
            return None
