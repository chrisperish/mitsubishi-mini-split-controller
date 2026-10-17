"""Pure-logic tests (no network, no clock)."""
from minisplit_controller.config import Tuning
from minisplit_controller.logic import decide, default_state, held, needed_changes, next_due, slope

M = 60
REGIME_MIN_S = Tuning().regime_min_h * 3600


def st(**kw):
    s = default_state(0)
    s.update(kw)
    return s


def test_heat_table_and_immediate_lower():
    s = st(regime_since=0, last_ok_ts=None, sp=70, sp_changed_at=0)
    d, _, i, ev = decide(71.2, s, 10 * M)
    assert d == {"mode": "heat", "heat_setpoint": 66, "cool_setpoint": 76, "fan": "quiet"}
    assert i["S"] == 0 and ev and "70 -> 66" in ev[0]


def test_raise_one_step_per_15_min():
    s = st(sp=66, sp_changed_at=0, last_ok_ts=None)
    d, s, _, _ = decide(67.0, s, 5 * M); assert d["heat_setpoint"] == 66
    d, s, _, _ = decide(67.0, s, 16 * M); assert d["heat_setpoint"] == 67
    d, s, _, _ = decide(67.0, s, 26 * M); assert d["heat_setpoint"] == 67
    d, s, _, _ = decide(67.0, s, 32 * M); assert d["heat_setpoint"] == 68


def test_slope_uses_reading_closest_to_30_min():
    h = [(0, 69.0), (40 * M, 69.5), (45 * M, 69.6)]
    S, age = slope(h, 70 * M, 70.0, 10)
    assert age == 30 and abs(S - 1.0) < 1e-9
    assert slope(h, 70 * M, 70.0, 31)[0] == 0


def test_held():
    h = [(t * M, 74.5) for t in (0, 10, 20, 30, 40, 50)]
    assert held(h, lambda t: t >= 74, 45, 50 * M)
    assert not held([(0, 74.5), (40 * M, 74.5), (50 * M, 74.5)], lambda t: t >= 74, 45, 50 * M)  # 40-min gap
    h3 = [(0, 74.5), (10 * M, 73.9), (20 * M, 74.5), (30 * M, 74.5), (45 * M, 74.5)]
    assert not held(h3, lambda t: t >= 74, 45, 45 * M)


def test_solar_gain_rule_switches_to_cool_after_1h_dwell():
    flat = [[t * M, 72.5] for t in range(0, 60, 10)]   # below 73: never switches
    s = st(regime_since=-REGIME_MIN_S, history=flat, last_ok_ts=50 * M, outdoor_f=63, outdoor_ts=55 * M)
    assert decide(72.5, s, 60 * M)[1]["regime"] == "heat"
    hot = [[t * M, 73.2] for t in (30, 40, 50)]
    assert decide(73.2, st(regime_since=0, history=hot, last_ok_ts=50 * M), 59 * M)[1]["regime"] == "heat"
    s = st(regime_since=0, history=hot, last_ok_ts=50 * M, outdoor_f=58, outdoor_ts=55 * M)
    d, s2, _, _ = decide(73.2, s, 61 * M)
    assert s2["regime"] == "cool"
    # adaptive: 58F outdoor -> target 70.86, Tp 73.2 is +2.34 -> 71 (floor 71); fan e 2.34 -> powerful
    assert d == {"mode": "cool", "heat_setpoint": 66, "cool_setpoint": 71, "fan": "powerful"}
    short = [[t * M, 73.5] for t in (45, 50, 55)]       # held only 15 min
    assert decide(73.5, st(regime_since=-REGIME_MIN_S, history=short, last_ok_ts=55 * M), 60 * M)[1]["regime"] == "heat"
    stale = st(regime_since=-REGIME_MIN_S, history=[[t * M, 73.5] for t in (30, 40, 50)], last_ok_ts=20 * M)
    assert decide(73.5, stale, 60 * M)[1]["regime"] == "heat"    # gap 40 min: no switch


def test_first_cool_sp_skips_step_and_cool_to_heat_keeps_3h_dwell():
    hist = [[t * M, 74.0] for t in (30, 40, 50)]
    s = st(regime_since=-REGIME_MIN_S, history=hist, last_ok_ts=50 * M, cool_sp=78, cool_sp_changed_at=55 * M)
    d, s2, _, _ = decide(74.0, s, 60 * M, live={"mode": "cool", "cool_setpoint": 78})
    assert s2["regime"] == "cool" and d["cool_setpoint"] == 73
    cold = [[t * M, 67.5] for t in range(0, 60, 10)]
    assert decide(67.5, st(regime="cool", regime_since=-1.5 * 3600, history=cold, last_ok_ts=50 * M),
                  60 * M)[1]["regime"] == "cool"
    assert decide(67.5, st(regime="cool", regime_since=-REGIME_MIN_S, history=cold, last_ok_ts=50 * M),
                  60 * M)[1]["regime"] == "heat"


def test_cool_lower_limited_raise_immediate():
    s = st(regime="cool", regime_since=0, cool_sp=78, cool_sp_changed_at=0)
    d, s, _, _ = decide(74.0, s, 5 * M); assert d["cool_setpoint"] == 78
    d, s, _, _ = decide(74.0, s, 16 * M); assert d["cool_setpoint"] == 76   # e 2.0 vs fallback 72 > 1.5: 2F step
    d, s, _, _ = decide(70.0, s, 17 * M); assert d["cool_setpoint"] == 78


def test_trim_steps_and_limits():
    hist = [[t * M, 69.0] for t in range(0, 181, 10)]
    s = st(history=hist[:-1], last_ok_ts=170 * M, last_trim_at=0, sp=71, sp_changed_at=0)
    _, s2, _, ev = decide(69.0, s, 180 * M)
    assert s2["trim"] == 1 and any("trim" in e for e in ev)
    assert decide(69.0, st(history=hist[:-1], last_ok_ts=170 * M, last_trim_at=0, trim=2), 180 * M)[1]["trim"] == 2


def test_custom_tuning_is_respected():
    t = Tuning(heat_min=68, heat_max=72, cool_park=80)
    d, *_ = decide(65.0, st(), 0, t=t)
    assert d["heat_setpoint"] == 72 and d["cool_setpoint"] == 80


def test_diff_and_schedule():
    live = {"mode": "heat", "heat_setpoint": 66, "cool_setpoint": 76, "fan": "auto"}
    assert needed_changes(live, dict(live)) == {}
    assert needed_changes(dict(live, mode="auto"), live) == {"mode": "heat"}
    assert next_due(1000, 1005, 1030, 600) == 1600
    assert next_due(1000, 1500, 1530, 600) == 2130


def test_cool_setpoint_is_clamped():
    t = Tuning(cool_floor=74, cool_max=76)
    d, *_ = decide(80.0, st(regime="cool", regime_since=0), 0, t=t)
    assert d["cool_setpoint"] == 74
    d, *_ = decide(60.0, st(regime="cool", regime_since=0), 0, t=t)
    assert d["cool_setpoint"] == 76
