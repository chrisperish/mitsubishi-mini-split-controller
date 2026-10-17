"""Configuration: built-in defaults, then an optional TOML file, then environment variables.

Precedence (highest wins): environment variable > config file > default.
The config file path comes from MSC_CONFIG (default: ./config.toml if it exists).
Credentials are never read from the config file; the Comfort client reads
MITSUBISHI_COMFORT_EMAIL and MITSUBISHI_COMFORT_PASSWORD from the environment.
"""
from __future__ import annotations

import math
import os
import shlex
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Fan speed names the Comfort client accepts (client/mitsubishi.py ALL_FANS; a test keeps them in sync).
# A given unit may support fewer; the client rejects those at send time.
FAN_SPEEDS = ("superQuiet", "quiet", "low", "powerful", "superPowerful", "auto")


@dataclass
class Tuning:
    """Control constants. See README "Tuning" for why each value was chosen."""

    target_f: float = 70.0
    heat_min: int = 66                 # lowest heat setpoint the controller will send
    heat_max: int = 71                 # highest heat setpoint the controller will send
    cool_park: int = 76                # cool setpoint while in the heat regime
    heat_park: int = 66                # heat setpoint while in the cool regime
    fan: str = "auto"                  # fan speed when fan_staging is off (or no stage is known yet on a long gap)
    # Fan staging: speed from the error e (cool: Tp - cool target, heat: target_f - Tp).
    fan_staging: bool = True
    # (threshold, speed): the first row with e <= threshold wins. Speeds are client fan names.
    fan_stages: list = field(default_factory=lambda: [(0.3, "quiet"), (1.5, "low"), (2.5, "powerful"),
                                                      (float("inf"), "superPowerful")])
    fan_down_hyst_f: float = 0.3       # step down only when e <= (lower stage's threshold - this) ...
    fan_down_cycles: int = 2           # ... on this many consecutive cycles
    fan_max_up: int = 2                # step up immediately, at most this many stages per cycle
    fan_cool_target_default_f: float = 72.0   # cool target for fan staging when there is no outdoor reading
    sensor_max_age_min: float = 20.0   # older wireless-sensor readings fall back to the unit's own temp
    history_h: float = 4.0             # readings kept in state for slope / trim / hold checks
    gap_max_min: float = 30.0          # a gap longer than this disables slope, regime switch and trim
    predict_h: float = 0.25            # Tp = T + predict_h * slope (15 minutes ahead)
    slope_limit: float = 2.0           # |slope| cap in F/h
    regime_min_h: float = 3.0          # normal minimum dwell between regime switches
    sun_dwell_h: float = 1.0           # shorter dwell for the solar-gain heat -> cool switch
    heat_to_cool_f: float = 73.0       # room must hold at or above this to start cooling (upper cap
                                       # when outdoor data is fresh: min(this, cool target + margin))
    heat_to_cool_hold_min: float = 20.0
    sun_override_f: float = 73.0       # holding >= this for the hold window switches to cool on a 1 h dwell,
                                       # even when it is cold outside (solar gain)
    cool_to_heat_f: float = 68.0       # room must hold at or below this to go back to heat
    cool_to_heat_hold_min: float = 45.0
    outdoor_min_f: float = 62.0        # alternative heat -> cool path: warm outside (3 h dwell)
    outdoor_max_age_h: float = 2.0
    step_min: float = 15.0             # slow direction of setpoint moves: 1F per step_min
    trim_every_h: float = 3.0
    trim_cover_h: float = 2.5          # trim needs this much readings coverage in the window
    trim_deadband_f: float = 0.4
    trim_min: int = -1
    trim_max: int = 2
    # (threshold, setpoint): heat uses the first row with Tp >= threshold, else heat_max.
    heat_table: list = field(default_factory=lambda: [(70.6, 66), (70.0, 67), (69.4, 68), (68.8, 69), (68.2, 70)])
    # Cool target: outdoor-adaptive with an outdoor reading at most outdoor_max_age_h old, linear between
    # (cool_target_out_lo_f, cool_target_lo_f) and (cool_target_out_hi_f, cool_target_hi_f), held flat
    # outside that range; cool_fallback_target_f without a fresh reading (or with adaptive_cool = false).
    adaptive_cool: bool = True
    cool_target_out_lo_f: float = 55.0
    cool_target_out_hi_f: float = 80.0
    cool_target_lo_f: float = 70.0
    cool_target_hi_f: float = 73.5
    cool_fallback_target_f: float = 72.0
    heat_to_cool_margin_f: float = 1.5  # heat -> cool entry = min(heat_to_cool_f, adaptive target + this)
    # Cool setpoint: u = clamp(target + cool_trim - cool_gain * (Tp - target), floor, cool_max),
    # floor = ceil(target - cool_floor_below_f), sent as floor(u + 0.5).
    cool_gain: float = 1.5
    cool_floor_below_f: float = 1.5
    cool_max: int = 78                 # highest cool setpoint the controller will send
    cool_hyst_f: float = 0.75          # change the cool setpoint only when |u - current| >= this
    # Cool drops: at most once per step_min, 1F, or cool_big_step when Tp - target > cool_big_err_f. Raises: at once.
    cool_big_err_f: float = 1.5
    cool_big_step: int = 2
    # Cool trim (cool regime only): every trim_every_h, -1 if the mean is above target + cool_trim_band_f,
    # +1 if below target - cool_trim_band_f, kept within cool_trim_min..cool_trim_max; reset on entering cool.
    cool_trim_band_f: float = 0.3
    cool_trim_min: int = -2
    cool_trim_max: int = 0
    # Unit-vs-sensor offset (log only, no control effect): EMA weight, clamp, mean window, alert threshold.
    unit_diff_alpha: float = 0.3
    unit_diff_max_f: float = 3.0
    unit_diff_window_h: float = 6.0
    unit_diff_alert_f: float = 1.5


