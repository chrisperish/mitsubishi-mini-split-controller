#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""mitsubishi - control Mitsubishi mini-splits in the Mitsubishi Comfort app (formerly Kumo Cloud).

The Comfort app talks to the Kumo Cloud v3 backend: REST at app-prod.kumocloud.com and
Socket.IO (engine.io 4) at socket-prod.kumocloud.com. The protocol details follow:
  - ventz/mitsubishi-comfort-api-kumo (4a3557b, 2026-09-24): MITM capture of Comfort app 3.5.0,
    send-command fields, account preferences (celsius), socket events, rate limit notes
  - EnumC/ha_kumo_ws pykumo2 (aa97516, 2026-10-04): cloud codec (power/operationMode/vent),
    capability derivation, headers, token lifetimes
  - nikolairahimi/mitsubishi-comfort (ade2405, 2026-09-17): CloudIndoorUnit, send-command,
    profile list-or-dict, V2 credential fallback
  - dlarrick/pykumo (8daed8f, 2026-10-04): v3 login/refresh, Socket.IO polling session,
    local LAN API token (W_PARAM/S_PARAM), fan speed quirks
  - dlarrick/hass-kumo (c9327ef, 2026-10-04): Mitsubishi F<->C setpoint table (PR #199)
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

API_BASE = os.environ.get("MITSUBISHI_API_BASE", "https://app-prod.kumocloud.com")
SOCKET_BASE = os.environ.get("MITSUBISHI_SOCKET_BASE", "https://socket-prod.kumocloud.com")
# 3.2.4 is the login appVersion used by pykumo, mitsubishi-comfort and ha_kumo_ws; the app is at 3.5.0.
APP_VERSION = os.environ.get("MITSUBISHI_APP_VERSION", "3.2.4")
# "commands" = {"deviceSerial", "commands"} used by every third-party client; "deviceCommands" is the
# form the 3.5.0 app sends ({"deviceSerial", "deviceCommands": {serial: {...}}}).
COMMAND_FORM = os.environ.get("MITSUBISHI_COMMAND_FORM", "commands")
BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "x-app-version": APP_VERSION,
    "app-env": "prd",
    "User-Agent": "kumocloud/1237 CFNetwork/3860.200.71 Darwin/25.1.0",
    "Cache-Control": "no-cache, no-store",
    "x-allow-cache": "false",
}
TIMEOUT = (10, 30)

ACCESS_FALLBACK_S = 18 * 60
REFRESH_FALLBACK_S = 25 * 86400
REFRESH_MARGIN = 120  # refresh if the access token expires within 2 min
LOGIN_MIN_INTERVAL_S = 600  # at most one automatic re-login per 10 min
DEFAULT_EMAIL = None  # set MITSUBISHI_COMFORT_EMAIL instead of hardcoding an account
META_TTL = 6 * 3600
PREFS_TTL = 24 * 3600

CONFIG_DIR = Path(os.environ.get("MITSUBISHI_CONFIG_DIR", Path.home() / ".config" / "mitsubishi"))
TOKEN_FILE = CONFIG_DIR / "tokens.json"
CACHE_FILE = CONFIG_DIR / "state.json"
LAN_FILE = CONFIG_DIR / "lan.json"

ALL_MODES = ("off", "heat", "cool", "auto", "dry", "fan")
WIRE_MODE = {"off": "off", "heat": "heat", "cool": "cool", "auto": "auto", "dry": "dry", "fan": "vent"}
FIVE_SPEEDS = ("superQuiet", "quiet", "low", "powerful", "superPowerful")
FOUR_SPEEDS = ("quiet", "low", "powerful", "superPowerful")
ALL_FANS = FIVE_SPEEDS + ("auto",)
VANES = ("horizontal", "midhorizontal", "midpoint", "midvertical", "vertical", "auto")
ALL_VANES = VANES + ("swing",)
DEFAULT_RANGE_C = (16.0, 31.0)

# Mitsubishi's own whole-°F <-> 0.5 °C setpoint mapping as shown by the Comfort app and MHK2
# (hass-kumo temperature.py, PR #199 merged 2026-04-05; same table as jjustinwilson/comfort_HA#23).
F_TO_C = {61: 16.0, 62: 16.5, 63: 17.0, 64: 17.5, 65: 18.0, 66: 18.5, 67: 19.5, 68: 20.0, 69: 21.0,
          70: 21.5, 71: 22.0, 72: 22.5, 73: 23.0, 74: 23.5, 75: 24.0, 76: 24.5, 77: 25.0, 78: 25.5,
          79: 26.0, 80: 26.5}
C_TO_F = {16.0: 61, 16.5: 62, 17.0: 63, 17.5: 64, 18.0: 65, 18.5: 66, 19.0: 67, 19.5: 67, 20.0: 68,
          20.5: 69, 21.0: 69, 21.5: 70, 22.0: 71, 22.5: 72, 23.0: 73, 23.5: 74, 24.0: 75, 24.5: 76,
          25.0: 77, 25.5: 78, 26.0: 79, 26.5: 80}

# Local LAN API (pykumo py_kumo_base._token / mitsubishi-comfort auth.compute_token).
W_PARAM = bytes.fromhex("44c73283b498d432ff25f5c8e06a016aef931e68f0a00ea710e36e6338fb22db")
S_PARAM = 0

_JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
_SECRET_KEYS = ("access", "refresh", "access_token", "refresh_token", "password", "cryptoSerial",
                "crypto_serial", "token", "Authorization", "mak", "baseMAK")
_SECRET_RE = re.compile(r'("(?:%s)"\s*:\s*)"[^"]*"' % "|".join(_SECRET_KEYS))
_BEARER_RE = re.compile(r"(Bearer\s+)[\w.-]+")
_M_RE = re.compile(r"([?&]m=)[0-9a-f]+")


class MitsubishiError(Exception):
    pass


class AuthError(MitsubishiError):
    pass


class ValidationError(MitsubishiError):
    """The request is not valid for this unit (exit 2)."""


def redact(text: Any) -> str:
    s = text if isinstance(text, str) else json.dumps(text, default=str)
    s = _JWT_RE.sub("<jwt>", s)
    s = _SECRET_RE.sub(r'\1"<redacted>"', s)
    s = _BEARER_RE.sub(r"\1<redacted>", s)
    return _M_RE.sub(r"\1<redacted>", s)


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _close(a, b, tol=0.05):
    try:
        return abs(float(a) - float(b)) < tol
    except (TypeError, ValueError):
        return False


def strip_secrets(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: strip_secrets(v) for k, v in d.items() if k not in ("password", "cryptoSerial", "cryptoKeySet")}
    if isinstance(d, list):
        return [strip_secrets(v) for v in d]
    return d


