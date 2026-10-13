"""Statistics computed from data/log.csv. Every number in the README and charts comes from here.

Conventions
* Readings: rows with a room temperature (Error rows are skipped). Several rows can share one
  timestamp (a cycle writes event rows plus its reading row); the last row per timestamp wins.
* Time weighting: each reading holds until the next reading (step hold, no interpolation). The
  interval after the last v1 reading ends at the v2 deployment time; the v2 period ends at its
  last reading.
* Comfort band: 69.0 to 71.0F inclusive.
* Mode flip: the logged mode changes between heat and cool from one reading to the next.
* Split: the first row whose action says the new (v2) controller logic started.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")   # zone of the published log
TZ_LABEL = "PT"                         # label used on chart axes
ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "log.csv"
BAND = (69.0, 71.0)
SPLIT_MARKER = "new controller logic started"


@dataclass
class Reading:
    ts: datetime
    room: float
    mode: str
    heat_sp: float | None
    cool_sp: float | None
    action: str
    tp: float | None
    outdoor: float | None


def parse_ts(s):
    return datetime.strptime(s.replace(" PT", ""), "%Y-%m-%d %I:%M %p").replace(tzinfo=PT)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path=LOG):
    with open(path, newline="") as f:
        raw = list(csv.DictReader(f))
    split = next(parse_ts(r["timestamp_pt"]) for r in raw if SPLIT_MARKER in r["action"])
    events = raw
    by_ts = {}
    for r in raw:
        if r["event"] == "Error" or not r["room_f"]:
            continue
        tp = re.search(r"\bTp (\d+\.\d+)", r["action"])
        od = re.search(r"outdoor (\d+\.\d+)F", r["action"])
        ts = parse_ts(r["timestamp_pt"])
        by_ts[ts] = Reading(ts, float(r["room_f"]), r["mode"], _num(r["heat_sp"]), _num(r["cool_sp"]),
                            r["action"], float(tp.group(1)) if tp else None, float(od.group(1)) if od else None)
    return sorted(by_ts.values(), key=lambda x: x.ts), split, events


def intervals(rs, start, end):
    """[(reading, seconds)] for readings in [start, end), each held until the next reading or `end`."""
    sel = [r for r in rs if start <= r.ts < end]
    out = []
    for a, b in zip(sel, sel[1:] + [None]):
        stop = b.ts if b else end
        out.append((a, (stop - a.ts).total_seconds()))
    return out


def summarize(rs, start, end):
    iv = intervals(rs, start, end)
    tot = sum(d for _, d in iv) or 1.0
    below = sum(d for r, d in iv if r.room < BAND[0])
    inb = sum(d for r, d in iv if BAND[0] <= r.room <= BAND[1])
    above = sum(d for r, d in iv if r.room > BAND[1])
    cool = sum(d for r, d in iv if r.mode == "cool")
    sel = [r for r, _ in iv]
    modes = [r.mode for r in sel if r.mode in ("heat", "cool")]
    flips = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
    return {"start": start, "end": end, "hours": tot / 3600, "below": below / tot, "in_band": inb / tot,
            "above": above / tot, "cool_share": cool / tot, "cool_h": cool / 3600, "flips": flips,
            "min": min(r.room for r in sel), "max": max(r.room for r in sel), "n": len(sel)}


def periods(rs, split):
    first, last = rs[0].ts, rs[-1].ts
    # The v1 night: 6 PM on the first day until v2 took over the next morning.
    night_start = first.replace(hour=18, minute=0)
    v2_end = last + timedelta(seconds=1)
    return {"v1": (first, split), "v1_night": (night_start, split), "v2": (split, v2_end)}


def overshoot(rs, start, end):
    """Highest reading in the window, and the lowest reading after it before heat resumes."""
    sel = [r for r in rs if start <= r.ts < end]
    peak = max(sel, key=lambda r: r.room)
    after = []
    for r in sel:
        if r.ts <= peak.ts:
            continue
        if r.mode == "heat" and after and after[-1].mode == "cool":
            break
        after.append(r)
    trough = min(after, key=lambda r: r.room) if after else None
    return peak, trough


def v2_steady(rs, split):
    """v2 heat-regime run before the room started to climb past the band (readings until the first > 71)."""
    sel = [r for r in rs if r.ts >= split]
    run = []
    for r in sel:
        if r.room > BAND[1]:
            break
        run.append(r)
    return run


def cool_segment(rs, split):
    """v2 readings in cool mode (the solar-gain regime)."""
    return [r for r in rs if r.ts >= split and r.mode == "cool"]


def afternoon(rs, split):
    """The one time of day both versions cover.

    One clock window for both days: from the first logged reading of the whole log (v1's start) to
    the last v2 reading. Each day's readings inside that window are time-weighted as usual.
    """
    first, last = rs[0].ts, rs[-1].ts
    t0, t1 = first.timetz(), last.timetz()
    s1, e1 = first, datetime.combine(first.date(), t1)
    s2, e2 = datetime.combine(last.date(), t0), last
    return {"v1": summarize(rs, s1, e1), "v2": summarize(rs, s2, e2 + timedelta(seconds=1)),
            "window1": (s1, e1), "window2": (s2, e2)}


def outdoor_range(rs, start, end):
    vals = [r.outdoor for r in rs if start <= r.ts < end and r.outdoor is not None]
    return (min(vals), max(vals)) if vals else None


def clock(dt):
    return dt.strftime("%-I:%M %p")


def compute(path=LOG):
    rs, split, events = load(path)
    p = periods(rs, split)
    out = {k: summarize(rs, *v) for k, v in p.items()}
    out["split"] = split
    out["readings"] = rs
    out["events"] = events
    out["overshoot"] = overshoot(rs, *p["v1_night"])
    out["v2_steady"] = v2_steady(rs, split)
    out["cool_seg"] = cool_segment(rs, split)
    out["outdoor_v1"] = outdoor_range(rs, *p["v1"])
    out["outdoor_v2"] = outdoor_range(rs, *p["v2"])
    out["afternoon"] = afternoon(rs, split)
    out["first_outdoor"] = next((r for r in rs if r.outdoor is not None), None)
    return out
