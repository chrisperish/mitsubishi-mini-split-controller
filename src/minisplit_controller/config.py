"""Configuration: built-in defaults, then an optional TOML file, then environment variables.

Precedence (highest wins): environment variable > config file > default.
The config file path comes from MSC_CONFIG (default: ./config.toml if it exists).
Credentials are never read from the config file; the Comfort client reads
MITSUBISHI_COMFORT_EMAIL and MITSUBISHI_COMFORT_PASSWORD from the environment.
"""
from __future__ import annotations

import os
import shlex
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Tuning:
    """Control constants. See README "Tuning" for why each value was chosen."""

    target_f: float = 70.0
    heat_min: int = 66                 # lowest heat setpoint the controller will send
    heat_max: int = 71                 # highest heat setpoint the controller will send
    cool_park: int = 76                # cool setpoint while in the heat regime
    heat_park: int = 66                # heat setpoint while in the cool regime
    fan: str = "auto"
    sensor_max_age_min: float = 20.0   # older wireless-sensor readings fall back to the unit's own temp
    history_h: float = 4.0             # readings kept in state for slope / trim / hold checks
    gap_max_min: float = 30.0          # a gap longer than this disables slope, regime switch and trim
    predict_h: float = 0.25            # Tp = T + predict_h * slope (15 minutes ahead)
    slope_limit: float = 2.0           # |slope| cap in F/h
    regime_min_h: float = 3.0          # normal minimum dwell between regime switches
    sun_dwell_h: float = 1.0           # shorter dwell for the solar-gain heat -> cool switch
    heat_to_cool_f: float = 73.0       # room must hold at or above this to start cooling
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
    # (threshold, setpoint): cool uses the first row with Tp <= threshold, else cool_floor.
    cool_table: list = field(default_factory=lambda: [(71.4, 78), (72.0, 77), (72.6, 76), (73.2, 75), (73.8, 74)])
    cool_floor: int = 73               # lowest cool setpoint the controller will send
    cool_max: int = 78                 # highest cool setpoint the controller will send


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
}


def _coerce(name: str, value):
    if name == "client_cmd":
        return shlex.split(value) if isinstance(value, str) else list(value)
    if name == "data_dir":
        return Path(value).expanduser()
    if name in ("latitude", "longitude"):
        return None if value in ("", None) else float(value)
    if name in ("interval_s", "cli_timeout_s", "cycle_watchdog_s"):
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
            if k not in names:
                raise ValueError(f"unknown tuning key: {k}")
            if k in ("heat_table", "cool_table"):
                v = [tuple(row) for row in v]
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
    _need(s.latitude is None or -90 <= s.latitude <= 90, "latitude must be -90..90")
    _need(s.longitude is None or -180 <= s.longitude <= 180, "longitude must be -180..180")
    _need(60 <= t.target_f <= 80, "target_f must be 60..80")
    _need(61 <= t.heat_min <= t.heat_max <= 80, "need 61 <= heat_min <= heat_max <= 80")
    _need(64 <= t.cool_floor <= t.cool_max <= 88, "need 64 <= cool_floor <= cool_max <= 88")
    _need(61 <= t.heat_park <= 80 and 64 <= t.cool_park <= 88, "park setpoints out of range")
    _need(t.cool_to_heat_f < t.heat_to_cool_f, "cool_to_heat_f must be below heat_to_cool_f")
    _need(t.trim_min <= 0 <= t.trim_max, "need trim_min <= 0 <= trim_max")
    _need(0 < t.trim_cover_h <= t.trim_every_h, "need 0 < trim_cover_h <= trim_every_h")
    _need(t.trim_deadband_f >= 0 and t.slope_limit > 0 and 0 <= t.predict_h <= 2, "deadband/slope/predict out of range")
    _need(0 < t.sun_dwell_h <= t.regime_min_h, "need 0 < sun_dwell_h <= regime_min_h")
    for name in ("sensor_max_age_min", "history_h", "gap_max_min", "step_min", "trim_every_h",
                 "heat_to_cool_hold_min", "cool_to_heat_hold_min", "outdoor_max_age_h"):
        _need(getattr(t, name) > 0, f"{name} must be > 0")
    for name, lo, hi in (("heat_table", t.heat_min, t.heat_max), ("cool_table", t.cool_floor, t.cool_max)):
        rows = getattr(t, name)
        _need(len(rows) > 0 and all(len(r) == 2 for r in rows), f"{name} must be [[threshold, setpoint], ...]")
        _need(all(lo <= sp <= hi for _, sp in rows), f"{name} setpoints must be within {lo}..{hi}")