# Keys from earlier releases that no longer exist, with what replaced them.
REMOVED_TUNING = {
    "cool_table": "the cool setpoint is now computed from the target (cool_gain, cool_fallback_target_f)",
    "cool_offsets": "the cool setpoint is now computed from the target (cool_gain)",
    "cool_offsets_else": "the cool setpoint is now computed from the target (cool_gain)",
    "cool_floor_table": "the floor is now ceil(target - cool_floor_below_f)",
    "cool_floor": "the floor is now ceil(target - cool_floor_below_f)",
    "cool_min": "the floor is now ceil(target - cool_floor_below_f)",
}


@dataclass
class Settings:
    """Runtime settings (where things live, which unit, how to reach the cloud client)."""

    device: str = ""                   # unit serial or name as shown by `mitsubishi.py status`
    sensor_name: str = ""              # optional: wireless sensor name, if not linked by serial
    client_cmd: list = field(default_factory=lambda: ["python3", str(REPO_ROOT / "client" / "mitsubishi.py")])
    data_dir: Path = REPO_ROOT / "var"  # state.json, log.csv, pid, heartbeat, locks
    latitude: float | None = None      # outdoor temperature (Open-Meteo); omit to disable the outdoor gate
    longitude: float | None = None
    interval_s: int = 600
    timezone: str = "America/Los_Angeles"   # IANA zone for log timestamps (env: MSC_TIMEZONE)
    cli_timeout_s: int = 90
    cycle_watchdog_s: int = 360
    skip_recent_s: int = 300           # loop skips its cycle if another (e.g. kick) cycle ran this recently;
                                       # clamped to interval_s / 2 at config load
    tuning: Tuning = field(default_factory=Tuning)

    @property
    def log_csv(self) -> Path:
        return self.data_dir / "log.csv"


ENV = {  # env var -> Settings attribute
    "MSC_DEVICE": "device",
    "MSC_SENSOR_NAME": "sensor_name",
    "MSC_CLIENT_CMD": "client_cmd",
    "MSC_DATA_DIR": "data_dir",
    "MSC_LATITUDE": "latitude",
    "MSC_LONGITUDE": "longitude",
    "MSC_INTERVAL_S": "interval_s",
    "MSC_TIMEZONE": "timezone",
    "MSC_SKIP_RECENT_S": "skip_recent_s",
}


