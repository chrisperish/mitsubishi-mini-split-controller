"""Illustrative simulation used for the README charts. Not measured data.

A one-room thermal model is run for 24 h (6 PM to 6 PM) at 5-minute steps, twice, under the same
outdoor temperature curve:

* Stock Auto: heat setpoint 68F, cool setpoint 72F. The unit keeps heating past its setpoint until
  the room crosses the cool side, flips to cool, overcools, and flips back.
* Controller: the real decision code (minisplit_controller.logic.decide) drives the same room.

Everything is deterministic: noise comes from random.Random(SEED).
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
SUN_PEAK = 0.8       # F/h of solar gain at 1 PM
HEAT_MAX = 3.5       # F/h the unit can add at full output
HEAT_GAIN = 2.0      # 1/h: inverter output per F of error
HEAT_OFFSET = 2.8    # F: the unit settles this far above its heat setpoint (sensor at the unit runs cool)
TRIM0 = 1
COOL_MAX = 3.0       # F/h the unit can remove at full output
AUTO_HEAT_P = 1.8    # F/h: Auto heating runs at a fixed output until it crosses the cool side
AUTO_FLIP_UP = 72.5  # Auto flips heat -> cool here (about 0.5F past the cool setpoint)
AUTO_FLIP_DOWN = 67.5  # and cool -> heat here (about 0.5F below the heat setpoint)


@dataclass
class Run:
    t: list = field(default_factory=list)        # datetimes
    room: list = field(default_factory=list)     # F
    outdoor: list = field(default_factory=list)  # F
    mode: list = field(default_factory=list)     # "heat" / "cool"
    heat_sp: list = field(default_factory=list)
    cool_sp: list = field(default_factory=list)
    tp: list = field(default_factory=list)       # controller only: predicted temp 15 min ahead


def outdoor_f(h):
    """Smooth outdoor curve: about 45F through the night, about 60F at midday (h = hours since 6 PM)."""
    clock = (18 + h) % 24
    return 52.5 + 7.5 * math.cos((clock - 13) / 24 * 2 * math.pi)   # 60F at 1 PM, 45F at 1 AM


def sun(h):
    clock = (18 + h) % 24
    return SUN_PEAK * max(0.0, math.sin((clock - 8) / 10 * math.pi)) if 8 <= clock <= 18 else 0.0


def steps():
    n = HOURS * 60 // STEP_MIN
    return [i * STEP_MIN / 60 for i in range(n + 1)]


def stock_auto(seed=SEED):
    rng = random.Random(seed)
    r, T, mode = Run(), 70.0, "heat"
    dt = STEP_MIN / 60
    for h in steps():
        out = outdoor_f(h)
        if mode == "heat" and T >= AUTO_FLIP_UP:
            mode = "cool"
        elif mode == "cool" and T <= AUTO_FLIP_DOWN:
            mode = "heat"
        power = AUTO_HEAT_P if mode == "heat" else -COOL_MAX
        r.t.append(START + timedelta(hours=h)); r.outdoor.append(out); r.mode.append(mode)
        r.room.append(round(T + rng.uniform(-0.15, 0.15), 2)); r.heat_sp.append(68); r.cool_sp.append(72)
        T += (power - LOSS * (T - out) + sun(h)) * dt
    return r


def controller(seed=SEED, tuning=None):
    rng = random.Random(seed)
    t = tuning or Tuning()
    r, T = Run(), 70.4
    dt = STEP_MIN / 60
    state = default_state(0.0)
    state["trim"] = TRIM0            # as if an earlier 3 h trim had found this room running cool
    sp, mode, csp = 68, "heat", t.cool_park
    for i, h in enumerate(steps()):
        out = outdoor_f(h)
        shown = round(T + rng.uniform(-0.1, 0.1), 2)   # plotted room temp: +/-0.1F noise
        if i % 2 == 0:                       # the controller runs every 10 minutes
            # The controller sees a sensor that reports in 0.3F steps, like the wireless sensor.
            meas = round(round(T / 0.3) * 0.3, 1)
            desired, state, info, _ = decide(meas, state, h * 3600, {"mode": mode, "heat_setpoint": sp}, t)
            sp, csp, mode = desired["heat_setpoint"], desired["cool_setpoint"], desired["mode"]
            tp = info["Tp"]
        if mode == "heat":   # inverter: output proportional to the error against setpoint + offset
            power = max(0.0, min(HEAT_MAX, HEAT_GAIN * (sp + HEAT_OFFSET - T)))
        else:
            power = -max(0.0, min(COOL_MAX, 2.0 * (T - csp)))
        r.t.append(START + timedelta(hours=h)); r.outdoor.append(out); r.mode.append(mode)
        r.room.append(shown); r.heat_sp.append(sp); r.cool_sp.append(csp)
        r.tp.append(tp)
        T += (power - LOSS * (T - out) + sun(h)) * dt
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