class TokenStore:
    def __init__(self, path: Path = TOKEN_FILE) -> None:
        self.path = path

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as err:
            raise AuthError(f"Can't read {self.path}: {err}") from err

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        with contextlib.suppress(OSError):
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    def clear(self):
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    @contextlib.contextmanager
    def lock(self):
        """Serialize refreshes across processes: a refresh token works once (it rotates)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX
                pass
            yield
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Temperatures
# ---------------------------------------------------------------------------

def round_half(x: float) -> float:
    return math.floor(x * 2 + 0.5) / 2


def c_to_f_display(c):
    """°C setpoint/room temp -> whole °F the way the Comfort app shows it."""
    if c is None:
        return None
    if abs(c * 2 - round(c * 2)) < 1e-6 and round_half(c) in C_TO_F:
        return C_TO_F[round_half(c)]
    return round(c * 9 / 5 + 32)


def f_to_c_setpoint(f: float) -> float:
    """Whole °F -> the 0.5 °C value the Comfort app stores for it."""
    if abs(f - round(f)) < 1e-9 and int(round(f)) in F_TO_C:
        return F_TO_C[int(round(f))]
    return round_half((f - 32) * 5 / 9)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _fold(s: str) -> str:
    return re.sub(r"[\s_-]", "", str(s)).casefold()


def canon(value: str, vocab) -> str | None:
    folded = _fold(value)
    for v in vocab:
        if _fold(v) == folded:
            return v
    return None


FAN_ALIASES = {"superlow": "superQuiet", "medium": "low", "med": "low", "high": "powerful", "max": "superPowerful",
               "superhigh": "superPowerful"}
VANE_ALIASES = {"mid": "midpoint", "middle": "midpoint", "up": "horizontal", "down": "vertical"}


def norm_fan(value: str) -> str:
    v = canon(value, ALL_FANS) or FAN_ALIASES.get(_fold(value))
    if not v:
        raise argparse.ArgumentTypeError(f"unknown fan speed '{value}' (use {', '.join(ALL_FANS)})")
    return v


def norm_vane(value: str) -> str:
    v = canon(value, ALL_VANES) or VANE_ALIASES.get(_fold(value))
    if not v:
        raise argparse.ArgumentTypeError(f"unknown vane position '{value}' (use {', '.join(ALL_VANES)})")
    return v


def parse_wire_mode(raw) -> tuple[str | None, str | None]:
    """operationMode -> (cli mode, active auto side). autoHeat/autoCool are auto."""
    if not isinstance(raw, str):
        return None, None
    f = raw.casefold()
    if f == "autoheat":
        return "auto", "heat"
    if f == "autocool":
        return "auto", "cool"
    if f in ("vent", "fan"):
        return "fan", None
    if f in ("off", "heat", "cool", "auto", "dry"):
        return f, None
    return f"unknown({raw})", None


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

class Device:
    """One indoor unit: merged cloud state (zone adapter + /v3/devices/{serial} + socket pushes),
    its profile (capabilities) and adapter status overlay."""

    def __init__(self, serial, name=None, *, site=None, state=None, profile=None, meta=None,
                 unit="C", last_mode=None, source="cloud"):
        self.serial = str(serial)
        self._name = name
        self.site = site or {}
        self.state: dict = dict(state or {})
        self.profile: dict = dict(profile or {})
        self.meta: dict = dict(meta or {})
        self.unit = unit
        self.last_mode = last_mode
        self.source = source
        self.link: dict | None = None  # socket device_status_v2 payload
        self.rec: dict = {}  # LAN credentials (lan transport only)

    @classmethod
    def from_cloud(cls, zone: dict, detail: dict | None = None, profile=None, meta=None, **kw):
        adapter = zone.get("adapter") if isinstance(zone.get("adapter"), dict) else {}
        state = {**adapter, **(detail or {})}
        if isinstance(profile, list):
            profile = profile[0] if profile else {}
        return cls(adapter.get("deviceSerial") or state.get("deviceSerial"), zone.get("name"),
                   state=state, profile=profile, meta=meta, **kw)

    def update(self, values: dict):
        self.state.update({k: v for k, v in values.items() if k != "deviceSerial"})

    # -- identity -------------------------------------------------------
    @property
    def name(self):
        return str(self._name or self.state.get("zoneName") or self.serial)

    @property
    def id(self):
        return self.serial

    @property
    def model(self):
        m = self.state.get("modelNumber")
        if not m and isinstance(self.state.get("model"), dict):
            m = self.state["model"].get("material")
        return str(m or "")

    # -- live values ----------------------------------------------------
    @property
    def online(self):
        if self.link and isinstance(self.link.get("status"), str):
            return self.link["status"] == "connected"
        c = self.state.get("connected")
        return None if c is None else bool(c)

    @property
    def online_source(self):
        if self.link and isinstance(self.link.get("status"), str):
            return "socket"
        if self.source == "lan" and "connected" in self.state:
            return "lan"
        return "cloud-record" if "connected" in self.state else None

    @property
    def power(self):
        p = self.state.get("power")
        mode, _ = parse_wire_mode(self.state.get("operationMode"))
        if p in (0, False) or mode == "off":
            return False
        if p in (1, True) or mode:
            return True
        return None

    @property
    def mode(self):
        if self.power is False:
            return "off"
        return parse_wire_mode(self.state.get("operationMode"))[0]

    @property
    def auto_active(self):
        return parse_wire_mode(self.state.get("operationMode"))[1]

    @property
    def sp_heat(self):
        return _num(self.state.get("spHeat"))

    @property
    def sp_cool(self):
        return _num(self.state.get("spCool"))

    @property
    def room_temp_c(self):
        return _num(self.state.get("roomTemp"))

    @property
    def humidity(self):
        return _num(self.state.get("humidity"))

    @property
    def fan(self):
        v = self.state.get("fanSpeed")
        return (canon(v, ALL_FANS) or v) if isinstance(v, str) else None

    @property
    def vane(self):
        v = self.state.get("airDirection")
        return (canon(v, ALL_VANES) or v) if isinstance(v, str) else None

    @property
    def error_code(self):
        return self.state.get("twoFiguresCode")

    # -- capabilities (ha_kumo_ws Capabilities.from_profile) -------------
    @property
    def has_profile(self):
        return bool(self.profile)

    def _allows(self, key):
        v = self.meta.get(key)
        return v is None or bool(v)

    @property
    def modes(self):
        p = self.profile
        if not p:
            return ["off", "heat", "cool"]  # conservative default without a profile
        found = {"off", "cool"}
        if p.get("hasModeHeat") and self._allows("modeHeat"):
            found.add("heat")
        if p.get("hasModeDry") and self._allows("modeDry"):
            found.add("dry")
        if p.get("hasModeVent", p.get("hasModeFan")):
            found.add("fan")
        # Honor autoModeDisable, but trust auto setpoints in the profile (pykumo 211b4fc quirk).
        if not self.meta.get("autoModeDisable") or "auto" in (p.get("maximumSetPoints") or {}) \
                or "auto" in (p.get("minimumSetPoints") or {}):
            found.add("auto")
        return [m for m in ALL_MODES if m in found]

    @property
    def number_of_fan_speeds(self):
        n = self.profile.get("numberOfFanSpeeds")
        return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else None

    @property
    def fan_speeds(self):
        if not self.profile:
            return list(ALL_FANS)
        n = self.number_of_fan_speeds
        auto = ["auto"] if self.profile.get("hasFanSpeedAuto") else []
        if n == 0:
            return auto
        # 3-speed units under-report and accept all five (pykumo 7a2caf5).
        return list(FOUR_SPEEDS if n == 4 else FIVE_SPEEDS) + auto

    @property
    def vane_positions(self):
        if not self.profile.get("hasVaneDir"):
            return []
        return list(VANES) + (["swing"] if self.profile.get("hasVaneSwing") else [])

    @property
    def uses_setpoint_in_dry(self):
        return bool(self.profile.get("usesSetPointInDryMode"))

    def _profile_range(self, key):
        lo = _num((self.profile.get("minimumSetPoints") or {}).get(key))
        hi = _num((self.profile.get("maximumSetPoints") or {}).get(key))
        lo = DEFAULT_RANGE_C[0] if lo is None else lo
        hi = DEFAULT_RANGE_C[1] if hi is None else hi
        return (lo, hi) if lo <= hi else DEFAULT_RANGE_C

    @staticmethod
    def _narrow(rng, lo=None, hi=None):
        nlo = max(rng[0], lo) if lo is not None else rng[0]
        nhi = min(rng[1], hi) if hi is not None else rng[1]
        return (nlo, nhi) if nlo <= nhi else rng

    def setpoint_range_c(self, side: str, mode: str | None = None):
        """Range for spHeat (side='heat') or spCool (side='cool') when the unit is in mode.
        The adapter's minSetPoint/maxSetPoint are the user's min-cool / max-heat limits."""
        user_min_cool = _num(self.meta.get("minSetPoint", self.meta.get("minSetpoint")))
        user_max_heat = _num(self.meta.get("maxSetPoint", self.meta.get("maxSetpoint")))
        heat = self._narrow(self._profile_range("heat"), None, user_max_heat)
        cool = self._narrow(self._profile_range("cool"), user_min_cool, None)
        if mode == "auto":
            auto = self._profile_range("auto")
            return self._narrow(auto, None, heat[1]) if side == "heat" else self._narrow(auto, cool[0], None)
        return heat if side == "heat" else cool

    def side_for(self, mode):
        """Which setpoint a single target temperature means in mode."""
        if mode == "heat":
            return "heat"
        if mode == "cool" or (mode == "dry" and self.uses_setpoint_in_dry):
            return "cool"
        return None

    @property
    def effective_mode(self):
        """Mode used to pick setpoints: current mode, or the remembered one while off."""
        m = self.mode
        if m == "off" or m is None:
            return self.last_mode if self.last_mode in ALL_MODES and self.last_mode != "off" else None
        return m

    # -- display ----------------------------------------------------------
    def to_unit(self, c):
        if c is None:
            return None
        return c_to_f_display(c) if self.unit == "F" else c

    def from_unit(self, v, celsius=False):
        if celsius:
            return round(float(v), 1)
        return f_to_c_setpoint(v) if self.unit == "F" else round_half(float(v))

    @property
    def setpoint_c(self):
        side = self.side_for(self.effective_mode)
        return self.sp_heat if side == "heat" else self.sp_cool if side == "cool" else None

    def _fmt(self, c):
        v = self.to_unit(c)
        if v is None:
            return "?"
        return f"{v:g}°{self.unit}"

    def summary(self):
        if self.online is False:
            head = "offline"
            if self.mode:
                head += f" (last known: {self.mode})"
        elif self.mode is None:
            head = "state unknown"
        elif self.mode == "off":
            head = f"off (heat {self._fmt(self.sp_heat)} / cool {self._fmt(self.sp_cool)})"
        elif self.mode == "auto":
            head = f"auto {self._fmt(self.sp_heat)}-{self._fmt(self.sp_cool)}"
            if self.auto_active:
                head += f" ({self.auto_active}ing)"
        elif self.setpoint_c is not None:
            head = f"{self.mode} {self._fmt(self.setpoint_c)}"
        else:
            head = self.mode
        if self.mode not in (None, "off") and self.online is not False:
            if self.fan:
                head += f", fan {self.fan}"
            if self.vane and self.vane_positions:
                head += f", vane {self.vane}"
        room = []
        if self.room_temp_c is not None:
            room.append(f"room {self._fmt(self.room_temp_c)}")
        if self.humidity not in (None, 0):
            room.append(f"{round(self.humidity, 1):g}% RH")
        tail = f" ({', '.join(room)})" if room else ""
        model = f" [{self.model}]" if self.model else ""
        return f"{self.name}{model}: {head}{tail}"

    def _range_dict(self, side, mode=None):
        lo, hi = self.setpoint_range_c(side, mode)
        return {"min": self.to_unit(lo), "max": self.to_unit(hi), "min_c": lo, "max_c": hi}

    def to_dict(self):
        ranges = {}
        if "heat" in self.modes:
            ranges["heat"] = self._range_dict("heat")
        ranges["cool"] = self._range_dict("cool")
        if "dry" in self.modes and self.uses_setpoint_in_dry:
            ranges["dry"] = self._range_dict("cool", "dry")
        if "auto" in self.modes:
            ranges["auto_heat"] = self._range_dict("heat", "auto")
            ranges["auto_cool"] = self._range_dict("cool", "auto")
        return {
            "name": self.name, "serial": self.serial, "site": self.site.get("name"), "model": self.model,
            "source": self.source, "online": self.online, "online_source": self.online_source,
            "power": None if self.power is None else ("on" if self.power else "off"),
            "mode": self.mode, "auto_active": self.auto_active,
            "setpoint": self.to_unit(self.setpoint_c), "setpoint_c": self.setpoint_c,
            "heat_setpoint": self.to_unit(self.sp_heat), "heat_setpoint_c": self.sp_heat,
            "cool_setpoint": self.to_unit(self.sp_cool), "cool_setpoint_c": self.sp_cool,
            "current_temp": self.to_unit(self.room_temp_c), "current_temp_c": self.room_temp_c,
            "humidity": self.humidity, "fan": self.fan, "vane": self.vane if self.vane_positions else None,
            "error_code": self.error_code, "unit": self.unit,
            "capabilities": {
                "profile_known": self.has_profile, "modes": self.modes, "fan_speeds": self.fan_speeds,
                "vane_positions": self.vane_positions, "swing_supported": "swing" in self.vane_positions,
                "setpoint_ranges": ranges, "unit": self.unit,
                "setpoint_step": 1 if self.unit == "F" else 0.5, "setpoint_step_c": 0.5,
                "uses_setpoint_in_dry": self.uses_setpoint_in_dry, "auto_uses_two_setpoints": True,
            },
        }


# ---------------------------------------------------------------------------
# Command planning (pure, unit-tested)
# ---------------------------------------------------------------------------

def plan_command(dev: Device, *, power=None, mode=None, temp=None, side=None, heat=None, cool=None,
                 fan=None, vane=None, celsius=False):
    """Return (commands, expect) for /v3/devices/send-command. Raises ValidationError.

    Abstract shapes: off = {"power": 0}; a mode = {"power": 1, "operationMode": <mode>} (fan is
    "vent"); setpoints are spHeat/spCool in °C; fanSpeed; airDirection. command_body() maps power
    onto the wire form. Turning on from off re-sends the mode's current setpoints, as the app does."""
    if mode is not None and mode not in dev.modes:
        raise ValidationError(f"{dev.name} doesn't support mode '{mode}'. Options: {', '.join(dev.modes)}")
    if fan is not None:
        if not dev.fan_speeds:
            raise ValidationError(f"{dev.name} has no fan speed control")
        if fan not in dev.fan_speeds:
            raise ValidationError(f"{dev.name} doesn't support fan '{fan}'. Options: {', '.join(dev.fan_speeds)}")
    if vane is not None:
        if not dev.vane_positions:
            raise ValidationError(f"{dev.name} has no vane control")
        if vane not in dev.vane_positions:
            raise ValidationError(f"{dev.name} doesn't support vane '{vane}'. "
                                  f"Options: {', '.join(dev.vane_positions)}")

    target = None  # None = leave the mode alone
    bare_on = False
    if power == "off" or mode == "off":
        target = "off"
    elif mode is not None:
        target = mode
    elif power == "on" and dev.power is not True:
        if dev.last_mode in dev.modes and dev.last_mode != "off":
            target = dev.last_mode
        else:
            bare_on = True

    sp_mode = target if target not in (None, "off") else dev.effective_mode
    if temp is not None:
        if side is None:
            side = dev.side_for(sp_mode)
            if side is None:
                if sp_mode == "auto":
                    raise ValidationError("Auto mode uses two setpoints: pass --side heat|cool, or use "
                                          "set --heat-temp N --cool-temp N")
                if sp_mode == "fan":
                    raise ValidationError("Fan mode has no setpoint")
                if sp_mode == "dry":
                    raise ValidationError(f"{dev.name} doesn't use a setpoint in dry mode")
                raise ValidationError("Can't tell which setpoint to change; pass --side heat|cool")
        if side == "heat":
            heat = temp
        else:
            cool = temp
    if heat is not None and "heat" not in dev.modes:
        raise ValidationError(f"{dev.name} has no heat mode")

    commands: dict[str, Any] = {}
    expect: dict[str, Any] = {}
    if target == "off":
        if dev.power is not False:
            commands["power"] = 0
            expect["power"] = False
    elif target is not None:
        if dev.power is not True or dev.mode != target:
            commands["power"] = 1
            commands["operationMode"] = WIRE_MODE[target]
            expect["power"] = True
            expect["mode"] = target
    elif bare_on:
        commands["power"] = 1
        expect["power"] = True

    new = {}
    for key, value, rng_side in (("spHeat", heat, "heat"), ("spCool", cool, "cool")):
        if value is None:
            continue
        if not math.isfinite(float(value)):
            raise ValidationError("Temperature must be a finite number")
        c = dev.from_unit(value, celsius)
        lo, hi = dev.setpoint_range_c(rng_side, sp_mode)
        if not (lo - 1e-6 <= c <= hi + 1e-6):
            u = "C" if celsius else dev.unit
            label = f"{'auto ' if sp_mode == 'auto' else ''}{rng_side}"
            raise ValidationError(f"{value:g}°{u} is outside {dev.name}'s {label} range "
                                  f"{dev.to_unit(lo):g}-{dev.to_unit(hi):g}°{dev.unit}")
        new[key] = c
    if sp_mode == "auto":
        h = new.get("spHeat", dev.sp_heat)
        c = new.get("spCool", dev.sp_cool)
        if h is not None and c is not None and h > c + 1e-6:
            raise ValidationError(f"Auto heat setpoint {dev._fmt(h)} is above the cool setpoint {dev._fmt(c)}")
    for key, c in new.items():
        current = dev.sp_heat if key == "spHeat" else dev.sp_cool
        if current is None or not _close(current, c):
            commands[key] = c
            expect[key] = c
    if target not in (None, "off") and dev.power is False:
        keys = {"auto": ("spHeat", "spCool"), "heat": ("spHeat",), "cool": ("spCool",),
                "dry": ("spCool",) if dev.uses_setpoint_in_dry else ()}.get(target, ())
        for key in keys:
            current = dev.sp_heat if key == "spHeat" else dev.sp_cool
            if key not in commands and current is not None:
                commands[key] = current
                expect[key] = current
    if fan is not None and fan != dev.fan:
        commands["fanSpeed"] = fan
        expect["fanSpeed"] = fan
    if vane is not None and vane != dev.vane:
        commands["airDirection"] = vane
        expect["airDirection"] = vane
    return commands, expect


