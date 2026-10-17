"""Fan staging: stage table, step-up limit, step-down hysteresis, long-gap hold, no resend, no vane."""
import math

import pytest

from minisplit_controller import config
from minisplit_controller.config import Tuning
from minisplit_controller.logic import decide, default_state, fan_error, fan_raw_stage, fan_stage, needed_changes

M = 60
T = Tuning()


def st(**kw):
    s = default_state(0)
    s["fan_stages_v"] = 2
    s.update(kw)
    return s


def cool(**kw):
    return st(regime="cool", regime_since=0, cool_sp=74, cool_sp_changed_at=0, outdoor_f=55, outdoor_ts=0, **kw)


def test_default_stages():
    assert T.fan_stages[:3] == [(0.3, "quiet"), (1.5, "low"), (2.5, "powerful")]
    assert T.fan_stages[3][1] == "superPowerful" and math.isinf(T.fan_stages[3][0])


def test_raw_stages():
    assert [fan_raw_stage(e, T) for e in (-1, 0.3, 0.31, 0.8, 1.5, 2.5, 2.6, 9)] == [0, 0, 1, 1, 1, 2, 3, 3]


def test_up_immediate_at_most_two():
    assert fan_stage(3.0, 0, 0, T) == (2, 0)
    assert fan_stage(3.0, 1, 0, T) == (3, 0)
    assert fan_stage(1.0, 0, 1, T) == (1, 0)


def test_down_hysteresis():
    assert fan_stage(1.3, 2, 0, T) == (2, 0)        # below 1.5 but not by 0.3
    assert fan_stage(1.2, 2, 0, T) == (2, 1)        # 1st qualifying cycle
    assert fan_stage(1.2, 2, 1, T) == (1, 0)        # 2nd: step down powerful -> low
    assert fan_stage(1.3, 2, 1, T) == (2, 0)        # streak resets
    assert fan_stage(0.0, 3, 1, T) == (0, 0)        # big drop goes straight to the stage
    assert fan_stage(0.1, 1, 1, T) == (1, 0) and fan_stage(0.0, 1, 1, T) == (0, 0)   # low -> quiet at e <= 0


def test_error_definitions():
    assert fan_error(73.0, "cool", 55, T) == pytest.approx(3.0)     # adaptive target 70.0
    assert fan_error(73.0, "cool", None, T) == pytest.approx(1.0)   # no outdoor reading: 72.0
    assert fan_error(69.0, "heat", 55, T) == pytest.approx(1.0)     # target_f 70 - Tp


def test_decide_cool_and_gap_keeps_live():
    d, s2, _, ev = decide(73.5, cool(last_ok_ts=50 * M, fan_stage=0), 60 * M)   # e 3.5 -> raw 3, +2 max
    assert d["fan"] == "powerful" and s2["fan_stage"] == 2 and any("fan stage 0 -> 2" in e for e in ev)
    d, s2, _, _ = decide(73.5, cool(last_ok_ts=10 * M, fan_stage=0), 60 * M, {"mode": "cool", "fan": "auto"})
    assert d["fan"] == "auto" and s2["fan_stage"] == 0                          # g 50 min > 30: hold


def test_decide_heat_and_no_resend():
    d, s2, _, _ = decide(69.0, st(sp=70, sp_changed_at=0, last_ok_ts=50 * M, fan_stage=1), 60 * M)
    assert d["fan"] == "low" and s2["fan_stage"] == 1          # e 1.0 -> low
    assert "fan" not in needed_changes(dict(d), d)
    assert needed_changes(dict(d, fan="auto"), d)["fan"] == "low"


def test_vane_never_sent():
    d = decide(75.0, cool(last_ok_ts=50 * M), 60 * M)[0]
    assert "vane" not in d and "vane" not in needed_changes({"vane": "swing"}, d)


def test_staging_off_uses_fixed_fan():
    t = Tuning(fan_staging=False)
    assert decide(69.0, st(last_ok_ts=50 * M), 60 * M, t=t)[0]["fan"] == "auto"


def test_config_fan_stages_and_validation(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[tuning]\nfan_stages = [[0.5, "quiet"], [inf, "powerful"]]\n')
    assert config.load(p, env={}).tuning.fan_stages == [(0.5, "quiet"), (math.inf, "powerful")]
    for bad in ('[tuning]\nfan_stages = [[1.0, "low"], [0.5, "quiet"]]\n', "[tuning]\nfan_max_up = 0\n",
                "[tuning]\nfan_down_cycles = 0\n", "[tuning]\nfan_cool_target_default_f = 90.0\n"):
        p.write_text(bad)
        with pytest.raises(ValueError):
            config.load(p, env={})


@pytest.mark.parametrize("bad", ['[tuning]\nfan_stages = [[0.5, "turbo"], [inf, "low"]]\n',
                                 '[tuning]\nfan_stages = [[0.5, "quiet"], [inf, "bogus"]]\n',
                                 '[tuning]\nfan = "turbo"\n', '[tuning]\nfan = "bogus"\n'])
def test_unknown_fan_speed_rejected(tmp_path, bad):
    p = tmp_path / "c.toml"
    p.write_text(bad)
    with pytest.raises(ValueError, match="unknown fan speed"):
        config.load(p, env={})


def test_fan_speeds_match_client():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "client" / "mitsubishi.py"
    spec = importlib.util.spec_from_file_location("mitsubishi_client", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(config.FAN_SPEEDS) == set(mod.ALL_FANS)


def test_long_gap_without_speed_or_stage_falls_back_to_fan():
    d, s2, _, _ = decide(73.5, cool(last_ok_ts=10 * M), 60 * M, {"mode": "cool"})
    assert d["fan"] == "auto" and s2.get("fan_stage") is None
