"""Pure control logic: no I/O, no clock, fully unit-testable.

The controller keeps the unit in one of two regimes and never uses the unit's own Auto mode:

* heat (default): the heat setpoint (66-71F) is picked from the predicted room temperature
  Tp = T + 0.25 h * slope, plus a slow trim. The cool setpoint is parked out of the way.
* cool: only when the room holds >= min(73F, cool target + 1.5F) for 20 minutes (solar gain, for
  example). The cool setpoint (71-78F) is picked from Tp minus an outdoor-adaptive cool target
  (70.5F when it is 55F or colder outside, rising linearly to 73.5F at 80F or warmer), never below
  an outdoor-dependent floor. Without a fresh outdoor reading the fixed cool table (73-78F) is used.
  The heat setpoint is parked at its minimum.
* fan: staged from the error e (cool: Tp - cool target, heat: target - Tp), quiet up to superPowerful.
  Steps up immediately (at most 2 stages per cycle), steps down only after e is 0.3F below the lower
  stage's threshold on 2 consecutive cycles, and holds the current speed after a gap over 30 minutes.
  The vane is never touched.

Timestamps are Unix seconds. Temperatures are Fahrenheit.
"""
from __future__ import annotations

from .config import Tuning


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def default_state(now_ts=None) -> dict:
    """Fresh controller state. Persisted as JSON between cycles."""
    return {"regime": "heat", "regime_since": now_ts, "history": [], "sp": None, "sp_changed_at": None,
            "cool_sp": None, "cool_sp_changed_at": None, "trim": 0, "last_trim_at": now_ts,
            "last_ok_ts": None, "backoff_until": 0}


def held(history, cond, n_min, now_ts, gap_max_min=30.0) -> bool:
    """True if the room has satisfied `cond` for the last `n_min` minutes.

    Requires at least 3 readings, every reading in the window meets `cond`, the oldest reading in
    the window is at least `n_min` old, and no gap inside the window exceeds `gap_max_min`.
    `history` is [(ts, T)] ascending and includes the current reading.
    """
    win = []
    for ts, t in reversed(history):
        win.append((ts, t))
        if now_ts - ts >= n_min * 60:
            break
    if len(win) < 3 or now_ts - win[-1][0] < n_min * 60:
        return False
    if not all(cond(t) for _, t in win):
        return False
    return all(a[0] - b[0] <= gap_max_min * 60 for a, b in zip(win, win[1:]))


def slope(history, now_ts, T, g, gap_max_min=30.0, limit=2.0):
    """Rate of change in F/h against the reading aged 20-60 min that is closest to 30 min old.

    Returns (slope, reference_age_min). Slope is 0 when there is no reference reading or when the
    gap since the last good reading (g, minutes) is too long to trust.
    """
    if g is None or g > gap_max_min:
        return 0.0, None
    cands = [(ts, t) for ts, t in history if 20 * 60 <= now_ts - ts <= 60 * 60]
    if not cands:
        return 0.0, None
    ts, t = min(cands, key=lambda r: abs((now_ts - r[0]) - 30 * 60))
    return clamp((T - t) / ((now_ts - ts) / 3600), -limit, limit), (now_ts - ts) / 60


def weighted_mean(history, now_ts, span_s, gap_max_min=30.0):
    """Trapezoid time-weighted mean over the last span_s seconds.

    Segments longer than gap_max_min are skipped. Returns (mean or None, covered_seconds).
    """
    pts = [(ts, t) for ts, t in history if now_ts - ts <= span_s]
    tot = cov = 0.0
    for (a, ta), (b, tb) in zip(pts, pts[1:]):
        d = b - a
        if 0 < d <= gap_max_min * 60:
            tot += d * (ta + tb) / 2
            cov += d
    return (tot / cov if cov else None), cov


def outdoor_ok(s, now_ts, t: Tuning):
    """(outdoor is warm enough and fresh, human-readable description)."""
    f, ts = s.get("outdoor_f"), s.get("outdoor_ts")
    if f is None or ts is None:
        return False, "outdoor n/a"
    age = now_ts - ts
    if age > t.outdoor_max_age_h * 3600:
        return False, f"outdoor {f:.1f}F stale ({age / 3600:.1f} h)"
    return f >= t.outdoor_min_f, f"outdoor {f:.1f}F"