def unmet(dev: Device, expect: dict) -> list[str]:
    out = []
    for key, want in expect.items():
        if key == "power":
            ok = dev.power is want
        elif key == "mode":
            ok = dev.mode == want
        elif key in ("spHeat", "spCool"):
            ok = _close(dev.state.get(key), want)
        elif key == "fanSpeed":
            ok = dev.fan == want
        elif key == "airDirection":
            ok = dev.vane == want
        else:
            ok = dev.state.get(key) == want
        if not ok:
            out.append(key)
    return out


def wire_commands(commands: dict, form: str | None = None) -> dict:
    """Map the abstract power key onto each send-command form (verified live 2026-10-11).

    Form A ("commands", the default) ignores `power`: the unit only turns off with
    operationMode "off" and turns on when a mode is sent. Form B ("deviceCommands") sends
    both power and operationMode (KUMO-API-V4-DOC.md)."""
    cmds = dict(commands)
    if (form or COMMAND_FORM) == "deviceCommands":
        if cmds.get("power") == 0:
            cmds["operationMode"] = "off"
        return cmds
    power = cmds.pop("power", None)
    if power == 0:
        cmds["operationMode"] = "off"
    elif power == 1 and "operationMode" not in cmds:
        raise ValidationError("Don't know which mode to turn on in; use: mitsubishi.py mode <mode>")
    return cmds


def command_body(serial: str, commands: dict, form: str | None = None) -> dict:
    cmds = wire_commands(commands, form)
    if (form or COMMAND_FORM) == "deviceCommands":
        return {"deviceSerial": serial, "deviceCommands": {serial: cmds}}
    return {"deviceSerial": serial, "commands": cmds}


