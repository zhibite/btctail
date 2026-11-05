"""Unit tests for the 0.5-crossing counter with a deadband."""
from src.strategy.late_favorite import update_mid_crossing


def test_first_excursion_is_not_a_crossing():
    state, count = update_mid_crossing(0, 0, 0.53, 0.02)
    assert (state, count) == (1, 0)


def test_deadband_ignores_noise():
    state, count = update_mid_crossing(0, 0, 0.53, 0.02)
    state, count = update_mid_crossing(state, count, 0.51, 0.02)
    assert (state, count) == (1, 0)
    state, count = update_mid_crossing(state, count, 0.49, 0.02)
    assert (state, count) == (1, 0)
    state, count = update_mid_crossing(state, count, 0.47, 0.02)
    assert (state, count) == (-1, 1)
    state, count = update_mid_crossing(state, count, 0.53, 0.02)
    assert (state, count) == (1, 2)


def test_zero_band_counts_every_flip():
    state = count = 0
    for up in (0.51, 0.49, 0.52, 0.48):
        state, count = update_mid_crossing(state, count, up, 0.0)
    assert count == 3


def test_nan_keeps_state():
    state, count = update_mid_crossing(1, 2, float("nan"), 0.02)
    assert (state, count) == (1, 2)
