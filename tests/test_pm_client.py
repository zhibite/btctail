"""The shared Gamma/CLOB client must recover from a wedged connection pool."""
from __future__ import annotations

import httpx
import pytest

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