def pick_device(devices, selector):
    if not devices:
        raise MitsubishiError("No indoor units found on this account")
    if not selector:
        if len(devices) == 1:
            return devices[0]
        raise ValidationError("Several units; pick one with -d. Names: " + ", ".join(d.name for d in devices))
    s = selector.casefold()
    for d in devices:
        if d.name.casefold() == s or d.serial.casefold() == s:
            return d
    partial = [d for d in devices if s in d.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    raise ValidationError(f"No unique unit matches '{selector}'. Names: " + ", ".join(d.name for d in devices))


# ---------------------------------------------------------------------------
# Socket.IO over engine.io 4 long-polling (pykumo KumoCloudV3._socketio_poll)
# ---------------------------------------------------------------------------

def parse_eio_payload(text: str) -> list[tuple[str, Any]]:
    """Split an engine.io 4 polling payload into packets.

    Returns (kind, data): ('ping', None), ('open', dict), ('connect', dict|None), ('connect_error', Any),
    ('disconnect', None), ('event', [name, *args]) or ('other', raw)."""
    out: list[tuple[str, Any]] = []
    if not text:
        return out
    for msg in text.split("\x1e"):
        if ":" in msg and msg.split(":", 1)[0].isdigit():  # legacy length prefix
            msg = msg.split(":", 1)[1]
        try:
            if msg == "2":
                out.append(("ping", None))
            elif msg.startswith("0{"):
                out.append(("open", json.loads(msg[1:])))
            elif msg.startswith("40"):
                out.append(("connect", json.loads(msg[2:]) if msg[2:].startswith("{") else None))
            elif msg.startswith("41"):
                out.append(("disconnect", None))
            elif msg.startswith("42"):
                data = json.loads(msg[2:])
                out.append(("event", data) if isinstance(data, list) and data else ("other", msg))
            elif msg.startswith("44"):
                out.append(("connect_error", json.loads(msg[2:]) if msg[2:] else None))
            elif msg:
                out.append(("other", msg))
        except ValueError:
            out.append(("connect_error", msg[2:]) if msg.startswith("44") else ("other", msg))
    return out


def sio_event(name: str, *args) -> str:
    return "42" + json.dumps([name, *args], separators=(",", ":"))


class SocketSession:
    MAX_RECONNECTS = 20  # the server drops polling sessions roughly every 10 s

    def __init__(self, client: "Comfort"):
        self.c = client
        self.http = requests.Session()
        self.sid = None
        self.subs: list[tuple] = []  # replayed after a reconnect
        self.reconnects = 0

    @staticmethod
    def _lost(r) -> bool:
        # Seen live 2026-10-11: after ~10 s the polling session can vanish server-side
        # (HTTP 400 {"code":1,"message":"Session ID unknown"}); no sticky cookie is offered.
        return r.status_code == 400 and "Session ID unknown" in (r.text or "")

    def _reconnect(self) -> bool:
        if self.reconnects >= self.MAX_RECONNECTS:
            return False
        self.reconnects += 1
        self.c._log("socket session lost; reconnecting", self.reconnects)
        self.open()
        if self.subs:
            self._post("\x1e".join(sio_event(*e) for e in self.subs))
        return True

    def _headers(self):
        return {"Authorization": f"Bearer {self.c.access_token()}", "Accept": "*/*",
                "User-Agent": BASE_HEADERS["User-Agent"]}

    def _params(self):
        p = {"EIO": "4", "transport": "polling"}
        if self.sid:
            p["sid"] = self.sid
        return p

    def _get(self, timeout=30.0):
        r = self.http.get(f"{SOCKET_BASE}/socket.io/", params=self._params(), headers=self._headers(),
                          timeout=(10, timeout))
        if self.sid and self._lost(r) and self._reconnect():
            return ""  # nothing received; the caller polls again on the new session
        if r.status_code != 200:
            raise MitsubishiError(f"socket poll HTTP {r.status_code}")
        return r.text

    def _post(self, data: str, _retry=True):
        r = self.http.post(f"{SOCKET_BASE}/socket.io/", params=self._params(), data=data.encode(),
                           headers={**self._headers(), "Content-Type": "text/plain;charset=UTF-8"},
                           timeout=(10, 15))
        if (_retry and self.sid and data not in ("3", "41") and self._lost(r) and self._reconnect()):
            return self._post(data, _retry=False)
        if r.status_code != 200:
            raise MitsubishiError(f"socket post HTTP {r.status_code}")

    def open(self, _retried=False):
        self.sid = None
        text = self._get(15)
        packets = parse_eio_payload(text)
        if not packets or packets[0][0] != "open" or not packets[0][1].get("sid"):
            raise MitsubishiError(f"socket handshake unexpected: {redact(text[:120])}")
        self.sid = packets[0][1]["sid"]
        self._post("40")
        for kind, _data in self.poll(10):
            if kind == "connect_error":
                if _retried:
                    raise AuthError("Socket rejected the session. Run: mitsubishi.py login")
                self.c.refresh()
                return self.open(_retried=True)
            if kind == "connect":
                break
        self.c._log("socket connected")
        return self

    def emit(self, *events: tuple):
        self.subs.extend(e for e in events if e and e[0] == "subscribe" and e not in self.subs)
        if events:
            payload = "\x1e".join(sio_event(*e) for e in events)
            self.c._log("socket emit", payload)
            self._post(payload)

    def poll(self, timeout=25.0):
        """One long-poll; answers pings. Returns the non-ping packets."""
        try:
            text = self._get(timeout)
        except requests.exceptions.ReadTimeout:
            return []
        out = []
        for kind, data in parse_eio_payload(text):
            if kind == "ping":
                with contextlib.suppress(Exception):
                    self._post("3")
                continue
            if kind == "event":
                self.c._log("socket recv", json.dumps(strip_secrets(data))[:400])
            else:
                self.c._log("socket packet", kind, redact(str(data))[:200])
            out.append((kind, data))
        return out

    def close(self):
        with contextlib.suppress(Exception):
            if self.sid:
                self._post("41")
        self.http.close()


def apply_socket_events(devices_by_serial: dict, packets) -> set[str]:
    """Merge device_update / device_status_v2 pushes into devices; returns serials touched."""
    touched = set()
    for kind, data in packets:
        if kind != "event" or len(data) < 2 or not isinstance(data[1], dict):
            continue
        name, payload = data[0], data[1]
        dev = devices_by_serial.get(str(payload.get("deviceSerial", "")))
        if dev is None:
            continue
        if name == "device_update":
            dev.update(strip_secrets(payload))
            touched.add(dev.serial)
        elif name == "device_status_v2":
            dev.link = {k: payload.get(k) for k in ("status", "lastTimeConnected", "lastTimeDisconnected",
                                                     "lastDisconnectedReason", "hasIduCommunicationError")}
            touched.add(dev.serial)
    return touched


# ---------------------------------------------------------------------------
# Wireless sensors (e.g. PAC-USWHS003-TH-1, paired to an indoor unit's adapter over Bluetooth)
# ---------------------------------------------------------------------------

def parse_iso(ts):
    """ISO-8601 (with Z) -> aware datetime, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return None


def _whole(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def signal_label(rssi):
    """Bluetooth RSSI (dBm) -> rough label. Approximate; the app's own thresholds aren't published."""
    if not isinstance(rssi, (int, float)):
        return None
    if rssi >= -60:
        return "very strong"
    if rssi >= -70:
        return "strong"
    if rssi >= -80:
        return "fair"
    return "weak"


def temp_source(dev) -> str | None:
    """The adapter's room-temp input: sensor0-3, returnair, remote, api; None when not reported.
    activeThermistor (what is in effect) wins over tempSource (what is configured)."""
    for key in ("activeThermistor", "tempSource"):
        v = dev.state.get(key)
        if isinstance(v, str) and v and v != "unset":
            return v
    return None


class Sensor:
    """One paired wireless sensor. Sources: cloud GET /v3/devices/{serial}/sensor (one object per
    adapter), the socket's sensor_update push, or the unit's LAN API {"c":{"sensors":{"N":{}}}}."""

    def __init__(self, raw: dict, dev: Device, *, source="cloud", slot=None, count=1):
        self.raw = strip_secrets(dict(raw or {}))
        self.dev = dev
        self.source = source
        self.slot = slot
        self.count = count  # sensors paired to this unit (for the drives-room-temp inference)

    uuid = property(lambda self: str(self.raw.get("uuid") or "") or None)
    record_id = property(lambda self: self.raw.get("id"))
    temp_c = property(lambda self: _num(self.raw.get("temperature")))
    humidity = property(lambda self: _num(self.raw.get("humidity")))
    battery = property(lambda self: _whole(_num(self.raw.get("battery"))))
    rssi = property(lambda self: _whole(_num(self.raw.get("rssi"))))
    tx_power = property(lambda self: _whole(_num(self.raw.get("txPower"))))

    @property
    def name(self):
        # The API has no sensor name; this mirrors the Comfort app's "Wireless sensor / <zone>".
        return self.raw.get("name") or f"Wireless sensor / {self.dev.name}"

    @property
    def model(self):
        return self.raw.get("model") or self.raw.get("modelNumber") or None  # not in the v3 payload

    @property
    def updated(self):
        return parse_iso(self.raw.get("updatedAt"))

    def temp(self):
        """Display-unit temperature to 0.1 (not the setpoint table: this is a measurement)."""
        c = self.temp_c
        if c is None:
            return None
        return round(c * 9 / 5 + 32, 1) if self.dev.unit == "F" else round(c, 1)

    def drives_room_temp(self):
        """(True/False/None, reason)."""
        src = temp_source(self.dev)
        if src is None:
            return None, "unknown: the cloud reports tempSource/activeThermistor as null (LAN-only fields)"
        if src.startswith("sensor"):
            if self.slot is not None:
                return src == f"sensor{self.slot}", f"unit temp source is {src}"
            if self.count == 1:
                return True, f"unit temp source is {src} (only paired sensor)"
            return None, f"unit temp source is {src}; sensor slot unknown"
        return False, f"unit temp source is {src}"

    def to_dict(self):
        drives, why = self.drives_room_temp()
        upd = self.updated
        room_c = self.dev.room_temp_c
        return {
            "name": self.name,
            "model": self.model,
            "uuid": self.uuid,
            "record_id": self.record_id,
            "slot": self.slot,
            "source": self.source,
            "temperature": self.temp(),
            "temperature_c": self.temp_c,
            "unit": self.dev.unit,
            "humidity": self.humidity,
            "battery": self.battery,
            "rssi": self.rssi,
            "signal": signal_label(self.rssi),
            "tx_power": self.tx_power,
            "updated_at": upd.astimezone().isoformat(timespec="seconds") if upd else None,
            "age_seconds": int(time.time() - upd.timestamp()) if upd else None,
            "linked_unit": {"name": self.dev.name, "serial": self.dev.serial,
                            "site": self.dev.site.get("name")},
            "unit_room_temp": self.dev.to_unit(room_c) if room_c is not None else None,
            "unit_room_temp_c": room_c,
            "unit_temp_source": temp_source(self.dev),
            "drives_room_temp": drives,
            "drives_room_temp_reason": why,
        }

    def summary(self):
        parts = []
        t = self.temp()
        if t is not None:
            parts.append(f"{t:g}°{self.dev.unit} ({self.temp_c:g} °C)")
        if self.humidity is not None:
            parts.append(f"{round(self.humidity, 1):g}% RH")
        if self.battery is not None:
            parts.append(f"battery {self.battery:g}%")
        if self.rssi is not None:
            parts.append(f"signal {self.rssi:g} dBm ({signal_label(self.rssi)})")
        upd = self.updated
        if upd:
            age = int(time.time() - upd.timestamp())
            ago = f"{age // 60} min ago" if age >= 60 else f"{max(age, 0)} s ago"
            parts.append(f"updated {upd.astimezone().strftime('%-I:%M %p %Z')} ({ago})")
        drives, _ = self.drives_room_temp()
        src = {True: "yes", False: "no", None: "unknown"}[drives]
        ident = self.uuid or self.record_id or "?"
        return (f"{self.name} [{ident}] -> {self.dev.name} ({self.dev.serial}): " + ", ".join(parts)
                + f"; drives unit room temp: {src}")


def sensors_from_payload(data, dev, source="cloud"):
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    items = [x for x in items if isinstance(x, dict) and (x.get("uuid") or x.get("id")) and not x.get("error")]
    return [Sensor(x, dev, source=source, count=len(items)) for x in items]


def merge_sensor_events(sensors: list, devs_by_serial: dict, packets) -> int:
    """Fold socket sensor_update pushes in by uuid (adding sensors REST didn't list). Returns count."""
    n = 0
    for kind, data in packets:
        if kind != "event" or len(data) < 2 or data[0] != "sensor_update" or not isinstance(data[1], dict):
            continue
        payload = data[1]
        dev = devs_by_serial.get(str(payload.get("deviceSerial", "")))
        key = payload.get("uuid") or payload.get("id")
        if dev is None or not key:
            continue
        n += 1
        match = next((x for x in sensors if (x.uuid or x.record_id) == key), None)
        if match is None:
            sensors.append(Sensor(payload, dev, source="socket"))
        else:
            old, new = match.updated, parse_iso(payload.get("updatedAt"))
            if old is None or new is None or new >= old:
                match.raw.update(strip_secrets(payload))
    for x in sensors:
        x.count = sum(1 for y in sensors if y.dev is x.dev)
    return n


# ---------------------------------------------------------------------------
# Cloud client
# ---------------------------------------------------------------------------

class Comfort:
    def __init__(self, store=None, cache=None, *, verbose=False, unit=None):
        self.store = store or TokenStore()
        self.cache = cache or TokenStore(CACHE_FILE)
        self.verbose = verbose
        self.unit_override = unit
        self.http = requests.Session()
        self.http.headers.update(BASE_HEADERS)
        self._tokens = None

    def _log(self, *args):
        if self.verbose:
            print("[debug]", *(redact(a) if not isinstance(a, (int, float)) else a for a in args), file=sys.stderr)

    # -- auth ---------------------------------------------------------------
    @staticmethod
    def token_record(access, refresh, now=None, **extra):
        now = time.time() if now is None else now
        ac, rc = jwt_claims(access), (jwt_claims(refresh) if refresh else {})
        rec = {
            "access": access, "refresh": refresh,
            "access_expires_at": float(ac.get("exp") or now + ACCESS_FALLBACK_S),
            "refresh_expires_at": float(rc.get("exp") or now + REFRESH_FALLBACK_S),
            "user_id": None if ac.get("id") is None else str(ac.get("id")),
        }
        rec.update(extra)
        return rec

    def login(self, email, password):
        body = {"username": email, "password": password, "appVersion": APP_VERSION}
        r = self.http.post(f"{API_BASE}/v3/login", json=body, timeout=TIMEOUT)
        self._log("POST /v3/login", r.status_code)
        if r.status_code in (400, 401, 403, 404):
            raise AuthError("Incorrect email or password")
        if r.status_code == 429:
            raise MitsubishiError("Rate limited by the Mitsubishi cloud; wait a few minutes")
        if r.status_code >= 500:
            raise MitsubishiError(f"Login failed: HTTP {r.status_code} (server error; 'Kumo Cloud Canada' "
                                  "accounts use a different backend)")
        if r.status_code != 200:
            raise MitsubishiError(f"Login failed: HTTP {r.status_code}")
        try:
            data = r.json() or {}
        except ValueError as err:
            raise MitsubishiError("Login returned invalid JSON") from err
        tok = data.get("token") or {}
        if not tok.get("access"):
            raise AuthError("Login response had no access token")
        self._tokens = self.token_record(tok["access"], tok.get("refresh") or "", email=email)
        if not self._tokens["user_id"] and data.get("id") is not None:
            self._tokens["user_id"] = str(data["id"])
        self.store.save(self._tokens)
        prefs = data.get("preferences") or {}
        if isinstance(prefs.get("celsius"), bool):
            self._save_pref(prefs["celsius"])

    def _require_tokens(self):
        if self._tokens is None:
            self._tokens = self.store.load()
        if not self._tokens or not self._tokens.get("access"):
            raise AuthError("Not logged in. Run: mitsubishi.py login")
        return self._tokens

    def _fresh(self, t):
        return bool(t and t.get("access")) and time.time() < float(t.get("access_expires_at", 0)) - REFRESH_MARGIN

    def _relogin_locked(self, t, why):
        """Non-interactive re-login (caller holds the token lock). At most one per LOGIN_MIN_INTERVAL_S."""
        email = (t or {}).get("email") or os.environ.get("MITSUBISHI_COMFORT_EMAIL") or DEFAULT_EMAIL
        password = os.environ.get("MITSUBISHI_COMFORT_PASSWORD")
        if not email:
            raise AuthError(f"{why}; no MITSUBISHI_COMFORT_EMAIL for auto re-login. Run: mitsubishi.py login")
        if not password:
            raise AuthError(f"{why}; no MITSUBISHI_COMFORT_PASSWORD for auto re-login. Run: mitsubishi.py login")
        stamp = self.store.path.parent / "last_auto_login"
        try:
            last = float(stamp.read_text().strip())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < LOGIN_MIN_INTERVAL_S:
            raise AuthError(f"{why}; auto re-login already tried {int(time.time() - last)}s ago (max 1 per "
                            f"{LOGIN_MIN_INTERVAL_S // 60} min)")
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp_tmp = stamp.with_name(f"{stamp.name}.{os.getpid()}.tmp")
        stamp_tmp.write_text(str(time.time()))
        os.replace(stamp_tmp, stamp)
        self._log("auto re-login:", why)
        self.login(email, password)

    def _refresh_locked(self, t):
        """POST /v3/refresh (caller holds the lock; t was just re-read from disk). Tokens rotate."""
        if not t or not t.get("refresh") or time.time() > float(t.get("refresh_expires_at", 0)):
            return self._relogin_locked(t, "refresh token missing or expired")
        r = self.http.post(f"{API_BASE}/v3/refresh", json={"refresh": t["refresh"]}, timeout=TIMEOUT,
                           headers={"Authorization": f"Bearer {t['refresh']}"})
        self._log("POST /v3/refresh", r.status_code)
        if r.status_code in (400, 401, 403):
            return self._relogin_locked(t, f"refresh token rejected (HTTP {r.status_code})")
        if r.status_code != 200:
            raise MitsubishiError(f"Token refresh failed: HTTP {r.status_code}")
        try:
            data = r.json() or {}
        except ValueError as err:
            raise MitsubishiError("Token refresh returned invalid JSON") from err
        src = data.get("token") if isinstance(data.get("token"), dict) else data
        if not src.get("access"):
            raise AuthError("Refresh response had no access token. Run: mitsubishi.py login")
        keep = {k: t[k] for k in ("email",) if k in t}
        new = self.token_record(src["access"], src.get("refresh") or t["refresh"], **keep)
        new["user_id"] = new["user_id"] or t.get("user_id")
        self.store.save(new)
        self._tokens = new

    def refresh(self, used_refresh=None, force=True):
        """Get usable tokens under the cross-process lock. The file is re-read after locking.
        force=True (after a 401): if the refresh token on disk differs from the one we used, another
        process already rotated it, so just adopt it and retry; otherwise refresh once."""
        if used_refresh is None and self._tokens:
            used_refresh = self._tokens.get("refresh")
        with self.store.lock():
            disk = self.store.load()
            if not disk or not disk.get("access"):
                if os.environ.get("MITSUBISHI_COMFORT_PASSWORD") and (self._tokens or disk is not None):
                    return self._relogin_locked(self._tokens, "token file empty")
                raise AuthError("Not logged in. Run: mitsubishi.py login")
            if force:
                if disk.get("refresh") != used_refresh:
                    self._tokens = disk
                    return
            elif self._fresh(disk):
                self._tokens = disk
                return
            self._refresh_locked(disk)

    def access_token(self):
        t = self._require_tokens()
        if not self._fresh(t):
            self.refresh(force=False)
        return self._tokens["access"]

    # -- REST ----------------------------------------------------------------
    def _request(self, method, path, body=None, ok404=False):
        for attempt in (1, 2):
            token = self.access_token()
            used_refresh = self._tokens.get("refresh")
            r = self.http.request(method, API_BASE + path, json=body, timeout=TIMEOUT,
                                  headers={"Authorization": f"Bearer {token}"})
            self._log(method, path, r.status_code, body if body is not None else "")
            if r.status_code == 401 and attempt == 1:
                self.refresh(used_refresh=used_refresh, force=True)
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"{path} rejected the session. Run: mitsubishi.py login")
            if r.status_code == 429:
                raise MitsubishiError("Rate limited by the Mitsubishi cloud (retry after "
                                      f"{r.headers.get('Retry-After', '60')}s)")
            if r.status_code == 404 and ok404:
                return None
            if r.status_code == 400 and method == "POST":
                raise MitsubishiError(f"Cloud rejected the command: {redact(r.text[:300])}")
            if r.status_code >= 400:
                raise MitsubishiError(f"{method} {path} failed: HTTP {r.status_code} {redact(r.text[:200])}")
            if r.status_code == 204 or not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        raise MitsubishiError(f"{method} {path} failed after token refresh")

    def sites(self):
        data = self._request("GET", "/v3/sites/")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def zones(self, site_id):
        data = self._request("GET", f"/v3/sites/{site_id}/zones")
        return [z for z in data if isinstance(z, dict)] if isinstance(data, list) else []

    def device_detail(self, serial):
        data = self._request("GET", f"/v3/devices/{serial}", ok404=True)
        return strip_secrets(data) if isinstance(data, dict) else {}

    def device_profile(self, serial):
        data = self._request("GET", f"/v3/devices/{serial}/profile", ok404=True)
        if isinstance(data, list):  # a one-item list for most accounts, a bare object for some
            data = data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def device_meta(self, serial):
        data = self._request("GET", f"/v3/devices/{serial}/status", ok404=True)
        return strip_secrets(data) if isinstance(data, dict) else {}

    # -- prefs / cache -------------------------------------------------------
    def _save_pref(self, celsius: bool):
        cache = self.cache.load() or {}
        cache["prefs"] = {"celsius": celsius, "at": time.time()}
        self.cache.save(cache)

    def display_unit(self, cache):
        """--unit, else the Comfort app's account preference (preferences.celsius), else °C."""
        if self.unit_override:
            return self.unit_override
        prefs = cache.get("prefs") or {}
        if not isinstance(prefs.get("celsius"), bool) or time.time() - float(prefs.get("at", 0)) > PREFS_TTL:
            with contextlib.suppress(MitsubishiError, requests.RequestException):
                me = self._request("GET", "/v3/accounts/me") or {}
                c = (me.get("preferences") or {}).get("celsius")
                if isinstance(c, bool):
                    prefs = cache["prefs"] = {"celsius": c, "at": time.time()}
                    self.cache.save(cache)
        c = prefs.get("celsius")
        return "F" if c is False else "C"

    def devices(self, fresh_meta=False):
        cache = self.cache.load() or {}
        unit = self.display_unit(cache)
        units = cache.setdefault("units", {})
        devs = []
        for site in self.sites():
            if not site.get("id"):
                continue
            for zone in self.zones(site["id"]):
                adapter = zone.get("adapter") if isinstance(zone.get("adapter"), dict) else {}
                serial = adapter.get("deviceSerial")
                if not serial or adapter.get("isHeadless"):
                    continue  # Kumo Station / headless units have no climate controls
                entry = units.setdefault(serial, {})
                detail = self.device_detail(serial)
                if fresh_meta or not entry.get("profile") or time.time() - float(entry.get("meta_at", 0)) > META_TTL:
                    with contextlib.suppress(MitsubishiError):
                        entry["profile"] = self.device_profile(serial)
                        entry["meta"] = self.device_meta(serial)
                        entry["meta_at"] = time.time()
                devs.append(Device.from_cloud(zone, detail, entry.get("profile"), entry.get("meta"),
                                              site={"id": site["id"], "name": site.get("name")}, unit=unit,
                                              last_mode=entry.get("last_mode")))
        self.remember(devs, cache)
        return devs

    def remember(self, devs, cache=None):
        """Keep the last non-off mode per unit (for `on`) in state.json. No secrets go here."""
        cache = cache if cache is not None else (self.cache.load() or {})
        units = cache.setdefault("units", {})
        for d in devs:
            if d.mode in ALL_MODES and d.mode != "off":
                units.setdefault(d.serial, {})["last_mode"] = d.mode
                d.last_mode = d.mode
        self.cache.save(cache)

    def refetch(self, dev):
        detail = self.device_detail(dev.serial)
        if detail:
            dev.update(detail)
        return dev

    # -- live -----------------------------------------------------------------
    def live(self, devs, wait=8.0):
        """Ask each adapter for fresh state and its real connection status over Socket.IO
        (the REST record keeps serving an offline adapter's last state)."""
        by = {d.serial: d for d in devs}
        sock = SocketSession(self).open()
        try:
            uid = self._require_tokens().get("user_id")
            sock.emit(*(([("subscribe", "", uid)] if uid else []) + [("subscribe", s) for s in by]))
            sock.emit(*[e for s in by for e in (("force_adapter_request", s, "iuStatus"), ("device_status_v2", s))])
            need = {("link", s) for s in by} | {("state", s) for s in by}
            deadline = time.monotonic() + max(wait, 1)
            while need and time.monotonic() < deadline:
                packets = sock.poll(max(1.0, min(25, deadline - time.monotonic())))
                for kind, data in packets:
                    if kind == "event" and len(data) > 1 and isinstance(data[1], dict):
                        s = str(data[1].get("deviceSerial"))
                        need.discard(("link" if data[0] == "device_status_v2" else "state", s))
                apply_socket_events(by, packets)
        finally:
            sock.close()
        return devs

    # -- sensors (read-only) -------------------------------------------------
    def sensors(self, devs):
        """GET /v3/devices/{serial}/sensor per unit (404 sensorNotFound = none paired).
        Source: jjustinwilson/comfort_HA kumo_cloud/api.py get_wireless_sensor; verified live."""
        out = []
        for d in devs:
            if d.state.get("hasSensor") is False:
                continue
            data = self._request("GET", f"/v3/devices/{d.serial}/sensor", ok404=True)
            out.extend(sensors_from_payload(data, d))
        return out

    def live_sensors(self, devs, sensors, wait=20.0):
        """Listen for sensor_update pushes (subscribe + status read requests only; nothing is written)."""
        by = {d.serial: d for d in devs}
        sock = SocketSession(self).open()
        seen = 0
        try:
            uid = self._require_tokens().get("user_id")
            sock.emit(*(([("subscribe", "", uid)] if uid else []) + [("subscribe", s) for s in by]))
            deadline = time.monotonic() + max(wait, 1)
            last_force = 0.0
            while time.monotonic() < deadline:
                if time.monotonic() - last_force > 15:
                    sock.emit(*[("force_adapter_request", s, "iuStatus") for s in by])
                    last_force = time.monotonic()
                packets = sock.poll(max(1.0, min(8.0, deadline - time.monotonic())))
                apply_socket_events(by, packets)
                seen += merge_sensor_events(sensors, by, packets)
        finally:
            sock.close()
        return seen

    # -- writes ----------------------------------------------------------------
    def send(self, dev, commands):
        res = self._request("POST", "/v3/devices/send-command", command_body(dev.serial, commands))
        if isinstance(res, dict):
            if res.get("success") is False or res.get("error") or res.get("errors"):
                raise MitsubishiError(f"Cloud rejected the command: {redact(res)}")
            if isinstance(res.get("devices"), list) and dev.serial not in [str(x) for x in res["devices"]]:
                raise MitsubishiError("Cloud did not accept the command for this unit")
        return res

    def set(self, dev, commands, expect, *, wait=20.0, confirm="auto"):
        self.confirmed_via = None
        if not commands:
            return True
        sock = None
        if wait > 0 and confirm in ("auto", "socket"):
            try:
                sock = SocketSession(self).open()
                uid = self._require_tokens().get("user_id")
                sock.emit(*([("subscribe", "", uid)] if uid else []), ("subscribe", dev.serial))
            except (MitsubishiError, requests.RequestException) as err:
                if confirm == "socket":
                    raise
                self._log("socket unavailable, confirming via REST:", str(err))
                sock = None
        try:
            self._log("send-command", command_body(dev.serial, commands))
            self.send(dev, commands)
            if wait <= 0:
                return False
            if sock is not None:
                self._socket_dropped = False
                ok = self._confirm_socket(sock, dev, expect, wait)
                self.confirmed_via = ("REST re-read (socket dropped)" if self._socket_dropped else "socket") if ok else None
                return ok
            ok = self._confirm_rest(dev, expect, wait)
            self.confirmed_via = "REST re-read" if ok else None
            return ok
        finally:
            if sock is not None:
                sock.close()

    def _confirm_socket(self, sock, dev, expect, wait):
        """Confirmed when a device_update pushed by the adapter matches every changed field."""
        by = {dev.serial: dev}
        deadline = time.monotonic() + wait
        last_force = 0.0
        while time.monotonic() < deadline:
            try:
                if time.monotonic() - last_force > 6:
                    sock.emit(("force_adapter_request", dev.serial, "iuStatus"),
                              ("device_status_v2", dev.serial))
                    last_force = time.monotonic()
                packets = sock.poll(max(1.0, min(8.0, deadline - time.monotonic())))
            except (MitsubishiError, requests.RequestException) as err:
                # The server can drop a polling session mid-wait (seen live as HTTP 400); the command
                # was already accepted, so finish with a REST re-read instead of failing.
                self._log("socket dropped, confirming via REST:", str(err))
                self._socket_dropped = True
                return self._confirm_rest(dev, expect, max(4.0, deadline - time.monotonic()))
            if apply_socket_events(by, packets):
                if dev.online is False:
                    self._log("adapter reports disconnected")
                    return False
                if not unmet(dev, expect):
                    return True
        self._log("unconfirmed:", ",".join(unmet(dev, expect)))
        return False

    def _confirm_rest(self, dev, expect, wait):
        # Weaker than the socket: the REST record may lag (or lead) the unit, and it keeps serving the
        # last state of an offline adapter.
        deadline = time.monotonic() + wait
        while True:
            time.sleep(2.0)
            self.refetch(dev)
            if not unmet(dev, expect):
                return True
            if time.monotonic() > deadline:
                self._log("unconfirmed:", ",".join(unmet(dev, expect)))
                return False


# ---------------------------------------------------------------------------
# Local LAN API (pykumo). Only for units whose password/cryptoSerial you already have.
# ---------------------------------------------------------------------------

def lan_token(password: bytes, crypto_serial: bytes, post_data: bytes) -> str:
    if len(crypto_serial) < 9:
        raise MitsubishiError("cryptoSerial is too short to sign local requests")
    data_hash = hashlib.sha256(password + post_data).digest()
    inter = bytearray(88)
    inter[0:32] = W_PARAM[0:32]
    inter[32:64] = data_hash[0:32]
    inter[64:66] = bytes.fromhex("0840")
    inter[66] = S_PARAM
    inter[79] = crypto_serial[8]
    inter[80:84] = crypto_serial[4:8]
    inter[84:88] = crypto_serial[0:4]
    return hashlib.sha256(inter).hexdigest()


def lan_body(path: tuple, leaf) -> bytes:
    for key in reversed(path):
        leaf = {key: leaf}
    return json.dumps({"c": leaf}, separators=(",", ":")).encode()


def parse_lan_import(data) -> dict:
    """Extract {serial: {label, address, password, cryptoSerial, mac}} from a pykumo kumo_cache.json
    (v2 login tree with zoneTable maps), a mitsubishi-comfort style dict, or a list of records."""
    found: dict[str, dict] = {}

    def take(key, rec):
        pw = rec.get("password")
        cs = rec.get("cryptoSerial") or rec.get("crypto_serial")
        serial = rec.get("serial") or key
        if isinstance(pw, str) and pw and isinstance(cs, str) and cs and serial:
            found[str(serial)] = {"label": rec.get("label") or rec.get("name") or "",
                                  "address": rec.get("address") or "", "mac": rec.get("mac") or "",
                                  "password": pw, "cryptoSerial": cs}

    def walk(node, key=None):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            take(key, node)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, k)

    walk(data)
    return found


