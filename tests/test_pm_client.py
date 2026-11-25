"""The shared Gamma/CLOB client must recover from a wedged connection pool."""
from __future__ import annotations

import httpx
import pytest

from src.market.models import Side
from src.market.pm_client import PolymarketClient


class _FailingClient:
    def __init__(self) -> None:
        self.is_closed = False
        self.closed = False

    async def get(self, *args, **kwargs):
        raise httpx.ConnectTimeout("pool wedged")

    async def aclose(self) -> None:
        self.closed = True
        self.is_closed = True


class _PoolFailingClient(_FailingClient):
    async def get(self, *args, **kwargs):
        raise httpx.PoolTimeout("all connections busy")


async def test_http_client_reset_after_repeated_transport_failures():
    client = PolymarketClient()
    fake = _FailingClient()
    client._http = fake

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await client._get("https://example.invalid", retries=0)

    assert fake.closed
    assert client._http is None
    assert client._failures == 0


async def test_pool_timeouts_do_not_trigger_a_client_reset():
    client = PolymarketClient()
    fake = _PoolFailingClient()
    client._http = fake

    for _ in range(4):
        with pytest.raises(RuntimeError):
            await client._get("https://example.invalid", retries=0)

    # Pool queueing is load, not a wedged socket: keep the pool and do not
    # count it towards the reset threshold.
    assert not fake.closed
    assert client._http is fake
    assert client._failures == 0


async def test_reset_is_a_noop_when_the_client_is_already_gone():
    client = PolymarketClient()
    await client.reset_http()
    await client.reset_http()
    assert client._http is None


async def test_stale_reset_generation_leaves_the_new_pool_alone():
    client = PolymarketClient()
    old = _FailingClient()
    client._http = old
    await client.reset_http()
    assert old.closed
    assert client._http is None

    new = _FailingClient()
    client._http = new
    client._http_gen = 1
    await client.reset_http(generation=0)  # a request that used the old pool
    assert not new.closed
    assert client._http is new


def test_parse_market_legacy_keeps_token_ids_and_real_tick():
    client = PolymarketClient()
    market = client.parse_market({
        "conditionId": "0xabc",
        "clobTokenIds": '["11", "22"]',
        "outcomes": '["Up", "Down"]',
        "orderPriceMinTickSize": 0.001,
        "endDate": "2026-11-24T07:00:00Z",
        "negRisk": False,
    }, "ETH", "15m", 0)

    assert market is not None
    assert market.uses_position_ids is False
    assert market.tick_size == pytest.approx(0.001)
    assert market.token_for(Side.UP) == "11"
    assert market.token_for(Side.DOWN) == "22"


def test_parse_market_v2_maps_position_ids_by_outcome():
    client = PolymarketClient()
    market = client.parse_market({
        "conditionId": "0xdef",
        "clobTokenIds": '["down-token", "up-token"]',
        "positionIds": ["down-position", "up-position"],
        "outcomes": '["Down", "Up"]',
        "orderPriceMinTickSize": 0.01,
        "endDate": "2026-11-24T07:00:00Z",
        "negRisk": False,
    }, "DOGE", "5m", 0)

    assert market is not None
    assert market.uses_position_ids is True
    assert market.token_for(Side.UP) == "up-token"
    assert market.position_for(Side.UP) == "up-position"
    assert market.position_for(Side.DOWN) == "down-position"
    assert market.tick_size == pytest.approx(0.01)
