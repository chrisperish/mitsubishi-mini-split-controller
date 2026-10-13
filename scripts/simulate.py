"""Illustrative simulation used for the README charts. Not measured data.

A one-room thermal model is run for 24 h (6 PM to 6 PM) at 5-minute steps, twice, under the same
outdoor temperature curve:

* Stock Auto: heat setpoint 70F, cool setpoint 72F (aimed at the band), the unit's own Auto changeover.
  A 67/73 run (the restore.sh defaults) is also computed for reference.
* Both runs use the same modulating indoor-unit model (PI loop, minimum output, thermo-off).
* Controller: the real decision code (minisplit_controller.logic.decide) drives the same room.

The dynamics never use random numbers. The seed only adds display noise to the plotted room temp.
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from minisplit_controller.config import Tuning  # noqa: E402
from minisplit_controller.logic import decide, default_state  # noqa: E402

SEED = 42
STEP_MIN = 5
HOURS = 24
START = datetime(2000, 1, 1, 18, 0)   # arbitrary reference day; only clock times are shown
BAND = (69.0, 71.0)

# Room model (F and hours). Heat loss is proportional to the indoor/outdoor difference.
LOSS = 0.06          # 1/h
SUN_PEAK = 2.8       # F/h of solar gain at the afternoon peak (calibrated in README "What to expect")
SUN_PEAK_CLOCK = 16.0
# Indoor unit: an inverter that modulates its output with a PI loop on its own setpoint.
Q_MAX = 4.0          # F/h at full output, heat or cool
Q_MIN = 0.8          # F/h: the compressor cannot run slower than this; below it the unit cycles off
KP, KI = 1.5, 1.0    # PI gains (F/h per F, F/h per F per h)
OFF_ABOVE = 1.0      # thermo-off once the room is this far past the setpoint ...
ON_WITHIN = 0.0      # ... and back on when it returns to the setpoint
CHANGEOVER_MIN = 15  # Auto changes mode after the room sits past the other setpoint this long
AUTO_HEAT, AUTO_COOL = 70, 72   # fair Stock Auto: setpoints aimed at the 69 to 71F band
RESTORE_HEAT, RESTORE_COOL = 67, 73   # the defaults restore.sh puts back (not aimed at the band)


@dataclass
class Run:
    t: list = field(default_factory=list)        # datetimes
    room: list = field(default_factory=list)     # F
    outdoor: list = field(default_factory=list)  # F
    mode: list = field(default_factory=list)     # "heat" / "cool"
    heat_sp: list = field(default_factory=list)
    cool_sp: list = field(default_factory=list)
    tp: list = field(default_factory=list)       # controller only: predicted temp 15 min ahead
    true: list = field(default_factory=list)     # room temp before display noise


def outdoor_f(h):
    """Smooth outdoor curve: 60F at 1 PM, 45F at 1 AM (h = hours since 6 PM)."""
    clock = (18 + h) % 24
    return 52.5 + 7.5 * math.cos((clock - 13) / 24 * 2 * math.pi)


def sun(h):
    """Solar gain in F/h: zero at night, a smooth bump from 9 AM to 7 PM peaking mid-afternoon."""
    clock = (18 + h) % 24
    if not 9 <= clock <= 19:
        return 0.0
    x = (clock - 9) / (19 - 9)
    peak = (SUN_PEAK_CLOCK - 9) / (19 - 9)
    shape = math.sin(math.pi * x ** (math.log(0.5) / math.log(peak)))   # skewed so it peaks at SUN_PEAK_CLOCK
    return SUN_PEAK * shape


def steps():
    n = HOURS * 60 // STEP_MIN
    return [i * STEP_MIN / 60 for i in range(n + 1)]


class Unit:
    """Modulating heat pump: PI on its own setpoint, a minimum output, and thermo-off past the setpoint."""

    def __init__(self, mode):
        self.mode, self.i, self.on = mode, 0.0, True

    def output(self, T, sp, dt):
        """Heat added to the room in F/h (negative when cooling)."""
        err = (sp - T) if self.mode == "heat" else (T - sp)        # positive = demand
        if self.on and err < -OFF_ABOVE:
            self.on, self.i = False, 0.0
        elif not self.on and err > -ON_WITHIN:
            self.on = True
        if not self.on:
            return 0.0
        self.i = max(0.0, min(Q_MAX, self.i + KI * err * dt))
        q = max(Q_MIN, min(Q_MAX, KP * err + self.i))
        return q if self.mode == "heat" else -q


def _room_step(T, q, h, dt):
    return T + (q - LOSS * (T - outdoor_f(h)) + sun(h)) * dt


def stock_auto(seed=SEED, heat=None, cool=None):
    """Stock Auto (default 70/72, aimed at the band). Mode changes come only from the room crossing the other setpoint.
    The seed only adds display noise; the dynamics are seed-independent."""
    rng = random.Random(seed)
    heat = AUTO_HEAT if heat is None else heat
    cool = AUTO_COOL if cool is None else cool
    r, T, unit, past = Run(), 70.0, Unit("heat"), 0
    dt = STEP_MIN / 60
    for h in steps():
        # Auto changeover: the room has stayed beyond the other side's setpoint for CHANGEOVER_MIN.
        beyond = (T > cool) if unit.mode == "heat" else (T < heat)
        past = past + STEP_MIN if beyond else 0
        if past >= CHANGEOVER_MIN:
            unit, past = Unit("cool" if unit.mode == "heat" else "heat"), 0
        sp = heat if unit.mode == "heat" else cool
        q = unit.output(T, sp, dt)
        r.t.append(START + timedelta(hours=h)); r.outdoor.append(outdoor_f(h)); r.mode.append(unit.mode)
        r.room.append(round(T + rng.uniform(-0.15, 0.15), 2)); r.heat_sp.append(heat); r.cool_sp.append(cool)
        r.true.append(T)
        T = _room_step(T, q, h, dt)
    return r


def controller(seed=SEED, tuning=None):
    """The real decision code (logic.decide) every 10 min driving the same modulating unit.
    It reads a sensor that reports in 0.3F steps. The seed only adds display noise."""
    rng = random.Random(seed)
    t = tuning or Tuning()
    r, T = Run(), 70.0
    dt = STEP_MIN / 60
    state = default_state(0.0)
    unit = Unit("heat")
    sp, csp, tp = 68, t.cool_park, None
    for i, h in enumerate(steps()):
        if i % 2 == 0:
            meas = round(round(T / 0.3) * 0.3, 1)
            desired, state, info, _ = decide(meas, state, h * 3600, {"mode": unit.mode, "heat_setpoint": sp}, t)
            if desired["mode"] != unit.mode:
                unit = Unit(desired["mode"])
            sp, csp, tp = desired["heat_setpoint"], desired["cool_setpoint"], info["Tp"]
        q = unit.output(T, sp if unit.mode == "heat" else csp, dt)
        r.t.append(START + timedelta(hours=h)); r.outdoor.append(outdoor_f(h)); r.mode.append(unit.mode)
        r.room.append(round(T + rng.uniform(-0.1, 0.1), 2)); r.heat_sp.append(sp); r.cool_sp.append(csp)
        r.tp.append(tp); r.true.append(T)
        T = _room_step(T, q, h, dt)
    return r


def summarize(r: Run):
    """Shares of time below / in / above the band, flips and cooling hours (each sample = 5 min)."""
    n = len(r.room) - 1
    w = STEP_MIN / 60
    below = sum(1 for x in r.room[:-1] if x < BAND[0]) / n
    inb = sum(1 for x in r.room[:-1] if BAND[0] <= x <= BAND[1]) / n
    cool = sum(1 for m in r.mode[:-1] if m == "cool")
    cold_cool = sum(1 for m, o in zip(r.mode[:-1], r.outdoor[:-1]) if m == "cool" and o < 50)
    flips = sum(1 for a, b in zip(r.mode, r.mode[1:]) if a != b)
    return {"below": below, "in_band": inb, "above": 1 - below - inb, "flips": flips,
            "cool_h": cool * w, "cool_cold_h": cold_cool * w, "min": min(r.room), "max": max(r.room)}