def normalize_lan(status: dict, profile: dict, adapter: dict | None) -> tuple[dict, dict, dict]:
    """Local indoorUnit/status + profile + adapter/status -> cloud-shaped (state, profile, meta)."""
    state = {k: status[k] for k in ("spHeat", "spCool", "roomTemp", "fanSpeed", "tempSource",
                                    "activeThermistor") if k in status}
    mode = status.get("mode")
    if isinstance(mode, str):
        state["operationMode"] = mode
        state["power"] = 0 if mode == "off" else 1
    if "vaneDir" in status:
        state["airDirection"] = status["vaneDir"]
    if isinstance(state.get("fanSpeed"), str):
        state["fanSpeed"] = canon(state["fanSpeed"], ALL_FANS) or state["fanSpeed"]
    state["connected"] = bool(status)
    meta = {}
    if adapter:
        meta = {
            "autoModeDisable": bool(adapter.get("autoModePrevention", False)),
            # pykumo parity: a missing userHasMode* flag masks the mode.
            "modeDry": bool(adapter.get("userHasModeDry", False)),
            "modeHeat": bool(adapter.get("userHasModeHeat", False)),
            "minSetPoint": adapter.get("userMinCoolSetPoint"),
            "maxSetPoint": adapter.get("userMaxHeatSetPoint"),
        }
    return state, dict(profile or {}), {k: v for k, v in meta.items() if v is not None}