def heat_base(tp, t: Tuning) -> int:
    """Heat setpoint from the table: the warmer the predicted room, the lower the setpoint."""
    for x, sp in t.heat_table:
        if tp >= x:
            return sp
    return t.heat_max


def outdoor_fresh(s, now_ts, t: Tuning):
    """Outdoor temperature (F) if present and at most outdoor_max_age_h old, else None."""
    f, ts = s.get("outdoor_f"), s.get("outdoor_ts")
    if f is None or ts is None or now_ts - ts > t.outdoor_max_age_h * 3600:
        return None
    return float(f)


def adaptive_target(out_f, t: Tuning):
    """Room cool target from outdoor temp, linear between the two anchor points; None without data."""
    if out_f is None or not t.adaptive_cool:
        return None
    x = clamp((out_f - t.cool_target_out_lo_f) / (t.cool_target_out_hi_f - t.cool_target_out_lo_f), 0.0, 1.0)
    return round(t.cool_target_lo_f + x * (t.cool_target_hi_f - t.cool_target_lo_f), 2)


def cool_floor(out_f, t: Tuning) -> int:
    """Lowest cool setpoint allowed for this outdoor temp: first row with outdoor < x, else cool_floor."""
    if out_f is not None and t.adaptive_cool:
        for x, sp in t.cool_floor_table:
            if out_f < x:
                return sp
    return t.cool_floor


def heat_to_cool_f(out_f, t: Tuning) -> float:
    """Heat -> cool entry threshold: min(heat_to_cool_f, target + margin), or heat_to_cool_f without data."""
    tgt = adaptive_target(out_f, t)
    return t.heat_to_cool_f if tgt is None else round(min(t.heat_to_cool_f, tgt + t.heat_to_cool_margin_f), 2)


def cool_target(tp, t: Tuning, out_f=None) -> int:
    """Cool setpoint for predicted temp tp: the cooler the predicted room, the higher the setpoint.

    With a fresh outdoor reading: first row of cool_offsets with tp <= target + offset, else
    cool_offsets_else, never below cool_floor(out_f). Without one: the fixed cool_table.
    """
    tgt = adaptive_target(out_f, t)
    if tgt is None:
        for x, sp in t.cool_table:
            if tp <= x:
                return sp
        return t.cool_floor
    sp = t.cool_offsets_else
    for off, v in t.cool_offsets:
        if tp <= tgt + off + 1e-9:
            sp = v
            break
    return max(sp, cool_floor(out_f, t))


def fan_raw_stage(e, t: Tuning) -> int:
    """Index of the first fan stage with e <= its threshold (the last stage if none)."""
    for i, (thr, _) in enumerate(t.fan_stages):
        if e <= thr + 1e-9:
            return i
    return len(t.fan_stages) - 1


def fan_stage(e, prev, down_count, t: Tuning):
    """Next fan stage. Returns (stage, down_count).

    Up: immediately, at most fan_max_up stages per cycle. Down: only after
    e <= (threshold of the lower stage) - fan_down_hyst_f on fan_down_cycles consecutive cycles.
    """
    raw = fan_raw_stage(e, t)
    if prev is None:
        return raw, 0
    prev = min(prev, len(t.fan_stages) - 1)
    if raw > prev:
        return min(raw, prev + t.fan_max_up), 0
    down = fan_raw_stage(e + t.fan_down_hyst_f, t)
    if down < prev:
        down_count += 1
        if down_count >= t.fan_down_cycles:
            return down, 0
        return prev, down_count
    return prev, 0


def fan_error(Tp, regime, out_f, t: Tuning) -> float:
    """Fan staging error: cool = Tp - cool target (default when no outdoor data), heat = target_f - Tp."""
    if regime == "cool":
        tgt = adaptive_target(out_f, t)
        return Tp - (t.fan_cool_target_default_f if tgt is None else tgt)
    return t.target_f - Tp


