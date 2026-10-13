"""Controller I/O tests with the cloud client and outdoor fetch mocked: fully offline."""
import csv

import pytest

from minisplit_controller import config
from minisplit_controller.controller import CSV_COLS, Controller, choose_temp, retry_after


class FakeClient:
    def __init__(self, status, sensor=None, set_rc=0, fail=None):
        self.st, self.sn, self.set_rc, self.fail = dict(status), sensor, set_rc, fail
        self.sets = []

    def status(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return dict(self.st)

    def sensor(self):
        if self.sn is None:
            raise RuntimeError("no sensor")
        return self.sn

    def set(self, ch):
        self.sets.append(ch)
        m = {"mode": "mode", "heat-temp": "heat_setpoint", "cool-temp": "cool_setpoint", "fan": "fan"}
        for k, v in ch.items():
            self.st[m[k]] = v
        return self.set_rc, "", ""


LIVE = {"online": True, "power": "on", "mode": "auto", "heat_setpoint": 67, "cool_setpoint": 73,
        "fan": "auto", "current_temp_c": 21.5, "humidity": 50.0}


@pytest.fixture
def settings(tmp_path):
    s = config.Settings(device="TEST-UNIT", data_dir=tmp_path)
    return s


def make(settings, client, now=1_700_000_000.0):
    return Controller(settings, client=client, outdoor=lambda lat, lon: (50.0, now),
                      clock=lambda: now, sleep=lambda _: None)


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_cycle_switches_auto_to_heat_and_logs(settings):
    cl = FakeClient(LIVE, sensor={"temp_f": 69.0, "age_seconds": 60})
    r = make(settings, cl).cycle()
    assert cl.sets == [{"mode": "heat", "heat-temp": 69, "cool-temp": 76}]
    assert r["desired"]["mode"] == "heat"
    out = rows(settings.log_csv)
    assert list(out[0].keys()) == CSV_COLS and "synced" not in out[0]
    assert out[-1]["event"] == "Control change" and out[-1]["temp_source"] == "sensor"


def test_dry_run_changes_nothing(settings, capsys):
    cl = FakeClient(LIVE, sensor={"temp_f": 69.0, "age_seconds": 60})
    make(settings, cl).cycle(dry_run=True)
    assert cl.sets == [] and not settings.log_csv.exists()
    assert "would set" in capsys.readouterr().out


def test_unit_off_is_left_alone(settings):
    cl = FakeClient(dict(LIVE, power="off"))
    assert "off" in make(settings, cl).cycle()["action"] and cl.sets == []


def test_rate_limit_sets_backoff(settings):
    c = make(settings, FakeClient(LIVE, fail="Rate limited, retry after 120"))
    c.cycle()
    assert c.load_state()["backoff_until"] == pytest.approx(1_700_000_000 + 120)
    assert "backoff" in c.cycle()["action"]


def test_sensor_fallback_to_unit():
    assert choose_temp({"temp_f": 69, "age_seconds": 3600}, 70.7, 20)[1] == "unit"
    assert choose_temp(None, 70.7, 20, "x")[0] == 70.7
    assert retry_after("retry after 30") == 30 and retry_after("ok") is None


def test_outdoor_disabled_without_coordinates(settings):
    called = []
    c = Controller(settings, client=FakeClient(LIVE), outdoor=lambda *a: called.append(a), clock=lambda: 0.0)
    assert c.update_outdoor({}) is None and called == []


def test_config_file_and_env(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[controller]\ndevice = "Den"\ninterval_s = 300\n[tuning]\nheat_max = 72\n'
                 'heat_table = [[70.0, 66], [69.0, 68]]\n')
    s = config.load(p, env={"MSC_DEVICE": "Office", "MSC_LATITUDE": "1.5"})
    assert s.device == "Office" and s.interval_s == 300 and s.latitude == 1.5
    assert s.tuning.heat_max == 72 and s.tuning.heat_table == [(70.0, 66), (69.0, 68)]
    p.write_text("[tuning]\nbogus = 1\n")
    with pytest.raises(ValueError):
        config.load(p, env={})


def test_example_config_parses():
    from pathlib import Path
    s = config.load(Path(__file__).resolve().parents[1] / "config.example.toml", env={})
    assert s.tuning == config.Tuning()


def test_config_validation_rejects_unsafe_values(tmp_path):
    p = tmp_path / "c.toml"
    for bad in ("[tuning]\nheat_max = 90\n", "[tuning]\nheat_min = 72\n", "[tuning]\ntrim_min = 1\n",
                "[tuning]\ncool_table = [[71.0, 95]]\n", "[controller]\ntimezone = \"Mars/Base\"\n",
                "[controller]\ninterval_s = 5\n"):
        p.write_text(bad)
        with pytest.raises(ValueError):
            config.load(p, env={})


def test_timezone_env_and_log_label(tmp_path):
    s = config.load(None, env={"MSC_TIMEZONE": "UTC", "MSC_DATA_DIR": str(tmp_path)})
    s.device = "TEST-UNIT"
    make(s, FakeClient(LIVE, sensor={"temp_f": 69.0, "age_seconds": 60})).cycle()
    ts = rows(s.log_csv)[-1]["timestamp_pt"]
    assert ts.endswith(" UTC") and ts.startswith("2023-11-14 10:13 PM")


def test_parse_pt_roundtrip():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from minisplit_controller.controller import fmt_pt, parse_pt
    tz = ZoneInfo("America/Los_Angeles")
    dt = datetime(2026, 10, 12, 7, 20, tzinfo=tz)
    assert fmt_pt(dt) == "2026-10-12 7:20 AM PT" and parse_pt(fmt_pt(dt), tz) == dt