def lan_writes(dev: Device, commands: dict) -> list[bytes]:
    """send-command fields -> one local request body per key (pykumo style)."""
    path = ("indoorUnit", "status")
    out = []
    if commands.get("power") == 0:
        out.append(lan_body(path, {"mode": "off"}))
    elif "operationMode" in commands:
        out.append(lan_body(path, {"mode": commands["operationMode"]}))
    elif commands.get("power") == 1:
        raise ValidationError("Over LAN, 'on' needs a mode and none is remembered for this unit; use mode <m>")
    for key in ("spHeat", "spCool"):
        if key in commands:
            out.append(lan_body(path, {key: round(float(commands[key]), 1)}))
    if "fanSpeed" in commands:
        speed = commands["fanSpeed"]
        if speed == "low" and dev.number_of_fan_speeds == 4:
            speed = "Low"  # 4-speed units use a capitalised label locally (pykumo get_fan_speeds)
        out.append(lan_body(path, {"fanSpeed": speed}))
    if "airDirection" in commands:
        out.append(lan_body(path, {"vaneDir": commands["airDirection"]}))
    return out


class Lan:
    """Direct-to-unit transport. Never contacts the cloud."""

    def __init__(self, store=None, cache=None, *, verbose=False, unit=None):
        self.store = store or TokenStore(LAN_FILE)
        self.cache = cache or TokenStore(CACHE_FILE)
        self.verbose = verbose
        self.unit_override = unit
        self.http = requests.Session()

    _log = Comfort._log
    remember = Comfort.remember

    def _creds(self):
        units = (self.store.load() or {}).get("units") or {}
        if not units:
            raise MitsubishiError(f"No LAN credentials. Import a pykumo kumo_cache.json with: "
                                  f"mitsubishi.py lan-import <file> (stored in {LAN_FILE})")
        return units

    def request(self, rec: dict, body: bytes):
        if not rec.get("address"):
            raise MitsubishiError(f"No LAN address for {rec.get('label') or 'unit'}; re-run lan-import --address")
        token = lan_token(base64.b64decode(rec["password"]), bytes.fromhex(rec["cryptoSerial"]), body)
        url = f"http://{rec['address']}/api"
        r = self.http.put(url, params={"m": token}, data=body, timeout=(2, 8), headers={
            "Accept": "application/json, text/plain, */*", "Content-Type": "application/json"})
        self._log("PUT", url, r.status_code, body.decode())
        if r.status_code != 200:
            raise MitsubishiError(f"Unit at {rec['address']} answered HTTP {r.status_code}")
        try:
            return json.loads(r.content.decode("utf-8").rstrip("\x00 \r\n"))
        except ValueError as err:
            raise MitsubishiError("Unit returned invalid JSON") from err

    def _read(self, rec, path):
        node = self.request(rec, lan_body(path, {}))
        node = node.get("r") if isinstance(node, dict) else None
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else {}

    def devices(self, fresh_meta=False):
        cache = self.cache.load() or {}
        prefs = cache.get("prefs") or {}
        unit = self.unit_override or ("F" if prefs.get("celsius") is False else "C")
        devs = []
        for serial, rec in self._creds().items():
            d = Device(serial, rec.get("label") or serial, unit=unit, source="lan",
                       last_mode=((cache.get("units") or {}).get(serial) or {}).get("last_mode"))
            d.rec = rec
            try:
                self._load(d, profile=True)
            except (MitsubishiError, requests.RequestException) as err:
                self._log("lan read failed:", rec.get("address") or "?", str(err))
                d.state = {"connected": False}
            devs.append(d)
        self.remember(devs, cache)
        return devs

    def _load(self, d, profile=False):
        status = self._read(d.rec, ("indoorUnit", "status"))
        prof, adapter = d.profile, None
        if profile:
            prof = self._read(d.rec, ("indoorUnit", "profile"))
            with contextlib.suppress(MitsubishiError, requests.RequestException):
                adapter = self._read(d.rec, ("adapter", "status"))
        state, prof, meta = normalize_lan(status, prof, adapter)
        d.state, d.profile = state, prof
        if adapter is not None:
            d.meta = meta

    def refetch(self, dev):
        self._load(dev)
        return dev

    def sensors(self, devs):
        """Local read of sensor slots 0-3 (pykumo/mitsubishi-comfort _fetch_sensors); stops at an empty slot."""
        out = []
        for d in devs:
            if not d.rec or d.state.get("connected") is False:
                continue
            found = []
            for slot in range(4):
                try:
                    raw = self._read(d.rec, ("sensors", str(slot)))
                except (MitsubishiError, requests.RequestException) as err:
                    self._log("lan sensor read failed:", str(err))
                    break
                if not raw.get("uuid"):
                    break
                found.append(Sensor(raw, d, source="lan", slot=slot))
            for x in found:
                x.count = len(found)
            out.extend(found)
        return out

    def set(self, dev, commands, expect, *, wait=20.0, confirm="auto"):
        if not commands:
            return True
        for body in lan_writes(dev, commands):
            res = self.request(dev.rec, body)
            if not isinstance(res, dict) or "r" not in res:
                raise MitsubishiError(f"Unit rejected {body.decode()}: {redact(res)}")
        if wait <= 0:
            return False
        deadline = time.monotonic() + wait
        while True:
            time.sleep(1.0)
            with contextlib.suppress(MitsubishiError, requests.RequestException):
                self.refetch(dev)
                if not unmet(dev, expect):
                    self.confirmed_via = "LAN re-read"
                    return True
            if time.monotonic() > deadline:
                return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _lan_import(args):
    try:
        data = json.loads(Path(args.file).expanduser().read_text())
    except (OSError, ValueError) as err:
        raise ValidationError(f"Can't read {args.file}: {err}") from err
    units = parse_lan_import(data)
    if not units:
        raise ValidationError("No unit with both password and cryptoSerial found in that file")
    for spec in args.address or []:
        key, _, ip = spec.partition("=")
        match = [s for s, r in units.items() if s == key or r["label"].casefold() == key.casefold()]
        if not ip or len(match) != 1:
            raise ValidationError(f"--address {spec}: expected SERIAL_OR_NAME=IP matching one imported unit")
        units[match[0]]["address"] = ip
    TokenStore(LAN_FILE).save({"units": units, "imported_at": time.time()})
    print(f"Saved LAN credentials for {len(units)} unit(s) to {LAN_FILE} (0600):")
    for s, r in units.items():
        print(f"  {r['label'] or s} ({s}) address={r['address'] or 'MISSING'}")
    return 0


