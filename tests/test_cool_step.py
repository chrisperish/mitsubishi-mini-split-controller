"""Cool table (75 at the target, 1F colder per 0.5F above it), 2F step-downs, loop deferral after a kick."""
import pytest

from minisplit_controller.config import Settings, Tuning, validate
from minisplit_controller.logic import (adaptive_target, cool_floor, cool_step, cool_target, decide,
                                        default_state, deferred_due)

M = 60
T = Tuning()


def st(**kw):
    s = default_state(0)
    s.update(kw)
    return s


def test_default_tables():
    assert T.cool_offsets == [(-1.5, 78), (-1.0, 77), (-0.5, 76), (0.0, 75), (0.5, 74), (1.0, 73), (1.5, 72)]
    assert T.cool_table == [(70.5, 78), (71.0, 77), (71.5, 76), (72.0, 75), (72.5, 74)]
    assert T.cool_offsets_else == 71


def test_bands_around_target():
    t = adaptive_target(58.1, T)                       # 70.89, floor 71
    assert t == pytest.approx(70.89, abs=0.02) and cool_floor(58.1, T) == 71
    sp = lambda off: cool_target(t + off, T, 58.1)  # noqa: E731
    assert [sp(o) for o in (-2.0, -1.2, -0.7, -0.2)] == [78, 77, 76, 75]
    assert sp(0.0) == 75 and sp(0.3) == 74 and sp(0.5) == 74
    assert sp(0.8) == 73 and sp(1.0) == 73
    assert sp(1.1) == 72 and sp(1.5) == 72 and sp(1.6) == 71 and sp(3.0) == 71


def test_floor_73_on_warm_days():
    for out in (68, 72, 80, 95):
        assert min(cool_target(x / 10, T, out) for x in range(690, 800)) == 73
    t = adaptive_target(80, T)                         # 73.5
    assert cool_target(t, T, 80) == 75 and cool_target(t + 0.5, T, 80) == 74 and cool_target(t + 1.2, T, 80) == 73
    assert cool_target(t - 1.6, T, 80) == 78
    assert cool_target(74.5, T) == 73                  # no outdoor: floor 73


def test_step_size():
    t = adaptive_target(58.1, T)
    assert cool_step(t + 1.6, 58.1, T) == 2 and cool_step(t + 1.5, 58.1, T) == 1
    assert cool_step(73.6, None, T) == 2 and cool_step(73.5, None, T) == 1   # fallback target 72.0
    assert cool_step(80.0, 58.1, Tuning(cool_big_step=1)) == 1


def run(T_room, cool_sp, out=58.1, at=16 * M, changed=0):
    s = st(regime="cool", regime_since=0, cool_sp=cool_sp, cool_sp_changed_at=changed, outdoor_f=out, outdoor_ts=0)
    return decide(T_room, s, at)


def test_step_down_rate_limit():
    t = adaptive_target(58.1, T)
    d, _, i, ev = run(t + 1.6, 75); assert d["cool_setpoint"] == 73 and i["step"] == 2 and "75 -> 73" in ev[0]
    d, _, i, _ = run(t + 1.4, 75); assert d["cool_setpoint"] == 74 and i["step"] == 1    # e <= 1.5: 1F
    d, _, _, _ = run(t + 1.6, 72); assert d["cool_setpoint"] == 71                       # never below want
    d, _, _, _ = run(t + 1.6, 75, at=10 * M); assert d["cool_setpoint"] == 75            # < 15 min: hold
    d, _, _, _ = run(t - 2.0, 72, at=1 * M); assert d["cool_setpoint"] == 78             # raise immediately
    d, _, i, _ = run(76.0, 75, out=85); assert d["cool_setpoint"] == 73 and i["step"] == 2   # hot: floor 73


def test_step_down_sequence():
    s = st(regime="cool", regime_since=0, cool_sp=77, cool_sp_changed_at=0, outdoor_f=50, outdoor_ts=0)
    d, s, i, _ = decide(73.6, s, 16 * M); assert d["cool_setpoint"] == 75 and i["step"] == 2   # e 3.1
    d, s, _, _ = decide(73.6, s, 20 * M); assert d["cool_setpoint"] == 75                     # < 15 min
    d, s, _, _ = decide(73.6, s, 32 * M); assert d["cool_setpoint"] == 73
    d, s, _, _ = decide(73.6, s, 48 * M); assert d["cool_setpoint"] == 71
    d, s, _, _ = decide(73.6, s, 64 * M); assert d["cool_setpoint"] == 71                     # 71 is the floor


def test_deferred_due():
    assert deferred_due(None, 1000, 600, 300) is None
    assert deferred_due(1000, 1100, 600, 300) == 1600   # outside cycle 100 s ago: wait until 10 min after it
    assert deferred_due(1000, 1300, 600, 300) is None   # >= 5 min ago: run now
    assert deferred_due(2000, 1000, 600, 300) is None   # clock oddity: run now
    assert deferred_due(1000, 1100, 600, 0) is None     # window 0 disables the skip


@pytest.mark.parametrize("field, value", [("cool_big_err_f", -1.0), ("cool_big_step", 0), ("cool_big_step", 4),
                                          ("cool_fallback_target_f", 90.0)])
def test_validation(field, value):
    s = Settings()
    setattr(s.tuning, field, value)
    with pytest.raises(ValueError):
        validate(s)
