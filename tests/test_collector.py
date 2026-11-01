"""Live samples must be readable by the same loader the research uses."""
from __future__ import annotations

from pathlib import Path

from src.market.models import MarketInfo, Side
from src.research.collector import LiveCollector
from src.research.dataset import clear_series_cache, load_series

SLOT = (1_770_000_000 // 900) * 900
END = SLOT + 900


def test_collector_roundtrip(tmp_path: Path):
    market = MarketInfo(
        asset="BTC", period="15m", slot=SLOT, slug=f"btc-updown-15m-{SLOT}",
        condition_id="0xdeadbeef", token_up="u", token_down="d", end_ts=END,
    )
    collector = LiveCollector(tmp_path, "BTC", "15m")
    for second in range(120):
        collector.add_sample(SLOT, END - 120 + second, 0.5, 0.5)
    assert collector.sample_count(SLOT) == 120
    written = collector.flush_window(SLOT, market, Side.UP)
    assert written == 120
    assert collector.sample_count(SLOT) == 0

    clear_series_cache()
    series = load_series(tmp_path, "btc", "15m", None, None, 300)
    assert len(series) == 1
    item = series[0]
    assert item.slug == market.slug
    assert item.end_ts == END
    assert item.resolved == "UP"
    assert len(item.times) == 120
    assert item.times[0] == END - 120
    assert item.times[-1] == END - 1

    # Flushing again with resolution info must not duplicate the price rows.
    collector.flush_window(SLOT, market, Side.UP)
    clear_series_cache()
    again = load_series(tmp_path, "btc", "15m", None, None, 300)
    assert len(again[0].times) == 120