def _changes(args) -> dict:
    changes: dict[str, Any] = {}
    if args.cmd in ("on", "off"):
        changes["power"] = args.cmd
    elif args.cmd == "mode":
        changes["mode"] = args.value
    elif args.cmd == "temp":
        changes["temp"] = args.value
        if args.side:
            changes["side"] = args.side
    elif args.cmd == "fan":
        changes["fan"] = args.value
    elif args.cmd == "vane":
        changes["vane"] = args.value
    elif args.cmd == "swing":
        changes["vane"] = "swing" if args.state == "on" else "auto"
    elif args.cmd == "set":
        for k in ("power", "mode", "temp", "side", "fan", "vane"):
            if getattr(args, k) is not None:
                changes[k] = getattr(args, k)
        if args.heat_temp is not None:
            changes["heat"] = args.heat_temp
        if args.cool_temp is not None:
            changes["cool"] = args.cool_temp
        if "side" in changes and "temp" not in changes:
            raise ValidationError("--side only applies with --temp")
        if "temp" in changes and ("heat" in changes or "cool" in changes):
            raise ValidationError("Use either --temp or --heat-temp/--cool-temp, not both")
        if not changes:
            raise ValidationError("Nothing to change")
    return changes


def _sensors(m, args):
    """Read-only: lists paired wireless sensors. Makes no write calls."""
    devices = m.devices()
    if args.device:
        devices = [pick_device(devices, args.device)]
    sensors = m.sensors(devices)
    if args.live and isinstance(m, Comfort) and devices:
        try:
            m.live_sensors(devices, sensors, wait=args.wait if args.wait > 0 else 20)
        except (MitsubishiError, requests.RequestException) as err:
            print(f"Live listen unavailable: {redact(str(err))}", file=sys.stderr)
    if args.json:
        print(json.dumps([x.to_dict() for x in sensors], indent=2))
    elif not sensors:
        print("No wireless sensors reported for " + ", ".join(d.name for d in devices) + ".")
    else:
        for x in sensors:
            print(x.summary())
    return 0