def _coerce(name: str, value):
    if name == "client_cmd":
        return shlex.split(value) if isinstance(value, str) else list(value)
    if name == "data_dir":
        return Path(value).expanduser()
    if name in ("latitude", "longitude"):
        return None if value in ("", None) else float(value)
    if name in ("interval_s", "cli_timeout_s", "cycle_watchdog_s", "skip_recent_s"):
        return int(value)
    return value


def load(path: str | os.PathLike | None = None, env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    path = path or env.get("MSC_CONFIG") or ("config.toml" if Path("config.toml").exists() else None)
    if path:
        data = tomllib.loads(Path(path).read_text())
        for k, v in data.get("controller", {}).items():
            if hasattr(s, k) and k != "tuning":
                setattr(s, k, _coerce(k, v))
        names = {f.name for f in fields(Tuning)}
        for k, v in data.get("tuning", {}).items():
            if k in REMOVED_TUNING:
                raise ValueError(f"tuning key {k!r} was removed: {REMOVED_TUNING[k]} (see README \"Tuning\")")
            if k not in names:
                raise ValueError(f"unknown tuning key: {k}")
            if k in ("heat_table", "fan_stages"):
                v = [tuple(row) for row in v]
                if k == "fan_stages":   # TOML: use inf for the last threshold
                    v = [(float(x), str(sp)) for x, sp in v]
            setattr(s.tuning, k, v)
    for var, attr in ENV.items():
        if env.get(var) not in (None, ""):
            setattr(s, attr, _coerce(attr, env[var]))
    validate(s)
    return s


def _need(ok, msg):
    if not ok:
        raise ValueError(f"invalid config: {msg}")


def validate(s: Settings) -> None:
    """Reject settings that could drive the unit somewhere unsafe or make the logic meaningless."""
    t = s.tuning
    try:
        ZoneInfo(s.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"invalid config: unknown timezone {s.timezone!r}") from None
    _need(60 <= s.interval_s <= 3600, "interval_s must be 60..3600")
    _need(0 <= s.skip_recent_s <= 3600, "skip_recent_s must be 0..3600")
    # Above interval_s / 2 the loop would also skip right after its own cycle: clamp it.
    s.skip_recent_s = min(s.skip_recent_s, s.interval_s // 2)
    _need(s.latitude is None or -90 <= s.latitude <= 90, "latitude must be -90..90")
    _need(s.longitude is None or -180 <= s.longitude <= 180, "longitude must be -180..180")
    _need(60 <= t.target_f <= 80, "target_f must be 60..80")
    _need(61 <= t.heat_min <= t.heat_max <= 80, "need 61 <= heat_min <= heat_max <= 80")
    _need(isinstance(t.adaptive_cool, bool), "adaptive_cool must be true or false")
    _need(-20 <= t.cool_target_out_lo_f < t.cool_target_out_hi_f <= 120,
          "need -20 <= cool_target_out_lo_f < cool_target_out_hi_f <= 120")
    _need(65 <= t.cool_target_lo_f <= t.cool_target_hi_f <= 80, "need 65 <= cool_target_lo_f <= cool_target_hi_f <= 80")
    _need(0 <= t.heat_to_cool_margin_f <= 5, "heat_to_cool_margin_f must be 0..5")
    _need(t.cool_to_heat_f < min(t.heat_to_cool_f, t.cool_target_lo_f + t.heat_to_cool_margin_f),
          "cool_to_heat_f must be below cool_target_lo_f + heat_to_cool_margin_f")
    _need(65 <= t.cool_fallback_target_f <= 80, "cool_fallback_target_f must be 65..80")
    _need(0 < t.cool_gain <= 5, "cool_gain must be > 0 and <= 5")
    _need(0 <= t.cool_floor_below_f <= 5, "cool_floor_below_f must be 0..5")
    lowest = math.ceil(min(t.cool_target_lo_f, t.cool_fallback_target_f) - t.cool_floor_below_f - 1e-9)
    highest = math.ceil(max(t.cool_target_hi_f, t.cool_fallback_target_f) - t.cool_floor_below_f - 1e-9)
    _need(lowest >= 64, f"lowest cool floor would be {lowest}F; must be >= 64")
    _need(highest <= t.cool_max <= 88, f"need highest cool floor ({highest}F) <= cool_max <= 88")
    _need(0 <= t.cool_hyst_f <= 3, "cool_hyst_f must be 0..3")
    _need(0 <= t.cool_trim_band_f <= 3, "cool_trim_band_f must be 0..3")
    _need(-5 <= t.cool_trim_min <= 0 <= t.cool_trim_max <= 5, "need -5 <= cool_trim_min <= 0 <= cool_trim_max <= 5")
    _need(0 <= t.unit_diff_alpha <= 1, "unit_diff_alpha must be 0..1")
    _need(t.unit_diff_max_f > 0 and t.unit_diff_window_h > 0 and t.unit_diff_alert_f >= 0,
          "unit_diff_max_f and unit_diff_window_h must be > 0, unit_diff_alert_f >= 0")
    _need(isinstance(t.fan_staging, bool), "fan_staging must be true or false")
    fs = t.fan_stages
    _need(len(fs) > 0 and all(len(r) == 2 for r in fs), "fan_stages must be [[threshold, speed], ...]")
    _need(all(a[0] < b[0] for a, b in zip(fs, fs[1:])), "fan_stages thresholds must increase")
    bad = [sp for _, sp in fs if sp not in FAN_SPEEDS]
    _need(not bad, f"fan_stages has unknown fan speed(s) {', '.join(map(repr, bad))}; "
                   f"supported: {', '.join(FAN_SPEEDS)}")
    _need(t.fan in FAN_SPEEDS, f"unknown fan speed {t.fan!r}; supported: {', '.join(FAN_SPEEDS)}")
    _need(0 <= t.fan_down_hyst_f <= 5, "fan_down_hyst_f must be 0..5")
    _need(t.fan_down_cycles >= 1 and t.fan_max_up >= 1, "fan_down_cycles and fan_max_up must be >= 1")
    _need(0 <= t.cool_big_err_f <= 5, "cool_big_err_f must be 0..5")
    _need(1 <= t.cool_big_step <= 3, "cool_big_step must be 1..3")
    _need(65 <= t.fan_cool_target_default_f <= 80, "fan_cool_target_default_f must be 65..80")
    _need(61 <= t.heat_park <= 80 and 64 <= t.cool_park <= 88, "park setpoints out of range")
    _need(t.cool_to_heat_f < t.heat_to_cool_f, "cool_to_heat_f must be below heat_to_cool_f")
    _need(t.trim_min <= 0 <= t.trim_max, "need trim_min <= 0 <= trim_max")
    _need(0 < t.trim_cover_h <= t.trim_every_h, "need 0 < trim_cover_h <= trim_every_h")
    _need(t.trim_deadband_f >= 0 and t.slope_limit > 0 and 0 <= t.predict_h <= 2, "deadband/slope/predict out of range")
    _need(0 < t.sun_dwell_h <= t.regime_min_h, "need 0 < sun_dwell_h <= regime_min_h")
    for name in ("sensor_max_age_min", "history_h", "gap_max_min", "step_min", "trim_every_h",
                 "heat_to_cool_hold_min", "cool_to_heat_hold_min", "outdoor_max_age_h"):
        _need(getattr(t, name) > 0, f"{name} must be > 0")
    for name, lo, hi in (("heat_table", t.heat_min, t.heat_max),):
        rows = getattr(t, name)
        _need(len(rows) > 0 and all(len(r) == 2 for r in rows), f"{name} must be [[threshold, setpoint], ...]")
        _need(all(lo <= sp <= hi for _, sp in rows), f"{name} setpoints must be within {lo}..{hi}")
