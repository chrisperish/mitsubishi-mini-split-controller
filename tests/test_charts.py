"""Illustrative simulation and charts: deterministic, labelled, and free of real-data claims."""
import csv
import re
import warnings
from pathlib import Path

import pytest

import make_charts
import simulate

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()


def test_simulation_is_deterministic():
    a1, a2 = simulate.stock_auto(), simulate.stock_auto()
    c1, c2 = simulate.controller(), simulate.controller()
    assert a1.room == a2.room and a1.mode == a2.mode
    assert c1.room == c2.room and c1.heat_sp == c2.heat_sp and c1.tp == c2.tp
    assert simulate.stock_auto(seed=7).room != a1.room       # the seed is what fixes it
    assert simulate.SEED == 42


def test_simulation_shape_matches_spec():
    a, c = simulate.stock_auto(), simulate.controller()
    assert len(a.t) == 24 * 12 + 1 and (a.t[1] - a.t[0]).seconds == 300
    assert a.t[0].hour == 18 and a.t[-1].hour == 18
    assert a.outdoor == c.outdoor                             # same weather for both runs
    assert 44 <= min(a.outdoor) <= 46 and 59 <= max(a.outdoor) <= 61
    sa, sc = simulate.summarize(a), simulate.summarize(c)
    assert 4 <= sa["flips"] <= 6 and 1 <= sa["cool_cold_h"] <= 2
    assert 72 <= sa["max"] <= 73 and 67 <= sa["min"] <= 68
    assert sc["flips"] == 0 and set(c.mode) == {"heat"}
    assert set(c.heat_sp) <= set(range(66, 72))
    assert all(y - x <= 1 for x, y in zip(c.heat_sp, c.heat_sp[1:]))   # raises are 1F at a time
    assert sc["in_band"] > sa["in_band"]


def test_charts_render_with_labels_and_no_warnings(tmp_path, monkeypatch):
    texts = []
    orig = make_charts.save

    def spy(fig, name, out):
        texts.extend(t.get_text() for t in fig.findobj(lambda o: hasattr(o, "get_text")))
        orig(fig, name, out)

    monkeypatch.setattr(make_charts, "save", spy)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_charts.illustrative(tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted(f"illustrative-{n}.{e}" for n in ("chart1-timeline", "chart2-steering",
                                                              "chart3-time-in-band") for e in ("png", "svg"))
    for label in ("Stock Auto drifts. The controller holds.", "How the controller steers.",
                  "Time in the comfort band (illustrative)", "Overshoot", "Flips to cooling",
                  "Cooling on a cold night", "Setpoint steps down before the room overshoots",
                  "Predicted to rise, so setpoint drops 1F", "Room temp", "Setpoint", "Heating", "Cooling"):
        assert label in texts, label
    assert texts.count(make_charts.ILLUSTRATIVE) == 6          # subtitle + corner on each chart


def test_committed_charts_and_no_data():
    files = sorted(p.name for p in (ROOT / "docs" / "charts").iterdir())
    assert files and all(f.startswith("illustrative-") for f in files)
    assert not (ROOT / "data").exists()


def test_readme_has_no_real_data_claims():
    assert "Charts are a simulation that shows the behavior. Your results depend on your room, unit and climate." in README
    assert "The controller writes its own log so you can chart your real results." in README
    assert "## What to expect" in README and "## Results" not in README
    for bad in ("52%", "6.2 h", "4.6 h", "72.1", "68.4", "70.3", "40-48", "v1", "v2", "Oct "):
        assert bad not in README, bad
    for p in [ROOT / "README.md", *(ROOT / "scripts").glob("*.py"), *(ROOT / "src").rglob("*.py")]:
        t = p.read_text()
        assert "\u2014" not in t and "\u2013" not in t, p
        assert ("ro" + "of") not in t.lower(), p


def test_from_log(tmp_path):
    log = tmp_path / "log.csv"
    with open(log, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "timestamp_pt", "room_f", "mode", "heat_sp", "cool_sp", "event", "action"])
        for i in range(12):
            w.writerow([i + 1, f"2000-01-01 {1 + i // 6}:{(i % 6) * 10:02d} AM PT", 69.5 + i * 0.2, "heat",
                        68, 76, "Reading", f"T 70 Tp {70 + i * 0.1:.2f}"])
    make_charts.main(["--from-log", str(log), "--out", str(tmp_path / "out")])
    assert {p.name for p in (tmp_path / "out").iterdir()} == {
        "log-timeline.png", "log-timeline.svg", "log-time-in-band.png", "log-time-in-band.svg"}
    s = make_charts.log_shares(make_charts.load_log(log))
    assert s["in_band"] == pytest.approx(8 / 11) and s["above"] == pytest.approx(3 / 11)
