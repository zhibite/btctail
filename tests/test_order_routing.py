"""Live order routing: v2 markets sign against Exchange V3 via position ids."""
from __future__ import annotations

import pytest

from src.config import Settings
from src.execution.broker import LimitOrder, LiveBroker
from src.execution.clob_client import ClobClient, FillResult, OrderAck
from src.market.models import MarketInfo, Side


class FakeClob:
    def __init__(self) -> None:
        self.limit_calls: list[dict] = []
        self.market_calls: list[dict] = []
        self.order_state: dict = {}
        self.fills: tuple[float, float] | None = None
        self.fill_calls = 0

    async def get_order(self, order_id):
        return self.order_state

    async def get_order_fills(self, order_id, token_id):
        self.fill_calls += 1
        return self.fills

    async def place_limit_buy(self, token_id, shares, price, tick_size, **kwargs):
        self.limit_calls.append(dict(
            token_id=token_id, shares=shares, price=price,
            tick_size=tick_size, **kwargs,
        ))
        return OrderAck(True, order_id="0xlimit", placed_size=shares, price=price)

    async def place_fak_buy(self, token_id, notional, limit_price, tick_size, **kwargs):
        self.market_calls.append(dict(
            token_id=token_id, notional=notional, limit_price=limit_price,
            tick_size=tick_size, **kwargs,
        ))
        return FillResult(True, order_id="0xfak", shares=notional / limit_price,
                          cost=notional, avg_price=limit_price)


class FakeSdk:
    def __init__(self) -> None:
        self.limit_orders: list[tuple] = []
        self.market_orders: list[tuple] = []

    def create_and_post_order(self, args, **kwargs):
        self.limit_orders.append((args, kwargs))
        return {"orderID": "0xfeed", "status": "live"}

    def create_and_post_market_order(self, args, **kwargs):
        self.market_orders.append((args, kwargs))
        return {"orderID": "0xbeef", "takingAmount": "5", "makingAmount": "4.7"}


def market(**overrides) -> MarketInfo:
    data = dict(
        asset="DOGE", period="5m", slot=0, slug="s", condition_id="c",
        token_up="token-up", token_down="token-down", end_ts=300,
        position_up="position-up", position_down="position-down",
        tick_size=0.01,
    )
    data.update(overrides)
    return MarketInfo(**data)


def live_broker(tmp_path) -> LiveBroker:
    settings = Settings(
        DATA_DIR=str(tmp_path), TAKER_FEE_RATE=0.01,
        TAKER_FEE_RATE_BTC_5M15M=0.05, FEE_RATE_AUTODETECT=False,
    )
    return LiveBroker(settings, FakeClob())


def sdk_client() -> tuple[ClobClient, FakeSdk]:
    client = ClobClient(host="http://clob.invalid")
    fake = FakeSdk()
    client._client = fake
    return client, fake


async def test_live_buy_routes_v2_market_by_position_id(tmp_path):
    broker = live_broker(tmp_path)
    fill = await broker.buy(market(), Side.UP, 0.92, 5.0, 0.94)

    assert fill.ok
    call = broker._clob.market_calls[0]
    assert call["token_id"] == "token-up"
    assert call["position_id"] == "position-up"
    assert call["tick_size"] == pytest.approx(0.01)


async def test_live_limit_routes_v2_market_by_position_id(tmp_path):
    broker = live_broker(tmp_path)
    order = await broker.place_limit_buy(market(), Side.DOWN, 5.3, 0.94)

    assert order is not None
    call = broker._clob.limit_calls[0]
    assert call["token_id"] == "token-down"
    assert call["position_id"] == "position-down"
    assert call["neg_risk"] is False
    assert call["post_only"] is True


async def test_live_buy_keeps_token_id_path_for_legacy_market(tmp_path):
    broker = live_broker(tmp_path)
    legacy = market(position_up="", position_down="")
    await broker.buy(legacy, Side.UP, 0.92, 5.0, 0.94)

    assert broker._clob.market_calls[0]["position_id"] == ""


async def test_clob_limit_order_signs_v2_market_with_position_id():
    client, fake = sdk_client()
    ack = await client.place_limit_buy(
        "token-up", 5.3, 0.94, 0.01, position_id="position-up", post_only=False
    )

    assert ack.ok
    args, kwargs = fake.limit_orders[0]
    assert args.position_id == "position-up"
    assert args.token_id is None
    assert kwargs["post_only"] is False


async def test_clob_limit_order_uses_token_id_without_position_id():
    client, fake = sdk_client()
    ack = await client.place_limit_buy("token-up", 5.3, 0.94, 0.01)

    assert ack.ok
    args, _ = fake.limit_orders[0]
    assert args.token_id == "token-up"
    assert args.position_id is None


async def test_clob_market_order_signs_v2_market_with_position_id():
    client, fake = sdk_client()
    fill = await client.place_fak_buy(
        "token-up", 5.0, 0.94, 0.01, position_id="position-up"
    )

    assert fill.filled
    args, _ = fake.market_orders[0]
    assert args.position_id == "position-up"
    assert args.token_id is None


async def test_limit_fill_uses_the_actual_average_price(tmp_path):
    broker = live_broker(tmp_path)
    broker._clob.order_state = {"status": "matched", "size_matched": "5.23"}
    broker._clob.fills = (5.23, 0.901)
    order = LimitOrder(order_id="0x1", token_id="token-up", side=Side.UP,
                       price=0.95, shares=5.23)

    fill = await broker.limit_fill(order)

    assert fill.filled == pytest.approx(5.23)
    assert fill.avg_price == pytest.approx(0.901)
    assert broker._clob.fill_calls == 1


async def test_limit_fill_falls_back_to_the_limit_price(tmp_path):
    broker = live_broker(tmp_path)
    broker._clob.order_state = {"status": "matched", "size_matched": "5.23"}
    broker._clob.fills = None
    order = LimitOrder(order_id="0x2", token_id="token-up", side=Side.UP,
                       price=0.95, shares=5.23)

    fill = await broker.limit_fill(order)

    assert fill.avg_price == pytest.approx(0.95)


class FakeTradesSdk:
    def __init__(self, trades):
        self.trades = trades

    def get_trades(self, params, only_first_page=False):
        return self.trades


async def test_clob_order_fills_sums_only_our_taker_order():
    client = ClobClient(host="http://clob.invalid")
    client._client = FakeTradesSdk([
        {"taker_order_id": "0xabc", "size": "5.23", "price": "0.9"},
        {"taker_order_id": "0xabc", "size": "1.0", "price": "0.91"},
        {"taker_order_id": "0xother", "size": "9.0", "price": "0.5"},
    ])

    fills = await client.get_order_fills("0xabc", "token-up")

    assert fills is not None
    shares, avg = fills
    assert shares == pytest.approx(6.23)
    assert avg == pytest.approx((5.23 * 0.9 + 0.91) / 6.23)


async def test_clob_order_fills_none_without_a_match():
    client = ClobClient(host="http://clob.invalid")
    client._client = FakeTradesSdk([
        {"taker_order_id": "0xother", "size": "1", "price": "0.5"},
    ])
    assert await client.get_order_fills("0xabc", "token-up") is None
