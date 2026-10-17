"""Cool setpoint design: u formula, rounding, hysteresis, step limits, cool trim, unit-vs-sensor offset log,
and the one-time fan stage remap. Ported from the reference controller's tests."""
import pytest

from minisplit_controller.config import Tuning
from minisplit_controller.logic import (FAN_STAGE_REMAP_V1, cool_floor, cool_target, cool_u, decide, default_state,
                                        diff_mean, next_cool_sp, sp_round, unit_diff, update_diff_ema)

M = 60
T = Tuning()
REGIME_MIN_S = T.regime_min_h * 3600


def st(**kw):
    s = default_state(0)
    s["fan_stages_v"] = 2
    s.update(kw)
    return s


def test_u_and_rounding():
    assert cool_u(70.0, T, 54) == 70.0 and cool_u(70.5, T, 54) == 69.25 and cool_u(72.5, T, 54) == 69   # floor 69
    assert cool_u(60.0, T, 54) == 78                                     # cap at cool_max
    assert cool_u(70.0, T, 54, -1) == 69.0 and cool_u(70.0, T, 54, -2) == 69   # trim, then floor
    assert cool_u(69.0, T, 54, -2) == 69.5
    assert [sp_round(x) for x in (69.49, 69.5, 70.5, 71.5, 72.25)] == [69, 70, 71, 72, 72]    # halves round up
    assert cool_target(73.5, T, 80) == 74 and cool_target(74.0, T, 80) == 73 and cool_target(75.0, T, 80) == 72


def test_hysteresis_and_steps():
    assert next_cool_sp(69.3, None, 1, False, T) == 69                  # no current SP: straight, no limit
    assert next_cool_sp(70.6, 70, 1, True, T) == 70                     # |u - sp| 0.6 < 0.75: hold
    assert next_cool_sp(70.74, 70, 1, True, T) == 70
    assert next_cool_sp(70.75, 70, 1, True, T) == 71                    # raise immediately
    assert next_cool_sp(73.0, 70, 1, False, T) == 73                    # raises ignore the 15 min limit
    assert next_cool_sp(69.3, 70, 1, True, T) == 70                     # 0.7: hold
    assert next_cool_sp(69.25, 70, 1, True, T) == 69                    # 0.75: drop
    assert next_cool_sp(69.0, 72, 1, True, T) == 71 and next_cool_sp(69.0, 72, 2, True, T) == 70
    assert next_cool_sp(69.0, 72, 2, False, T) == 72                    # drop rate-limited
    assert next_cool_sp(69.0, 70, 2, True, T) == 69                     # never past sp_round(u)
    assert next_cool_sp(70.6, 70, 1, True, Tuning(cool_hyst_f=0.5)) == 71   # configurable


def run(T_room, cool_sp, out=54, at=16 * M, changed=0, **kw):
    s = st(regime="cool", regime_since=0, cool_sp=cool_sp, cool_sp_changed_at=changed, outdoor_f=out,
           outdoor_ts=0, **kw)
    return decide(T_room, s, at)


def test_decide_cool_hysteresis_step_and_entry():
    d, _, i, ev = run(71.6, 72)
    assert d["cool_setpoint"] == 70 and i["step"] == 2 and i["u"] == 69.0 and "72 -> 70" in ev[0]
    d, _, i, _ = run(71.4, 72); assert d["cool_setpoint"] == 71 and i["step"] == 1          # e <= 1.5: 1F
    d, _, i, _ = run(71.5, 72); assert d["cool_setpoint"] == 71 and i["step"] == 1          # e == 1.5: still 1F
    d, _, _, _ = run(71.6, 72, at=10 * M); assert d["cool_setpoint"] == 72                  # < 15 min: hold
    d, _, _, _ = run(70.4, 70); assert d["cool_setpoint"] == 70                             # u 69.4: within 0.75
    d, _, _, _ = run(69.7, 70, at=1 * M); assert d["cool_setpoint"] == 70                   # u 70.45: within 0.75
    d, _, _, _ = run(69.5, 70, at=1 * M); assert d["cool_setpoint"] == 71                   # |u - sp| = 0.75
    d, _, _, _ = run(69.0, 70, at=1 * M); assert d["cool_setpoint"] == 72                   # u 71.5: raise now
    d, _, i, _ = run(76.0, 75, out=85); assert d["cool_setpoint"] == 73 and i["floor"] == 72 and i["step"] == 2
    d, _, _, _ = run(71.0, None); assert d["cool_setpoint"] == 69                           # no SP: straight


