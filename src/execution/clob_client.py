"""Thin async wrapper around py_clob_client_v2.

The bot only ever sends **taker** orders:

  * entry  — FAK BUY sized in USDC, marketable up to a hard price limit
  * exit   — only used by the optional manual "flatten" control; the strategy
             itself holds to resolution

`py_clob_client_v2` is synchronous, so every SDK call is pushed through
`asyncio.to_thread` to keep the trading loop responsive.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from src.infra.logging import get_logger

log = get_logger(__name__)

CLOB_BASE_UNITS = 1_000_000
POLYGON_CHAIN_ID = 137


def round_down(value: float, tick: float) -> float:
    if value <= 0:
        return 0.0
    tick_d = Decimal(str(max(tick, 0.0001)))
    return float(Decimal(str(value)).quantize(tick_d, rounding=ROUND_DOWN))


@dataclass
class OrderAck:
    ok: bool
    order_id: str = ""
    placed_size: float = 0.0
    price: float = 0.0
    error: str = ""


@dataclass
class FillResult:
    success: bool
    order_id: str = ""
    shares: float = 0.0
    cost: float = 0.0
    avg_price: float = 0.0
    fee: Optional[float] = None      # exchange-reported fee, when present
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.success and self.shares > 0 and self.cost > 0


class ClobClient:
    def __init__(
        self,
        *,
        host: str,
        private_key: str = "",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        signature_type: int = 0,
        funder_address: str = "",
    ) -> None:
        self.host = host.rstrip("/")
        self._private_key = private_key
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._signature_type = int(signature_type)
        self._funder = funder_address
        self._client: Any = None
        self._funder_resolved = ""
        self._init_error = ""
        self._init()

    # ── init ──────────────────────────────────────────────────────────────

    def _init(self) -> None:
        if not self._private_key and not (self._api_key and self._api_secret):
            self._init_error = "missing credentials"
            log.warning("clob.not_configured",
                        note="set API_KEY/API_SECRET/API_PASSPHRASE or WALLET_PRIVATE_KEY")
            return
        if not self._private_key:
            # L2 credentials without a signing key cannot place orders; the
            # CLOB requires an L1 signature for every order payload.
            self._init_error = "L2 credentials need WALLET_PRIVATE_KEY to sign orders"
            log.warning("clob.no_private_key", note=self._init_error)
            return
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient as SdkClient

            try:
                from eth_account import Account

                eoa = Account.from_key(self._private_key).address if self._private_key else ""
            except Exception:  # noqa: BLE001
                eoa = ""
            funder = self._funder or eoa
            self._funder_resolved = funder

            creds = None
            if self._api_key and self._api_secret:
                creds = ApiCreds(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    api_passphrase=self._api_passphrase,
                )
            client = SdkClient(
                host=self.host,
                chain_id=POLYGON_CHAIN_ID,
                key=self._private_key,
                creds=creds,
                signature_type=self._signature_type,
                funder=funder,
            )
            if creds is None:
                creds = client.create_or_derive_api_key()
                client.set_api_creds(creds)
                log.info("clob.derived_api_creds", api_key=(creds.api_key or "")[:12])
            self._client = client
            log.info("clob.ready", host=self.host, signature_type=self._signature_type,
                     funder=funder or "(empty)")
        except Exception as exc:  # noqa: BLE001
            self._init_error = str(exc)
            log.error("clob.init_failed", error=str(exc))

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    @property
    def init_error(self) -> str:
        return self._init_error

    @property
    def funder_address(self) -> str:
        return self._funder_resolved or self._funder

    async def _run(self, fn, *args, timeout: float = 20.0, **kwargs):
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)

    # ── balances ──────────────────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """Collateral (USDC) balance. None on error -- never coerced to 0."""
        if self._client is None:
            return None
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            resp = await self._run(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            if not isinstance(resp, dict) or resp.get("balance") is None:
                return None
            return float(resp["balance"]) / CLOB_BASE_UNITS
        except Exception as exc:  # noqa: BLE001
            log.warning("clob.get_balance_failed", error=str(exc))
            return None

    async def get_token_balance(self, token_id: str) -> Optional[float]:
        """Shares held for an outcome token. None on error."""
        if self._client is None or not token_id:
            return None
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            resp = await self._run(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id),
            )
            if not isinstance(resp, dict) or resp.get("balance") is None:
                return None
            return float(resp["balance"]) / CLOB_BASE_UNITS
        except Exception as exc:  # noqa: BLE001
            log.debug("clob.get_token_balance_failed", error=str(exc))
            return None

    # ── orders ────────────────────────────────────────────────────────────

    async def place_fak_buy(
        self, token_id: str, notional: float, limit_price: float, tick_size: float
    ) -> FillResult:
        """Marketable BUY of `notional` USDC, never paying above `limit_price`."""
        if self._client is None:
            return FillResult(False, error=self._init_error or "clob not configured")

        price = round_down(limit_price, tick_size)
        amount = round_down(notional, 0.01)
        if price <= 0 or amount <= 0:
            return FillResult(False, error="invalid amount/price")

        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType

            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(amount),
                side="BUY",
                price=float(price),
                order_type=OrderType.FAK,
            )
            resp = await self._run(
                self._client.create_and_post_market_order, args,
                order_type=OrderType.FAK, timeout=25.0,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "no orders found to match" in message.lower():
                log.warning("clob.fak_buy_no_fill", token=token_id[:16], amount=amount, price=price)
                return FillResult(False, error="no_match")
            log.warning("clob.fak_buy_failed", token=token_id[:16], error=message,
                        amount=amount, price=price)
            return FillResult(False, error=message)

        result = self._parse_response(resp, price)
        if result.filled:
            log.info(
                "clob.fak_buy_filled",
                token=token_id[:16], shares=round(result.shares, 4),
                cost=round(result.cost, 4), avg_price=round(result.avg_price, 5),
                order_id=result.order_id[:16],
            )
        else:
            log.warning("clob.fak_buy_empty", token=token_id[:16],
                        error=result.error, price=price, amount=amount,
                        raw=str(resp)[:300])
        return result

    async def place_fak_sell(
        self, token_id: str, shares: float, limit_price: float, tick_size: float
    ) -> FillResult:
        """Optional flatten: marketable SELL of `shares` at >= `limit_price`."""
        if self._client is None:
            return FillResult(False, error=self._init_error or "clob not configured")
        price = round_down(limit_price, tick_size)
        size = round_down(shares, 0.0001)
        if price <= 0 or size <= 0:
            return FillResult(False, error="invalid size/price")
        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType

            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(size),
                side="SELL",
                price=float(price),
                order_type=OrderType.FAK,
            )
            resp = await self._run(
                self._client.create_and_post_market_order, args,
                order_type=OrderType.FAK, timeout=25.0,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            log.warning("clob.fak_sell_failed", token=token_id[:16], error=message)
            return FillResult(False, error=message)
        result = self._parse_response(resp, price)
        if result.filled:
            log.info("clob.fak_sell_filled", token=token_id[:16],
                     shares=round(result.shares, 4), cost=round(result.cost, 4),
                     avg_price=round(result.avg_price, 5))
        return result

    @staticmethod
    def _parse_response(resp: Any, limit_price: float) -> FillResult:
        if not isinstance(resp, dict):
            return FillResult(False, error=f"unexpected response: {type(resp).__name__}")

        order_id = str(resp.get("orderID") or resp.get("order_id") or "")
        taking = _float(resp.get("takingAmount"))
        making = _float(resp.get("makingAmount"))
        shares = 0.0
        cost = 0.0
        # BUY: taking = shares received, making = USDC spent.
        if taking is not None and making is not None and taking > 0 and making > 0:
            shares, cost = taking, making
        else:
            for fill in resp.get("fills") or []:
                if not isinstance(fill, dict):
                    continue
                size = _float(fill.get("size"))
                price = _float(fill.get("price"))
                if size and price and size > 0 and price > 0:
                    shares += size
                    cost += size * price

        shares, cost = _normalize_units(shares, cost)
        if shares <= 0 or cost <= 0:
            return FillResult(False, order_id=order_id, error="no_fill", raw=resp)

        fee = _float(resp.get("fee") or resp.get("feeAmount") or resp.get("fee_amount"))
        if fee is not None and fee > 0:
            # A taker fee is a tiny fraction of the notional; anything larger
            # than half the cost is a base-unit value (6 decimals), not USDC.
            if cost > 0 and fee > cost * 0.5 and fee > 100:
                fee /= CLOB_BASE_UNITS
        avg_price = cost / shares
        if not math.isfinite(avg_price) or avg_price <= 0 or avg_price > 1.0:
            return FillResult(False, order_id=order_id, error="bad_avg_price", raw=resp)
        return FillResult(
            True, order_id=order_id, shares=shares, cost=cost,
            avg_price=avg_price, fee=fee, raw=resp,
        )

    async def place_limit_buy(
        self,
        token_id: str,
        shares: float,
        price: float,
        tick_size: float,
        *,
        neg_risk: bool = False,
        post_only: bool = True,
    ) -> OrderAck:
        """Rest a GTC limit BUY for `shares` at `price`.

        The CLOB enforces a 5-share minimum on resting orders, so anything
        smaller is refused here (the caller can fall back to FAK).
        """
        if self._client is None:
            return OrderAck(False, error=self._init_error or "clob not configured")
        price_q = round_down(price, tick_size)
        size_q = round_down(shares, tick_size)
        if price_q <= 0 or size_q <= 0:
            return OrderAck(False, error="invalid size/price")
        if size_q < 5.0:
            return OrderAck(False, error="GTD order needs at least 5 shares")
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions

            args = OrderArgs(
                token_id=token_id,
                price=float(price_q),
                size=float(size_q),
                side="BUY",
            )
            options = PartialCreateOrderOptions(
                tick_size=str(tick_size), neg_risk=bool(neg_risk)
            )
            resp = await self._run(
                self._client.create_and_post_order,
                args,
                options=options,
                order_type=OrderType.GTC,
                post_only=bool(post_only),
                timeout=25.0,
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            log.warning("clob.limit_buy_failed", token=token_id[:16],
                        error=message, price=price_q, size=size_q)
            return OrderAck(False, error=message)

        if not isinstance(resp, dict):
            return OrderAck(False, error=f"unexpected response: {type(resp).__name__}")
        order_id = str(resp.get("orderID") or resp.get("order_id") or "")
        if not order_id:
            return OrderAck(False, error=str(
                resp.get("errorMsg") or resp.get("error") or "no orderID"
            ))
        log.info("clob.limit_buy_placed", token=token_id[:16], order_id=order_id[:16],
                 price=price_q, size=size_q, post_only=post_only,
                 status=resp.get("status"))
        return OrderAck(True, order_id=order_id, placed_size=size_q, price=price_q)

    async def cancel_order(self, order_id: str) -> bool:
        if self._client is None or not order_id:
            return False
        try:
            from py_clob_client_v2 import OrderPayload

            await self._run(
                self._client.cancel_order, OrderPayload(orderID=order_id), timeout=15.0
            )
            # A non-raising cancel is success for this API shape; the batch
            # endpoint (cancel_orders) is the one that returns a split.
            log.info("clob.order_cancelled", order_id=order_id[:16])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("clob.cancel_failed", order_id=order_id[:16], error=str(exc))
            return False

    async def get_order(self, order_id: str) -> Optional[dict]:
        if self._client is None or not order_id:
            return None
        try:
            resp = await self._run(self._client.get_order, order_id, timeout=10.0)
            return resp if isinstance(resp, dict) else None
        except Exception:  # noqa: BLE001
            return None


def _normalize_units(shares: float, cost: float) -> tuple[float, float]:
    """Return (shares, cost) in human units.

    A BUY response normally reports shares and USDC directly, but some
    deployments return 6-decimal base units. A plain "> 1e6" test misses the
    small orders this bot sends (a 1 USDC fill is 1_000_000 base units at
    most), so try the plausible scalings and keep the one that yields a
    coherent price and share count.
    """
    def sane(s: float, c: float) -> bool:
        return s > 0 and c > 0 and 0.0 < c / s < 1.0 and s < 1_000_000

    candidates = [(shares, cost)]
    if shares > 0 and cost > 0:
        candidates.extend([
            (shares / CLOB_BASE_UNITS, cost / CLOB_BASE_UNITS),
            (shares, cost / CLOB_BASE_UNITS),
            (shares / CLOB_BASE_UNITS, cost),
        ])
    for s, c in candidates:
        if sane(s, c):
            return s, c
    return shares, cost


def _float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
