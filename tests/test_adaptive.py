"""Outdoor-adaptive cooling: target interpolation, setpoint table edges, floor, switch, fallback."""
from dataclasses import replace

import pytest

from minisplit_controller.config import Settings, Tuning, validate
from minisplit_controller.logic import (adaptive_target, cool_floor, cool_target, decide, default_state,
                                        heat_to_cool_f, outdoor_fresh)

M = 60
T = Tuning()


def st(**kw):
    s = default_state(0)
    s.update(kw)
    return s


def test_target_interpolation():
    assert adaptive_target(None, T) is None
    assert adaptive_target(40, T) == 70.5 and adaptive_target(55, T) == 70.5
    assert adaptive_target(60, T) == 71.1 and adaptive_target(67.5, T) == 72.0
    assert adaptive_target(80, T) == 73.5 and adaptive_target(95, T) == 73.5
    assert adaptive_target(70, replace(T, adaptive_cool=False)) is None


@pytest.mark.parametrize("off, sp", [(-0.6, 78), (0.0, 77), (0.6, 76), (1.2, 75), (1.8, 74), (2.4, 73), (3.0, 72)])
def test_setpoint_table_edges(off, sp):
    tgt = adaptive_target(50, T)                       # 70.5, floor 71, so nothing is clamped
    assert cool_target(tgt + off, T, 50) == sp         # Tp exactly on the edge takes that row (<=)
    assert cool_target(tgt + off + 0.01, T, 50) == sp - 1   # just past it falls to the next row


def test_setpoint_table_ends():
    assert cool_target(60.0, T, 50) == 78               # far below target
    assert cool_target(70.5 + 5, T, 50) == 71           # past the last row


def test_floor_boundaries():
    assert cool_floor(None, T) == 73
    assert cool_floor(60, T) == 71 and cool_floor(65, T) == 71 and cool_floor(67.9, T) == 71
    assert cool_floor(68, T) == 73 and cool_floor(70, T) == 73
    assert cool_target(80.0, T, 85) == 73               # hot day: clamped at 73
    assert cool_target(76.0, T, 65) == 71               # mild day (< 68F outside): floor 71
    assert cool_target(73.5, T, 85) == 77


def test_fallback_table_without_outdoor():
    assert [cool_target(x, T) for x in (71.4, 72.0, 72.6, 73.2, 73.8, 74.5)] == [78, 77, 76, 75, 74, 73]


def test_outdoor_fresh_and_stale():
    assert outdoor_fresh({}, 10_000, T) is None
    assert outdoor_fresh({"outdoor_f": 50, "outdoor_ts": 0}, 2 * 3600, T) == 50.0
    assert outdoor_fresh({"outdoor_f": 50, "outdoor_ts": 0}, 2 * 3600 + 1, T) is None


def test_heat_to_cool_threshold():
    assert heat_to_cool_f(None, T) == 73.0
    assert heat_to_cool_f(50, T) == 72.0                # 70.5 + 1.5
    assert heat_to_cool_f(85, T) == 73.0                # capped at heat_to_cool_f


def _hist(temp, mins):
    return [[t * M, temp] for t in mins]


def test_switch_dwell_20_min_at_lowered_threshold():
    now = 4 * 3600
    base = dict(regime_since=0, last_ok_ts=now - 10 * M, outdoor_f=50, outdoor_ts=now - 5 * M)
    held = _hist(72.1, [now // M - 30, now // M - 20, now // M - 10])
    assert decide(72.1, st(history=held, **base), now)[1]["regime"] == "cool"
    short = _hist(72.1, [now // M - 15, now // M - 10, now // M - 5])     # held only 15 min
    assert decide(72.1, st(history=short, last_ok_ts=now - 5 * M, regime_since=0,
                           outdoor_f=50, outdoor_ts=now - 5 * M), now)[1]["regime"] == "heat"
    below = _hist(71.9, [now // M - 30, now // M - 20, now // M - 10])    # under 72.0
    assert decide(71.9, st(history=below, **base), now)[1]["regime"] == "heat"


@pytest.mark.parametrize("extra", [{}, {"outdoor_f": 50, "outdoor_ts": 0}])   # missing / 4 h stale
def test_missing_or_stale_outdoor_falls_back(extra):
    now = 4 * 3600
    held = _hist(72.1, [now // M - 30, now // M - 20, now // M - 10])
    _, s2, info, _ = decide(72.1, st(history=held, regime_since=0, last_ok_ts=now - 10 * M, **extra), now)
    assert s2["regime"] == "heat" and info["h2c"] == 73.0 and info["cool_target"] is None
    d, _, info, _ = decide(73.2, st(regime="cool", regime_since=0, last_ok_ts=now - 10 * M, **extra), now)
    assert d["cool_setpoint"] == 75 and info["floor"] == 73


@pytest.mark.parametrize("field, value", [
    ("cool_target_lo_f", 74.0), ("cool_target_out_hi_f", 50.0), ("cool_min", 74),
    ("cool_offsets", [(0.0, 77), (-0.6, 78)]), ("cool_offsets", [(0.0, 90)]),
    ("cool_floor_table", [(60.0, 60)]), ("heat_to_cool_margin_f", -1.0), ("cool_offsets_else", 70),
    ("cool_to_heat_f", 72.5),        # would meet the lowered heat -> cool entry (70.5 + 1.5)
])
def test_validation(field, value):
    validate(Settings())
    s = Settings()
    setattr(s.tuning, field, value)
    with pytest.raises(ValueError):
        validate(s)
