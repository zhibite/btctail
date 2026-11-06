"""Full-window integration: switch -> sample -> signal -> fill -> settle."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.config import Settings
from src.execution.broker import Broker, EntryFill
from src.market.market_service import Quote
from src.market.models import MarketInfo, Side
from src.persistence.db import DB
from src.runtime import Runtime

# Must be aligned to the 900 s boundary that the runtime uses.
SLOT = (1_770_000_000 // 900) * 900
END = SLOT + 900


class FakePM:
    async def get_market_resolution(self, condition_id, slug):
        return Side.UP

    async def close(self):
        return None


class FakeMarketService:
    def __init__(self, clock):
        self.clock = clock
        self.markets: dict[int, MarketInfo] = {}

    async def discover(self, asset, period, slot, force=False):
        market = MarketInfo(
            asset="BTC", period="15m", slot=slot,
            slug=f"btc-updown-15m-{slot}", condition_id=f"0x{slot}",
            token_up=f"up-{slot}", token_down=f"down-{slot}", end_ts=slot + 900,
            tick_size=0.01,
        )
        self.markets[slot] = market
        return market

    def get_market(self, slot):
        # MarketService.get_market is synchronous; keep the fake faithful.
        return self.markets.get(slot)

    async def fetch_quote(self, market, with_books=False, ws_max_age=None, mids_only=False):
        now = self.clock()
        up = 0.92 if now >= END - 50 else 0.50
        return Quote(ts=now, up_mid=up, down_mid=round(1 - up, 4),
                     up_bid=round(up - 0.01, 4), up_ask=round(up + 0.01, 4))


class FakeBroker(Broker):
    mode = "paper"

    async def balance(self):
        return 1000.0

    async def buy(self, market, side, signal_price, stake, limit_price, quote=None):
        price = min(limit_price, signal_price + 0.005)
        shares = stake / price
        fee = shares * 0.07 * price * (1 - price)
        return EntryFill(True, shares=shares, cost=stake, avg_price=price,
                         fee=fee, fee_rate=0.07, order_id="fake")


async def test_full_window_flow(tmp_path: Path):
    clock = [END - 300.0]
    settings = Settings(
        DATA_DIR=str(tmp_path), DATA_COLLECT_ENABLED=True, COLLECT_SEC=300,
        MAX_SPREAD_AT_ENTRY=0.10, MIN_WALLET_BALANCE_USD=0, HEARTBEAT_S=3600,
        RESOLUTION_POLL_S=1,
        ENTRY_WINDOW_SEC=60, ENTRY_THRESHOLD=0.90, CONFIRM_SEC=10,
        MAX_ENTRY_PRICE=0.94,
    )
    db = DB(tmp_path / "flow.db")
    svc = FakeMarketService(lambda: clock[0])
    runtime = Runtime(settings, db, FakePM(), svc, FakeBroker(), ws=None)
    runtime._clock = lambda: clock[0]
    runtime._last_settle_poll = 0.0

    # Window switch: discovery is a background task.
    await runtime._tick(clock[0])
    assert runtime.active is not None
    await runtime.active.discovery_task
    assert runtime.active.market is not None

    # Walk the window second by second.
    for offset in range(299, -2, -1):
        clock[0] = END - offset
        await runtime._tick(clock[0])
        if offset == 40:
            assert len(db.recent_trades()) == 1, "entry should have happened"

    trade = db.recent_trades()[0]
    assert trade["side"] == "UP"
    assert trade["status"] in ("open", "won")
    # Settlement runs a few seconds after the boundary; this tick also
    # switches to the next window and flushes the old one's samples.
    runtime._settle_task = None
    runtime._last_settle_poll = 0.0
    clock[0] = END + 5
    await runtime._tick(clock[0])
    assert runtime._settle_task is not None, "settlement should be scheduled"
    await runtime._settle_task
    assert db.recent_trades()[0]["status"] == "won"
    assert runtime.active.slot == SLOT + 900
    if runtime.active.discovery_task is not None:
        await runtime.active.discovery_task

    files = list((tmp_path / "dataset").rglob("klines_fine.csv"))
    assert files, "collector should have written samples"
    with files[0].open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    # Collection starts with the final WS window (120 s), not the full
    # 300 s display period.
    assert len(rows) > 100
    assert {row["slug"] for row in rows} == {f"btc-updown-15m-{SLOT}"}
    assert all(float(row["up_price"]) > 0 for row in rows)

    markets_files = list((tmp_path / "dataset").rglob("markets.csv"))
    assert markets_files
    db.close()