def decide(T, state, now_ts, live=None, t: Tuning | None = None):
    """One control decision.

    T: current room temperature (F). state: persisted dict (see default_state). live: the unit's
    current status dict (used only to adopt an existing heat setpoint on first run).
    Returns (desired settings dict, new state, info dict, list of event strings).
    """
    t = t or Tuning()
    s = dict(default_state(now_ts), **(state or {}))
    live = live or {}
    events = []
    gmax = t.gap_max_min
    hist = [(float(a), float(b)) for a, b in s["history"]
            if now_ts - float(a) <= t.history_h * 3600 and a < now_ts]
    g = None if s.get("last_ok_ts") is None else (now_ts - s["last_ok_ts"]) / 60
    S, ref_age = slope(hist, now_ts, T, g, gmax, t.slope_limit)
    Tp = T + t.predict_h * S
    hist.append((now_ts, T))
    s["history"] = [[round(a, 1), b] for a, b in hist]
    s["last_ok_ts"] = now_ts
    fresh = g is not None and g <= gmax

    # --- regime switch (only on fresh data, never faster than the short sun dwell)
    pending_note = None
    out_f = outdoor_fresh(s, now_ts, t)
    h2c = heat_to_cool_f(out_f, t)
    sun_f = min(t.sun_override_f, h2c)
    dwell = None if s["regime_since"] is None else now_ts - s["regime_since"]
    if fresh and (dwell is None or dwell >= t.sun_dwell_h * 3600):
        new = None
        held_min = t.cool_to_heat_hold_min
        full_dwell = dwell is None or dwell >= t.regime_min_h * 3600
        if s["regime"] == "heat" and held(hist, lambda x: x >= h2c,
                                          t.heat_to_cool_hold_min, now_ts, gmax):
            held_min = t.heat_to_cool_hold_min
            out_ok, out_txt = outdoor_ok(s, now_ts, t)
            sun = held(hist, lambda x: x >= sun_f, t.heat_to_cool_hold_min, now_ts, gmax)
            if sun:
                new = "cool"   # solar-gain rule: 1 h dwell, bypasses the outdoor gate
            elif out_ok and full_dwell:
                new = "cool"   # warm outside: normal 3 h dwell
            else:
                pending_note = f"cool switch blocked: {out_txt}, room not held >= {sun_f}"
        elif (s["regime"] == "cool" and full_dwell
              and held(hist, lambda x: x <= t.cool_to_heat_f, t.cool_to_heat_hold_min, now_ts, gmax)):
            new = "heat"
        if new:
            events.append(f"regime change: {s['regime']} -> {new} (T {T:.1f}, held {held_min:.0f} min; "
                          f"{outdoor_ok(s, now_ts, t)[1]})")
            s["regime"], s["regime_since"] = new, now_ts
            if new == "cool":
                s["cool_sp"], s["cool_sp_changed_at"] = None, None  # first cool SP goes straight to the table

    # --- trim: every 3 h, nudge by 1F if the time-weighted mean drifted out of the deadband
    if fresh and (s["last_trim_at"] is None or now_ts - s["last_trim_at"] >= t.trim_every_h * 3600):
        m, cov = weighted_mean(hist, now_ts, t.trim_every_h * 3600, gmax)
        s["last_trim_at"] = now_ts
        if m is not None and cov >= t.trim_cover_h * 3600:
            old = s["trim"]
            step = 1 if m < t.target_f - t.trim_deadband_f else -1 if m > t.target_f + t.trim_deadband_f else 0
            nt = clamp(old + step, t.trim_min, t.trim_max)
            if nt != old:
                s["trim"] = nt
                events.append(f"trim change: {old:+d} -> {nt:+d} (3 h mean {m:.2f}F)")

    step_s = t.step_min * 60
    if s["regime"] == "heat":
        base = heat_base(Tp, t)
        want = clamp(base + s["trim"], t.heat_min, t.heat_max)
        sp = s["sp"]
        if sp is None:
            sp = live.get("heat_setpoint") if live.get("mode") == "heat" else None
        if sp is None:
            new_sp = want
        elif want > sp:   # raising heat is the risky direction: 1F per step
            ok = s["sp_changed_at"] is None or now_ts - s["sp_changed_at"] >= step_s
            new_sp = sp + 1 if ok else sp
        else:             # lowering heat happens right away, before the room overshoots
            new_sp = want
        new_sp = int(clamp(new_sp, t.heat_min, t.heat_max))
        if new_sp != s["sp"]:
            if s["sp"] is not None:
                events.append(f"heat SP {s['sp']} -> {new_sp} (base {base}, want {want})")
            s["sp"], s["sp_changed_at"] = new_sp, now_ts
        desired = {"mode": "heat", "heat_setpoint": new_sp, "cool_setpoint": t.cool_park, "fan": t.fan}
        info = {"base": base, "want": want}
    else:
        want = cool_target(Tp, t, out_f)
        sp = s["cool_sp"]
        if sp is None or want >= sp:   # raising the cool setpoint (less cooling) is immediate
            new_sp = want
        else:                          # lowering it (more cooling) is rate limited
            ok = s["cool_sp_changed_at"] is None or now_ts - s["cool_sp_changed_at"] >= step_s
            new_sp = sp - 1 if ok else sp
        new_sp = int(clamp(new_sp, cool_floor(out_f, t), t.cool_max))
        if new_sp != s["cool_sp"]:
            if s["cool_sp"] is not None:
                events.append(f"cool SP {s['cool_sp']} -> {new_sp} (want {want})")
            s["cool_sp"], s["cool_sp_changed_at"] = new_sp, now_ts
        desired = {"mode": "cool", "heat_setpoint": t.heat_park, "cool_setpoint": new_sp, "fan": t.fan}
        info = {"want": want, "floor": cool_floor(out_f, t)}
    # --- fan staging: only on fresh data (or the very first reading); after a long gap keep the speed
    if t.fan_staging:
        err = fan_error(Tp, s["regime"], out_f, t)
        prev = s.get("fan_stage")
        if prev is not None and not 0 <= prev < len(t.fan_stages):
            prev = None
        if fresh or g is None:
            stage, s["fan_down_count"] = fan_stage(err, prev, s.get("fan_down_count", 0), t)
            if prev is not None and stage != prev:
                events.append(f"fan stage {prev} -> {stage} ({t.fan_stages[prev][1]} -> "
                              f"{t.fan_stages[stage][1]}, e {err:+.2f})")
            s["fan_stage"] = stage
            fan = t.fan_stages[stage][1]
        else:
            fan = live.get("fan") or (t.fan_stages[prev][1] if prev is not None else t.fan)
        desired["fan"] = fan
        info.update({"fan_err": round(err, 2), "fan_stage": s.get("fan_stage"), "fan": fan})
    info.update({"T": T, "Tp": round(Tp, 2), "S": round(S, 2), "g": None if g is None else round(g, 1),
                 "ref_age_min": None if ref_age is None else round(ref_age), "trim": s["trim"],
                 "regime": s["regime"], "outdoor": outdoor_ok(s, now_ts, t)[1], "blocked": pending_note,
                 "cool_target": adaptive_target(out_f, t), "h2c": h2c})
    return desired, s, info, events


