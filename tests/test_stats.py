"""The published numbers are computed from data/log.csv; check the pipeline and the README agree."""
from pathlib import Path

import stats

ROOT = Path(__file__).resolve().parents[1]


def test_split_and_periods():
    s = stats.compute()
    assert s["split"].strftime("%H:%M") == "07:17"
    for k in ("v1", "v1_night", "v2"):
        d = s[k]
        assert abs(d["below"] + d["in_band"] + d["above"] - 1) < 1e-9
        assert 0 <= d["cool_share"] <= 1


def test_no_synced_column():
    header = (ROOT / "data" / "log.csv").read_text().splitlines()[0]
    assert "synced" not in header


def test_readme_tables_match_data():
    s = stats.compute()
    text = (ROOT / "README.md").read_text()
    assert f"| Heat/cool mode flips in one night | {s['v1_night']['flips']} |" in text
    assert f"| Time in 69 to 71°F | {s['v1']['in_band'] * 100:.0f}% | {s['v2']['in_band'] * 100:.0f}% |" in text
    assert "\u2014" not in text   # house style: no em dashes


def _p(x):
    return f"{x * 100:.0f}%"


def test_readme_problem_and_results_numbers():
    s = stats.compute()
    text = (ROOT / "README.md").read_text()
    n, v1, v2 = s["v1_night"], s["v1"], s["v2"]
    assert f"| Time in 69 to 71°F | {_p(n['in_band'])} |" in text
    assert f"| Cooling hours that night | {n['cool_h']:.1f} h |" in text
    assert f"| Heat/cool mode flips | {v1['flips']} | {v2['flips']} |" in text
    assert f"| Cooling hours | {v1['cool_h']:.1f} h | {v2['cool_h']:.1f} h |" in text
    assert f"| Min room temp | {v1['min']:.1f}°F | {v2['min']:.1f}°F |" in text
    assert f"| Max room temp | {v1['max']:.1f}°F | {v2['max']:.1f}°F |" in text
    peak, trough = s["overshoot"]
    assert f"Peak {peak.room:.1f}°F" in text and f"down to {trough.room:.1f}°F" in text


def test_readme_afternoon_comparison():
    s = stats.compute()
    a = s["afternoon"]
    text = (ROOT / "README.md").read_text()
    assert "v2 has not yet beaten v1 on time in band." in text
    assert a["v1"]["in_band"] == 1.0                     # v1 was in band the whole Oct 11 afternoon
    assert a["v2"]["above"] > 0.5                        # v2 sat above the band for most of it
    (s1, e1), (s2, e2) = a["window1"], a["window2"]
    assert (s1.timetz(), e1.timetz()) == (s2.timetz(), e2.timetz())   # same clock window both days
    assert s1.strftime("%-I:%M %p") == "1:15 PM" and e2.strftime("%-I:%M %p") == "6:16 PM"
    assert f"| v1 | Oct 11 1:15 PM to 6:16 PM |" in text and f"| v2 | Oct 12 1:15 PM to 6:16 PM |" in text
    assert f"| {_p(a['v1']['in_band'])} | {_p(a['v1']['above'])} |" in text
    assert f"| {_p(a['v2']['in_band'])} | {_p(a['v2']['above'])} |" in text


def test_copy_style():
    for p in [ROOT / "README.md", *(ROOT / "scripts").glob("*.py"), *(ROOT / "src").rglob("*.py")]:
        t = p.read_text()
        assert "\u2014" not in t and "\u2013" not in t, p     # no em or en dashes
        assert "ro" + "of" not in t.lower(), p   # wording: "solar gain"
    assert "stays in heat\"" not in (ROOT / "scripts" / "make_charts.py").read_text()


def test_row_125_note_matches_log():
    import csv
    with open(ROOT / "data" / "log.csv", newline="") as f:
        rows = {r["id"]: r for r in csv.DictReader(f)}
    assert "cool SP 73 -> 75" in rows["124"]["action"]
    assert "changed cool SP 74 to 75" in rows["125"]["action"]
    assert all(rows[i]["cool_sp"] == "73" for i in ("121", "122", "123"))
    assert "rows 124 and 125" in (ROOT / "README.md").read_text()


def test_charts_render_without_font_warnings(tmp_path, monkeypatch):
    import warnings
    import make_charts
    monkeypatch.setattr(make_charts, "OUT", tmp_path)
    monkeypatch.setattr(make_charts, "README", tmp_path / "none.md")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_charts.main()
    assert len(list(tmp_path.glob("*.png"))) == 3
