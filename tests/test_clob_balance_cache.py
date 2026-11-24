"""One shared CLOB balance lookup per TTL across all fourteen strategies."""
from __future__ import annotations

import asyncio
import threading
import time as real_time

import pytest

import src.execution.clob_client as clob_mod
from src.execution.clob_client import ClobClient


class FakeClock:
    """Stands in for clob_client's `time` module (monotonic only)."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class FakeSdk:
    def __init__(self, balance: float = 42.5, error: str | None = None,
                 delay: float = 0.0) -> None:
        self.balance = balance
        self.error = error
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def get_balance_allowance(self, params) -> dict:
        with self._lock:
            self.calls += 1
        if self.delay:
            real_time.sleep(self.delay)
        if self.error is not None:
            raise RuntimeError(self.error)
        return {"balance": str(int(self.balance * clob_mod.CLOB_BASE_UNITS))}


def make_client(fake: FakeSdk) -> ClobClient:
    client = ClobClient(host="http://clob.invalid")
    client._client = fake
    return client


@pytest.fixture()
def clock(monkeypatch) -> FakeClock:
    fake_clock = FakeClock()
    monkeypatch.setattr(clob_mod, "time", fake_clock)
    return fake_clock


async def test_fourteen_concurrent_callers_share_one_lookup(clock):
    fake = FakeSdk(balance=79.05, delay=0.05)
    client = make_client(fake)

    results = await asyncio.gather(*(client.get_balance() for _ in range(14)))

    assert fake.calls == 1
    assert results == pytest.approx([79.05] * 14)


async def test_value_is_reused_until_the_ttl_expires(clock):
    fake = FakeSdk(balance=10.0)
    client = make_client(fake)

    assert await client.get_balance() == pytest.approx(10.0)
    clock.now += clob_mod.BALANCE_CACHE_TTL_S - 0.1
    assert await client.get_balance() == pytest.approx(10.0)
    assert fake.calls == 1

    fake.balance = 12.0
    clock.now += 0.2
    assert await client.get_balance() == pytest.approx(12.0)
    assert fake.calls == 2


async def test_a_failed_lookup_is_not_retried_by_the_waiting_callers(clock):
    fake = FakeSdk(error="ConnectionTerminated")
    client = make_client(fake)

    results = await asyncio.gather(*(client.get_balance() for _ in range(14)))

    assert results == [None] * 14
    assert fake.calls == 1

    # Still inside the TTL: no new attempt even though nothing was cached.
    assert await client.get_balance() is None
    assert fake.calls == 1


async def test_failure_returns_the_last_known_balance_then_recovers(clock):
    fake = FakeSdk(balance=79.05)
    client = make_client(fake)
    assert await client.get_balance() == pytest.approx(79.05)

    fake.error = "ConnectionTerminated"
    clock.now += clob_mod.BALANCE_CACHE_TTL_S + 0.1
    assert await client.get_balance() == pytest.approx(79.05)
    assert fake.calls == 2

    # The failed attempt also starts a new TTL window.
    assert await client.get_balance() == pytest.approx(79.05)
    assert fake.calls == 2

    fake.error = None
    fake.balance = 80.5
    clock.now += clob_mod.BALANCE_CACHE_TTL_S + 0.1
    assert await client.get_balance() == pytest.approx(80.5)
    assert fake.calls == 3