def needed_changes(live, desired) -> dict:
    """Diff desired vs live settings. Returns client `set` arguments for only what differs."""
    ch = {}
    if live.get("mode") != desired["mode"]:
        ch["mode"] = desired["mode"]
    if live.get("heat_setpoint") != desired["heat_setpoint"]:
        ch["heat-temp"] = int(desired["heat_setpoint"])
    if live.get("cool_setpoint") != desired["cool_setpoint"]:
        ch["cool-temp"] = int(desired["cool_setpoint"])
    if live.get("fan") != desired["fan"]:
        ch["fan"] = desired["fan"]
    return ch


def info_text(i) -> str:
    """One-line summary written to the log's action column (parsed by scripts/make_charts.py)."""
    sl = f"S {i['S']:+.2f}F/h" + (f" vs {i['ref_age_min']}m ago" if i.get("ref_age_min") else " (no ref)")
    g = "first reading" if i["g"] is None else f"g {i['g']:.0f}m"
    return (f"{i.get('outdoor', 'outdoor n/a')}; T {i['T']:.1f} Tp {i['Tp']:.2f} {sl} {g} "
            f"trim {i['trim']:+d} regime {i['regime']}"
            + (f" ctgt {i['cool_target']:.1f}" if i.get("cool_target") is not None else " ctgt table")
            + (f" h2c {i['h2c']:.1f}" if i.get("h2c") is not None else "")
            + (f" fan e {i['fan_err']:+.2f} stage {i['fan_stage']} {i['fan']}" if "fan_err" in i else ""))


def next_due(due, started, finished, interval, late_tol=60):
    """Scheduling: on time -> next grid slot; ran late or overran -> now + interval."""
    if started - due > late_tol:
        return finished + interval
    nxt = due + interval
    return nxt if nxt > finished else finished + interval