def test_cold_day_steps_down_then_raises():
    s = st(regime="cool", regime_since=0, cool_sp=77, cool_sp_changed_at=0, outdoor_f=50, outdoor_ts=0)
    d, s, i, _ = decide(73.6, s, 5 * M); assert d["cool_setpoint"] == 77 and i["cool_target"] == 70.0
    d, s, i, _ = decide(73.6, s, 16 * M); assert d["cool_setpoint"] == 75 and i["step"] == 2   # e 3.6: 2F step
    d, s, _, _ = decide(73.6, s, 20 * M); assert d["cool_setpoint"] == 75                     # < 15 min
    d, s, _, _ = decide(73.6, s, 32 * M); assert d["cool_setpoint"] == 73
    d, s, _, _ = decide(73.6, s, 48 * M); assert d["cool_setpoint"] == 71
    d, s, _, _ = decide(73.6, s, 64 * M); assert d["cool_setpoint"] == 69
    d, s, _, _ = decide(73.6, s, 80 * M); assert d["cool_setpoint"] == 69                     # 69 is the floor
    d, s, i, _ = decide(69.0, s, 100 * M); assert d["cool_setpoint"] == 72 and i["Tp"] == 68.5   # raise at once
    d, s, i, _ = decide(69.0, s, 130 * M)                                                     # outdoor now stale
    assert d["cool_setpoint"] == 77 and i["cool_target"] is None                              # 72 + 4.5 -> 77


def test_cool_trim_every_3h():
    hist = [[t * M, 71.0] for t in range(0, 181, 10)]   # 3 h at 71.0, target 70.0 at 54F
    base = dict(regime="cool", regime_since=-REGIME_MIN_S, history=hist[:-1], last_ok_ts=170 * M, cool_sp=69,
                outdoor_f=54, outdoor_ts=170 * M, last_trim_at=180 * M)
    _, s2, _, ev = decide(71.0, st(last_cool_trim_at=0, cool_trim=0, **base), 180 * M)
    assert s2["cool_trim"] == -1 and any("cool trim change: +0 -> -1" in e for e in ev) and s2["trim"] == 0
    assert decide(71.0, st(last_cool_trim_at=0, cool_trim=-2, **base), 180 * M)[1]["cool_trim"] == -2     # clamp
    assert decide(71.0, st(last_cool_trim_at=60 * M, cool_trim=0, **base), 180 * M)[1]["cool_trim"] == 0  # < 3 h
    cold = dict(base, history=[[t * M, 69.5] for t in range(0, 180, 10)])
    assert decide(69.5, st(last_cool_trim_at=0, cool_trim=-1, **cold), 180 * M)[1]["cool_trim"] == 0     # below
    assert decide(69.5, st(last_cool_trim_at=0, cool_trim=0, **cold), 180 * M)[1]["cool_trim"] == 0      # clamp 0
    ok = dict(base, history=[[t * M, 70.2] for t in range(0, 180, 10)])
    assert decide(70.2, st(last_cool_trim_at=0, cool_trim=-1, **ok), 180 * M)[1]["cool_trim"] == -1      # in band
    short = dict(base, history=[[t * M, 71.0] for t in range(60, 180, 10)])                              # 2 h only
    assert decide(71.0, st(last_cool_trim_at=0, cool_trim=0, **short), 180 * M)[1]["cool_trim"] == 0
    s3 = decide(71.0, st(cool_trim=0, **base), 180 * M)[1]                                               # no stamp
    assert s3["last_cool_trim_at"] == 180 * M and s3["cool_trim"] == 0
    s = st(regime="cool", regime_since=0, cool_sp=None, cool_trim=-1, outdoor_f=54, outdoor_ts=0)
    assert decide(70.0, s, 10 * M)[0]["cool_setpoint"] == 69                                             # lowers u
    t = Tuning(cool_trim_min=-1)
    assert decide(71.0, st(last_cool_trim_at=0, cool_trim=-1, **base), 180 * M, t=t)[1]["cool_trim"] == -1


