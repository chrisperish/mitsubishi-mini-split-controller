"""Outdoor-adaptive cool target, floor, heat -> cool threshold, fallback without outdoor data, validation."""
import math
from dataclasses import replace

import pytest

from minisplit_controller import config
from minisplit_controller.config import Settings, Tuning, validate
from minisplit_controller.logic import (adaptive_target, cool_floor, cool_target, cool_tgt, decide, default_state,
                                        heat_to_cool_f, outdoor_fresh)

M = 60
T = Tuning()


def st(**kw):
    s = default_state(0)
    s["fan_stages_v"] = 2
    s.update(kw)
    return s


def test_target_interpolation_and_clamping():
    assert adaptive_target(None, T) is None
    assert adaptive_target(40, T) == 70.0 and adaptive_target(55, T) == 70.0      # flat below 55F
    assert adaptive_target(60, T) == 70.7 and adaptive_target(67.5, T) == 71.75
    assert adaptive_target(80, T) == 73.5 and adaptive_target(95, T) == 73.5      # flat above 80F
    assert adaptive_target(70, replace(T, adaptive_cool=False)) is None
    assert cool_tgt(None, T) == 72.0 and cool_tgt(70, replace(T, adaptive_cool=False)) == 72.0


def test_floor_is_ceil_target_minus_margin():
    for out in (40, 55, 58, 62, 67.5, 67.9, 68, 72, 80, 95, None):
        tgt = 72.0 if out is None else adaptive_target(out, T)
        assert cool_floor(out, T) == math.ceil(tgt - 1.5)
    assert (cool_floor(50, T), cool_floor(None, T), cool_floor(80, T), cool_floor(67.5, T)) == (69, 71, 72, 71)


def test_setpoint_examples():
    # no outdoor: target 72.0, floor 71
    assert [cool_target(x, T) for x in (68.0, 70.0, 71.0, 72.0, 72.5, 74.5)] == [78, 75, 74, 72, 71, 71]
    # cold (target 70.0, floor 69)
    assert [cool_target(x, T, 50) for x in (68.0, 69.0, 69.5, 70.0, 70.5, 71.0, 72.5)] == [73, 72, 71, 70, 69, 69, 69]
    # hot (target 73.5, floor 72)
    assert cool_target(73.5, T, 85) == 74 and cool_target(76.0, T, 85) == 72 and cool_target(72.0, T, 85) == 76


def test_outdoor_fresh_and_stale():
    assert outdoor_fresh({}, 10_000, T) is None
    assert outdoor_fresh({"outdoor_f": 50, "outdoor_ts": 0}, 2 * 3600, T) == 50.0
    assert outdoor_fresh({"outdoor_f": 50, "outdoor_ts": 0}, 2 * 3600 + 1, T) is None


def test_heat_to_cool_threshold():
    assert heat_to_cool_f(None, T) == 73.0
    assert heat_to_cool_f(50, T) == 71.5                # 70.0 + 1.5
    assert heat_to_cool_f(85, T) == 73.0                # capped at heat_to_cool_f


def _hist(temp, mins):
    return [[t * M, temp] for t in mins]


def test_switch_at_lowered_threshold_and_fallback():
    warm = _hist(72.2, (30, 40, 50))
    s = st(regime_since=0, history=warm, last_ok_ts=50 * M, outdoor_f=50, outdoor_ts=55 * M)
    d, s2, _, _ = decide(72.2, s, 61 * M)
    assert s2["regime"] == "cool" and d["cool_setpoint"] == 69     # target 70.0: u 66.7 -> floor 69
    assert decide(72.2, st(regime_since=0, history=warm, last_ok_ts=50 * M), 61 * M)[1]["regime"] == "heat"
    stale = st(regime_since=0, history=warm, last_ok_ts=50 * M, outdoor_f=50, outdoor_ts=-3 * 3600)
    assert decide(72.2, stale, 61 * M)[1]["regime"] == "heat"


@pytest.mark.parametrize("extra", [{}, {"outdoor_f": 50, "outdoor_ts": 0}])   # missing / 4 h stale
def test_missing_or_stale_outdoor_uses_fallback_target(extra):
    now = 4 * 3600
    d, _, info, _ = decide(73.2, st(regime="cool", regime_since=0, last_ok_ts=now - 10 * M, **extra), now)
    assert info["cool_target"] is None and info["floor"] == 71 and d["cool_setpoint"] == 71   # 72 - 1.8 -> 71


@pytest.mark.parametrize("field, value", [
    ("cool_target_lo_f", 74.0), ("cool_target_out_hi_f", 50.0), ("heat_to_cool_margin_f", -1.0),
    ("cool_to_heat_f", 71.6),        # would meet the lowered heat -> cool entry (70.0 + 1.5)
    ("cool_gain", 0.0), ("cool_floor_below_f", 6.0), ("cool_max", 71), ("cool_max", 90), ("cool_hyst_f", -0.1),
    ("cool_trim_min", 1), ("cool_trim_max", -1), ("cool_trim_band_f", -1.0), ("cool_fallback_target_f", 90.0),
    ("unit_diff_alpha", 1.5), ("unit_diff_window_h", 0.0),
])
def test_validation(field, value):
    validate(Settings())
    s = Settings()
    setattr(s.tuning, field, value)
    with pytest.raises(ValueError):
        validate(s)


def test_validation_rejects_a_floor_below_64():
    s = Settings()
    s.tuning.cool_target_lo_f, s.tuning.cool_floor_below_f = 65.0, 2.0   # ceil(63.0) = 63
    s.tuning.heat_to_cool_margin_f = 4.0                                   # keep cool_to_heat_f valid
    with pytest.raises(ValueError, match="lowest cool floor"):
        validate(s)


@pytest.mark.parametrize("key", ["cool_table", "cool_offsets", "cool_offsets_else", "cool_floor_table",
                                 "cool_floor", "cool_min"])
def test_removed_keys_get_a_clear_error(tmp_path, key):
    p = tmp_path / "c.toml"
    p.write_text(f"[tuning]\n{key} = 1\n")
    with pytest.raises(ValueError, match="was removed"):
        config.load(p, env={})
