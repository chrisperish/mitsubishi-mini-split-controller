"""Illustrative simulation and charts: deterministic, labelled, and free of real-data claims."""
import csv
import re
import warnings
from pathlib import Path

import matplotlib.text
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
    assert set(a.heat_sp) == {70} and set(a.cool_sp) == {72}  # fair run: aimed at the band
    ref = simulate.stock_auto(heat=67, cool=73)                # restore.sh defaults, reference only
    assert simulate.summarize(ref)["below"] > 0.5
    # The sun is calibrated to a real 74.3F afternoon peak recorded under an earlier, milder cool design;
    # with the current design the simulated controller peaks near 73.7F.
    assert 73.4 <= max(c.true) <= 74.8
    assert set(c.heat_sp) <= set(range(66, 72))
    assert all(y - x <= 1 for x, y in zip(c.heat_sp, c.heat_sp[1:]))   # raises are 1F at a time
    sa, sc = simulate.summarize(a), simulate.summarize(c)
    assert abs(sc["in_band"] - sa["in_band"]) < 0.05          # about equal on time in band
    assert sc["cool_h"] < sa["cool_h"]                        # the real edge: less cooling


def test_outcome_does_not_depend_on_seed():
    base_a, base_c = simulate.stock_auto(0), simulate.controller(0)
    for seed in range(21):
        a, c = simulate.stock_auto(seed), simulate.controller(seed)
        assert a.true == base_a.true and a.mode == base_a.mode
        assert c.true == base_c.true and c.mode == base_c.mode and c.heat_sp == base_c.heat_sp
        for r, base in ((a, base_a), (c, base_c)):
            s, s0 = simulate.summarize(r), simulate.summarize(base)
            assert abs(s["in_band"] - s0["in_band"]) < 0.03


def test_charts_render_with_labels_and_no_warnings(tmp_path, monkeypatch):
    texts, per_fig = [], {}
    orig = make_charts.save

    def spy(fig, name, out):
        got = [t.get_text() for t in fig.findobj(lambda o: hasattr(o, "get_text"))]
        per_fig[name] = [t.get_text() for t in fig.findobj(matplotlib.text.Text)]
        texts.extend(got)
        orig(fig, name, out)

    monkeypatch.setattr(make_charts, "save", spy)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_charts.illustrative(tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted(f"illustrative-{n}.{e}" for n in ("chart1-timeline", "chart2-steering",
                                                              "chart3-time-in-band") for e in ("png", "svg"))
    sa, sc = simulate.summarize(simulate.stock_auto()), simulate.summarize(simulate.controller())
    c1 = [x for x in texts if x.startswith("Sun: flips to cooling at") or x.startswith("Stays in heat until 73F")
          or x == "Setpoint steps down before the room overshoots"]
    assert len(c1) == 3                                        # chart 1: exactly 3 callouts
    assert any(f"cools {sa['cool_h']:.1f} h" in x for x in c1) and any(f"cools {sc['cool_h']:.1f} h" in x for x in c1)
    c1_texts = next(v for k, v in per_fig.items() if "chart1" in k)
    assert c1_texts.count("Heating") == 1 and c1_texts.count("Cooling") == 1   # chart 1: legend only
    for label in ("In simulation, both hold the band about equally;\nthe controller cools less.",
                  "How the controller steers (simulated).", "Time in the comfort band (illustrative)",
                  "Stock Auto 70/72", "Stock Auto 67/73\n(not aimed at band)",
                  "Predicted to rise, so setpoint drops 1F", "Room temp", "Setpoint", "Heating", "Cooling"):
        assert label in texts, label
    assert texts.count(make_charts.ILLUSTRATIVE) == 6          # subtitle + corner on each chart


def test_committed_charts_and_no_data():
    files = sorted(p.name for p in (ROOT / "docs" / "charts").iterdir())
    assert files and all(f.startswith("illustrative-") for f in files)
    assert not (ROOT / "data").exists()


def test_readme_claims_are_qualified():
    assert "Charts are a simulation that shows the behavior. Your results depend on your room, unit and climate." in README
    assert "The controller writes its own log so you can chart your real results." in README
    assert "## What to expect" in README and "## Results" not in README
    assert "**Status: experimental.**" in README
    assert "In the author's own trial, this version has not yet beaten an earlier, simpler version (roughly 44% vs 71% of time in band)." in README
    for alt in re.findall(r"!\[([^\]]*)\]", README):
        assert alt.startswith("Simulated, not measured"), alt
    assert "A 67/73 Auto (the defaults `scripts/restore.sh` puts back) is not trying to hold a 69 to 71°F band" in README
    assert README.index("**Status: experimental.**") < README.index("## The problem")   # near the top
    assert "toggles its heat setpoint between 69 and 70°F about every 30 minutes" in README
    assert "mixed" not in README.splitlines()[2]


def test_readme_numbers_match_simulation():
    sa = simulate.summarize(simulate.stock_auto())
    sc = simulate.summarize(simulate.controller())
    sr = simulate.summarize(simulate.stock_auto(heat=67, cool=73))
    assert f"({sa['in_band']:.0%} for Stock Auto, {sc['in_band']:.0%} for the controller)" in README
    assert f"cools for {sa['cool_h']:.1f} hours" in README and f"cools for {sc['cool_h']:.1f} hours" in README
    assert f"it spends {sr['below']:.0%} of the day below 69°F" in README
    for p in [ROOT / "README.md", *(ROOT / "scripts").glob("*.py"), *(ROOT / "src").rglob("*.py")]:
        t = p.read_text()
        assert "\u2014" not in t and "\u2013" not in t, p
        assert ("ro" + "of") not in t.lower(), p


def test_gitignore_keeps_docs_charts():
    lines = (ROOT / ".gitignore").read_text().split()
    assert "/charts/" in lines and "charts/" not in lines


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