def test_cool_trim_resets_on_entering_cool():
    hot = [[t * M, 73.2] for t in (30, 40, 50)]
    s = st(regime_since=0, history=hot, last_ok_ts=50 * M, outdoor_f=58, outdoor_ts=55 * M, cool_trim=-2,
           last_cool_trim_at=0)
    _, s2, _, _ = decide(73.2, s, 61 * M)
    assert s2["regime"] == "cool" and s2["cool_trim"] == 0 and s2["last_cool_trim_at"] == 61 * M


def test_unit_diff_is_log_only():
    assert unit_diff(71.6, 72.1, "sensor") == -0.5 and unit_diff(71.6, 72.1, "unit") is None
    assert unit_diff(None, 72, "sensor") is None
    assert update_diff_ema(None, -0.5, T) == -0.5 and update_diff_ema(-0.5, 0.5, T) == round(0.15 - 0.35, 3)
    assert update_diff_ema(0.2, None, T) == 0.2 and update_diff_ema(None, 9, T) == 3.0
    s = st(regime="cool", regime_since=0, cool_sp=None, outdoor_f=54, outdoor_ts=0)
    a = decide(70.0, s, 10 * M, unit_f=72.5, source="sensor")
    b = decide(70.0, s, 10 * M)
    assert a[0] == b[0]                                               # no control effect
    assert a[1]["unit_diff_ema"] == 2.5 and a[2]["udiff"] == 2.5
    assert "sensor offset alert" in a[2]["diff_alert"] and b[2]["diff_alert"] is None
    assert diff_mean([[0, 1.0], [100, 2.0], [-30000, 9.0]], 200, T) == 1.5
    s = st(regime="cool", regime_since=0, unit_diff_hist=[[0, 0.5], [600, 0.3]])
    _, s2, i, _ = decide(70.0, s, 1200, unit_f=70.4, source="sensor")
    assert i["udiff_6h"] == 0.4 and i["diff_alert"] is None and len(s2["unit_diff_hist"]) == 3


def test_fan_stage_remap_from_old_state():
    assert FAN_STAGE_REMAP_V1 == {0: 0, 1: 1, 2: 1, 3: 2, 4: 3}
    for old, new in FAN_STAGE_REMAP_V1.items():
        s = default_state(0)
        s.update(regime="heat", sp=70, sp_changed_at=0, fan_stage=old)   # saved before fan_stages_v existed
        s2 = decide(70.0, s, 10 * M)[1]
        assert s2["fan_stages_v"] == 2 and s2["fan_stage"] == new
    s = default_state(0)
    s.update(regime="cool", regime_since=0, cool_sp=72, fan_stage=3, fan_down_count=0)
    d, s2, _, _ = decide(73.0, s, 10 * M)     # no outdoor: e 1.0; old 3 (powerful) -> 2 (powerful), held
    assert s2["fan_stage"] == 2 and d["fan"] == "powerful" and s2["fan_down_count"] == 0
    assert decide(73.0, s2, 20 * M)[1]["fan_stage"] == 2                 # already v2: not remapped again


def test_floor_examples():
    assert (cool_floor(54, T), cool_floor(None, T), cool_floor(80, T)) == (69, 71, 72)
    assert cool_floor(54, Tuning(cool_floor_below_f=1.0)) == 69 and cool_floor(54, Tuning(cool_floor_below_f=2.0)) == 68