def _run(args):
    if args.cmd == "logout":
        TokenStore().clear()
        print("Logged out.")
        return 0
    if args.cmd == "lan-import":
        return _lan_import(args)
    if args.cmd == "login":
        if args.transport == "lan":
            raise ValidationError("login is for the cloud; LAN mode uses lan-import")
        m = Comfort(verbose=args.verbose, unit=args.unit)
        email = os.environ.get("MITSUBISHI_COMFORT_EMAIL") or input("Mitsubishi Comfort email: ").strip()
        password = os.environ.get("MITSUBISHI_COMFORT_PASSWORD") or getpass.getpass("Password: ")
        m.login(email, password)
        del password
        devices = m.devices(fresh_meta=True)
        print(f"Logged in. {len(devices)} indoor unit(s):")
        for d in devices:
            print("  " + d.summary())
        return 0

    changes = _changes(args) if args.cmd not in ("status", "sensors") else {}
    m = Lan(verbose=args.verbose, unit=args.unit) if args.transport == "lan" else \
        Comfort(verbose=args.verbose, unit=args.unit)
    if args.cmd == "status":
        devices = m.devices()
        if args.device:
            devices = [pick_device(devices, args.device)]
        if args.live and isinstance(m, Comfort) and devices:
            try:
                m.live(devices, wait=min(args.wait, 10) if args.wait > 0 else 8)
            except (MitsubishiError, requests.RequestException) as err:
                print(f"Live check unavailable: {redact(str(err))}", file=sys.stderr)
            m.remember(devices)
        if args.json:
            print(json.dumps([d.to_dict() for d in devices], indent=2))
        else:
            for d in devices:
                print(d.summary())
        return 0
    if args.cmd == "sensors":
        return _sensors(m, args)

    dev = pick_device(m.devices(), args.device)
    if args.cmd == "swing" and "swing" not in dev.vane_positions:
        raise ValidationError(f"{dev.name} doesn't support swing")
    if dev.online is False:
        raise MitsubishiError(f"{dev.name} is offline; not sending")
    commands, expect = plan_command(dev, celsius=getattr(args, "celsius", False), **changes)
    if not commands:
        print(dev.summary())
        print("Already set; nothing sent.", file=sys.stderr)
        return 0
    m._log("plan:", commands)
    confirmed = m.set(dev, commands, expect, wait=args.wait, confirm=args.confirm)
    m.remember([dev])
    print(dev.summary())
    if not confirmed:
        print("Sent, but the unit didn't confirm in time. Re-check with: mitsubishi.py status --live",
              file=sys.stderr)
        return 3
    print(f"Confirmed via {getattr(m, 'confirmed_via', None) or 'state re-read'}.", file=sys.stderr)
    return 0


COMMON_OPTS = [
    (("-v", "--verbose"), dict(action="store_true", help="debug output (tokens redacted)")),
    (("-d", "--device"), dict(help="unit name, unique name fragment, or serial")),
    (("--wait",), dict(type=float, default=20.0, help="seconds to wait for confirmation (default 20; 0 = don't wait)")),
    (("--unit",), dict(type=str.upper, choices=("F", "C"), help="display/input unit (default: Comfort app preference)")),
    (("--transport",), dict(choices=("cloud", "lan"), default="cloud",
                            help="cloud (default) or lan (direct to units; needs lan-import)")),
    (("--confirm",), dict(choices=("auto", "socket", "rest"), default="auto",
                          help="confirm writes from the unit's socket push (auto prefers it) or a REST re-read")),
]


def build_parser():
    p = argparse.ArgumentParser(prog="mitsubishi", description="Control Mitsubishi mini-splits through the "
                                "Mitsubishi Comfort app's cloud (Kumo Cloud v3).")
    for flags, kw in COMMON_OPTS:
        p.add_argument(*flags, **kw)
    # Let the common options also follow the subcommand (e.g. `status -d Bedroom`).
    common = argparse.ArgumentParser(add_help=False)
    for flags, kw in COMMON_OPTS:
        common.add_argument(*flags, **{**kw, "default": argparse.SUPPRESS})
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)
    sub.add_parser("login", help="log in with MITSUBISHI_COMFORT_EMAIL / MITSUBISHI_COMFORT_PASSWORD (or prompt)")
    sub.add_parser("logout", help="delete stored cloud tokens")
    s = sub.add_parser("status", help="show indoor units")
    s.add_argument("--json", action="store_true", help="full state and capabilities as JSON")
    s.add_argument("--live", action="store_true", help="ask units for fresh state and real online status (socket)")
    se = sub.add_parser("sensors", help="list paired wireless sensors with their own readings (read-only)")
    se.add_argument("--json", action="store_true", help="full sensor readings as JSON")
    se.add_argument("--live", action="store_true",
                    help="also listen --wait seconds (default 20) for socket sensor_update pushes")
    sub.add_parser("on", help="turn on (restores the last mode)")
    sub.add_parser("off", help="turn off")
    mo = sub.add_parser("mode", help="set mode (validated against the unit)")
    mo.add_argument("value", choices=ALL_MODES)
    t = sub.add_parser("temp", help="set the setpoint for the current mode, in the display unit")
    t.add_argument("value", type=float)
    t.add_argument("--side", choices=("heat", "cool"), help="which setpoint (required in auto mode)")
    t.add_argument("-C", "--celsius", action="store_true", help="value is raw °C (sent to 0.1 °C)")
    f = sub.add_parser("fan", help=f"set fan speed ({', '.join(ALL_FANS)})")
    f.add_argument("value", type=norm_fan, metavar="SPEED")
    v = sub.add_parser("vane", help=f"set the vertical vane ({', '.join(ALL_VANES)})")
    v.add_argument("value", type=norm_vane, metavar="POSITION")
    sw = sub.add_parser("swing", help="vane swing on, or off (vane back to auto)")
    sw.add_argument("state", nargs="?", choices=("on", "off"), default="on")
    st = sub.add_parser("set", help="change several settings in one command")
    st.add_argument("--power", choices=("on", "off"))
    st.add_argument("--mode", choices=ALL_MODES)
    st.add_argument("--temp", type=float, help="setpoint for the (new) mode")
    st.add_argument("--side", choices=("heat", "cool"), help="which setpoint --temp is for")
    st.add_argument("--heat-temp", type=float, help="heat setpoint (low end in auto)")
    st.add_argument("--cool-temp", type=float, help="cool setpoint (high end in auto)")
    st.add_argument("--fan", type=norm_fan, metavar="SPEED")
    st.add_argument("--vane", type=norm_vane, metavar="POSITION")
    st.add_argument("-C", "--celsius", action="store_true", help="temperatures are raw °C (sent to 0.1 °C)")
    li = sub.add_parser("lan-import", help="import local credentials from a pykumo kumo_cache.json (LAN mode)")
    li.add_argument("file")
    li.add_argument("--address", action="append", metavar="SERIAL_OR_NAME=IP", help="set a unit's LAN IP")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ValidationError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    except AuthError as err:
        print(f"Auth error: {err}", file=sys.stderr)
        return 1
    except MitsubishiError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except requests.RequestException as err:
        print(f"Network error: {redact(str(err))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
