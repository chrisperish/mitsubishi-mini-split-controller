"""I/O side of the controller: talk to the unit, keep state, write the log, run the loop.

Usage:
  python -m minisplit_controller --once [--dry-run] [--note TEXT]   run one cycle (exit 2 on error)
  python -m minisplit_controller --loop               run forever (every MSC_INTERVAL_S, default 600 s)

The unit is reached through the bundled Comfort cloud client (client/mitsubishi.py), run as a
subprocess with a hard timeout so a hung network call can never wedge the loop.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config as config_mod
from .logic import decide, default_state, deferred_due, info_text, needed_changes, next_due

DEFAULT_TZ = ZoneInfo("America/Los_Angeles")   # override with MSC_TIMEZONE / [controller] timezone
UTC = ZoneInfo("UTC")
OUTDOOR_EVERY_S = 30 * 60
CLI_TIMEOUT_RC = 124
TERM_GRACE_S = 5

# Log columns, in order. See README "Log format".
CSV_COLS = ["id", "timestamp_pt", "room_f", "humidity", "mode", "auto_active", "heat_sp", "cool_sp",
            "fan", "event", "action", "room_c", "sensor_f", "unit_f", "temp_source", "sensor_age_min"]


def tz_label(tz):
    """Short zone label written after log timestamps: "PT" for Pacific, else the IANA key (e.g. "UTC")."""
    return "PT" if str(tz) == "America/Los_Angeles" else str(tz)


def now_pt(tz=DEFAULT_TZ):
    return datetime.now(tz)


def fmt_pt(dt, tz=None):
    """'2026-10-12 7:20 AM PT' in the given zone (default: the datetime's own zone)."""
    tz = tz or dt.tzinfo or DEFAULT_TZ
    return dt.astimezone(tz).strftime("%Y-%m-%d %-I:%M %p ") + tz_label(tz)


def parse_pt(text, tz=DEFAULT_TZ):
    """Inverse of fmt_pt. The trailing zone label is ignored; `tz` says which zone the log uses."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} [AP]M)", text)
    if not m:
        raise ValueError(f"bad timestamp: {text!r}")
    return datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p").replace(tzinfo=tz)


def c_to_f(c):
    return None if c is None else round(c * 9 / 5 + 32, 1)


def retry_after(msg):
    """Seconds to back off after a rate-limit message from the cloud, else None."""
    m = re.search(r"retry after (\d+)", msg or "", re.I)
    if m:
        return int(m.group(1))
    return 60 if "rate limited" in (msg or "").lower() else None


# ---------------------------------------------------------------- cloud client
class Client:
    """Thin wrapper around the Comfort CLI. Every call runs in its own process group with a timeout."""

    def __init__(self, settings):
        self.s = settings

    def run(self, *args, device=True):
        """Returns (returncode, stdout, stderr). Return code 124 means it timed out and was killed."""
        cmd = [*self.s.client_cmd, *(["-d", self.s.device] if device else []), *args]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            out, err = p.communicate(timeout=self.s.cli_timeout_s)
            return p.returncode, out, (err or "").strip()
        except subprocess.TimeoutExpired:
            _kill_group(p)
            return CLI_TIMEOUT_RC, "", f"timed out after {self.s.cli_timeout_s}s ({args[0]}); killed"
        finally:
            if p.poll() is None:
                _kill_group(p)

    def status(self):
        rc, out, err = self.run("status", "--json")
        if rc != 0:
            raise RuntimeError(err or f"status exit {rc}")
        data = json.loads(out)
        return data[0] if isinstance(data, list) else data

    def sensor(self):
        """The wireless sensor linked to our unit (by serial), else one whose name matches MSC_SENSOR_NAME."""
        rc, out, err = self.run("sensors", "--json", device=False)
        if rc != 0:
            raise RuntimeError(err or f"sensors exit {rc}")
        data = json.loads(out)
        data = data if isinstance(data, list) else [data]
        dev = self.s.device.lower()
        name = self.s.sensor_name.lower()
        mine = [d for d in data if dev and dev in (
            str((d.get("linked_unit") or {}).get("serial", "")).lower(),
            str((d.get("linked_unit") or {}).get("name", "")).lower())]
        mine = mine or [d for d in data if name and name in (d.get("name") or "").lower()]
        if not mine:
            raise RuntimeError("no wireless sensor linked to the configured unit")
        return mine[0]

    def set(self, changes):
        args = ["set"]
        for k, v in changes.items():
            args += [f"--{k}", str(v)]
        return self.run(*args)


def _kill_group(p):
    """SIGTERM the child's process group, wait briefly, then SIGKILL (also reaps grandchildren)."""
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            p.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            pass


def fetch_outdoor(lat, lon, timeout=8):
    """Current outdoor temperature from Open-Meteo (free, no key). Returns (temp_f, unix_ts)."""
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m&temperature_unit=fahrenheit&timezone=UTC")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        cur = json.loads(r.read().decode())["current"]
    try:
        ts = datetime.fromisoformat(cur["time"]).replace(tzinfo=UTC).timestamp()
    except (KeyError, ValueError):
        ts = time.time()
    return round(float(cur["temperature_2m"]), 1), ts


def choose_temp(sensor, unit_f, max_age_min, sensor_err=None):
    """Prefer the wireless sensor if fresh. Returns (room_f, source, sensor_f, age_min, note)."""
    if sensor is None:
        return unit_f, "unit", None, None, f"sensor unavailable ({str(sensor_err)[:80]}); used unit temp"
    t = sensor.get("temp_f", sensor.get("temperature"))
    if t is None and sensor.get("temperature_c") is not None:
        t = c_to_f(sensor["temperature_c"])
    age = sensor.get("age_seconds")
    age_min = None if age is None else round(age / 60, 1)
    sensor_f = None if t is None else round(float(t), 1)
    if sensor_f is None:
        return unit_f, "unit", None, age_min, "sensor has no temperature; used unit temp"
    if age is None or age > max_age_min * 60:
        return unit_f, "unit", sensor_f, age_min, f"sensor stale ({age_min} min); used unit temp"
    return sensor_f, "sensor", sensor_f, age_min, None


# ---------------------------------------------------------------- controller
class Controller:
    def __init__(self, settings, client=None, outdoor=fetch_outdoor, clock=time.time, sleep=time.sleep):
        self.s = settings
        self.t = settings.tuning
        self.tz = ZoneInfo(settings.timezone)
        self.client = client or Client(settings)
        self.outdoor = outdoor
        self.clock = clock
        self.sleep = sleep
        d = settings.data_dir
        d.mkdir(parents=True, exist_ok=True)
        self.state_file, self.log_csv = d / "state.json", d / "log.csv"
        self.log_lock, self.cycle_lock = d / "log.lock", d / "cycle.lock"
        self.pid_file, self.heartbeat = d / "controller.pid", d / "heartbeat.json"

    # -- persistence
    def load_state(self):
        try:
            s = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            s = {}
        if "regime" not in s:
            now = self.clock()
            s = default_state(now)
            s["history"] = self.seed_history(now)
            s["last_ok_ts"] = s["history"][-1][0] if s["history"] else None
        return s

    def save_state(self, s):
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(s, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    def seed_history(self, now_ts):
        """Rebuild recent (ts, T) pairs from log.csv so a restart keeps slope and hold checks."""
        out = []
        try:
            with open(self.log_csv, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("event") == "Error" or not r.get("room_f"):
                        continue
                    try:
                        ts, temp = parse_pt(r["timestamp_pt"], self.tz).timestamp(), float(r["room_f"])
                    except (ValueError, KeyError):
                        continue
                    if 0 < now_ts - ts <= self.t.history_h * 3600:
                        out.append([ts, temp])
        except OSError:
            pass
        return sorted(out)

    def append_row(self, row):
        """Append one log row under a file lock, assigning the next id."""
        with open(self.log_lock, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            new = not self.log_csv.exists()
            next_id = 1
            if not new:
                with open(self.log_csv, newline="") as f:
                    ids = [int(r["id"]) for r in csv.DictReader(f) if r.get("id", "").isdigit()]
                next_id = max(ids, default=0) + 1
            row = {**{k: "" for k in CSV_COLS}, **row, "id": next_id}
            with open(self.log_csv, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        return row

    @staticmethod
    def row_from(dev, event, action, ts, temp=None):
        unit_f = c_to_f(dev.get("current_temp_c"))
        temp = temp or {}
        return {"timestamp_pt": fmt_pt(ts), "room_f": temp.get("room_f", unit_f), "unit_f": unit_f,
                "sensor_f": temp.get("sensor_f"), "temp_source": temp.get("source", "unit"),
                "sensor_age_min": temp.get("age_min"),
                "humidity": None if dev.get("humidity") is None else round(dev["humidity"], 1),
                "mode": dev.get("mode"), "auto_active": dev.get("auto_active") or "",
                "heat_sp": dev.get("heat_setpoint"), "cool_sp": dev.get("cool_setpoint"),
                "fan": dev.get("fan"), "event": event, "action": action, "room_c": dev.get("current_temp_c")}

    def update_outdoor(self, state):
        """Refresh the cached outdoor temp at most every 30 min. Never raises; returns a note on failure."""
        if self.s.latitude is None or self.s.longitude is None:
            return None
        now = self.clock()
        if now - (state.get("outdoor_fetched_at") or 0) < OUTDOOR_EVERY_S:
            return None
        state["outdoor_fetched_at"] = now
        try:
            f, ts = self.outdoor(self.s.latitude, self.s.longitude)
            state["outdoor_f"], state["outdoor_ts"] = f, min(ts, now)
            return None
        except Exception as err:  # noqa: BLE001  (network, JSON, anything: never stop a cycle)
            return f"outdoor fetch failed ({str(err)[:60]})"

    # -- one cycle
    def cycle(self, dry_run=False, gap_note=None):
        """One control cycle, serialized by a non-blocking flock (an overlapping cycle is skipped)."""
        with open(self.cycle_lock, "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"action": "another cycle is running; skipped"}
            return self._cycle(dry_run, gap_note)

    def _log(self, dry_run, row):
        if not dry_run:
            self.append_row(row)

    def _cycle(self, dry_run, gap_note):
        ts = datetime.fromtimestamp(self.clock(), self.tz)
        state = self.load_state()
        if not dry_run and self.clock() < state.get("backoff_until", 0):
            return {"action": "rate-limit backoff active; skipped"}
        try:
            dev = self.client.status()
        except Exception as err:  # noqa: BLE001
            msg = f"error: status failed: {err}"[:300]
            row_msg = f"{gap_note}; {msg}" if gap_note else msg
            ra = retry_after(str(err))
            if ra and not dry_run:
                state["backoff_until"] = self.clock() + ra
                self.save_state(state)
            self._log(dry_run, {"timestamp_pt": fmt_pt(ts), "event": "Error", "action": row_msg})
            return {"action": msg}

        # Never fight a person: offline or switched off means hands off.
        if not dev.get("online", False):
            msg = "unit offline; no change"
            self._log(dry_run, self.row_from(dev, "Error", msg, ts))
            return {"action": msg}
        if dev.get("power") == "off" or dev.get("mode") == "off":
            msg = "unit is off (manual override?); no change"
            self._log(dry_run, self.row_from(dev, "Reading", msg, ts))
            return {"action": msg}

        unit_f = c_to_f(dev.get("current_temp_c"))
        try:
            sensor, s_err = self.client.sensor(), None
        except Exception as err:  # noqa: BLE001
            sensor, s_err = None, err
        room_f, source, sensor_f, age_min, t_note = choose_temp(sensor, unit_f, self.t.sensor_max_age_min, s_err)
        temp = {"room_f": room_f, "source": source, "sensor_f": sensor_f, "age_min": age_min}
        if room_f is None:
            msg = "error: no room temperature from sensor or unit; no change"
            self._log(dry_run, self.row_from(dev, "Error", msg, ts, temp))
            return {"action": msg}

        o_note = self.update_outdoor(state)
        desired, new_state, info, events = decide(room_f, state, self.clock(), dev, self.t)
        notes = [n for n in (gap_note, t_note, o_note, info.get("blocked")) if n]
        if info["g"] is not None and info["g"] > self.t.gap_max_min:
            notes.append(f"gap {info['g']:.0f} min > {self.t.gap_max_min:.0f}: no slope, regime change or trim")
        head = info_text(info) + "; " + ("; ".join(notes) + "; " if notes else "")
        ch = needed_changes(dev, desired)
        if not ch:
            action, event = f"{head}none", "Reading"
        else:
            names = {"mode": ("mode", "mode"), "heat-temp": ("heat SP", "heat_setpoint"),
                     "cool-temp": ("cool SP", "cool_setpoint"), "fan": ("fan", "fan")}
            desc = ", ".join(f"changed {names[k][0]} {dev.get(names[k][1])} to {v}" for k, v in ch.items())
            if "mode" in ch and not any("regime" in e for e in events):
                desc = f"mode was {dev.get('mode')} (manual?), reverting to {desired['mode']}; " + desc
            if dry_run:
                action, event = f"{head}[dry-run] would set {ch} ({desc})", "Control change"
            else:
                rc, _, err = self.client.set(ch)
                if rc == 0:
                    action, event = f"{head}{desc}", "Control change"
                elif rc == 3:   # client exit 3: command sent but the unit did not confirm in time
                    action, event = f"{head}{desc} (sent, unconfirmed)", "Control change"
                else:
                    action, event = f"{head}error: {desc} failed (exit {rc}): {err[:200]}", "Error"
                    ra = retry_after(err)
                    if ra:
                        new_state["backoff_until"] = self.clock() + ra
                if rc in (0, 3):
                    try:
                        self.sleep(3)
                        dev = self.client.status()
                        left = needed_changes(dev, desired)
                        if rc == 3 or left:
                            action += " -> re-check: " + ("now matches" if not left else f"still differs {left}")
                    except Exception as err2:  # noqa: BLE001
                        action += f" (re-read failed: {str(err2)[:100]})"
        if dry_run:
            print(json.dumps({"temp": temp, "info": info, "desired": desired, "events": events,
                              "changes": ch, "action": action}, indent=2))
            return {"action": action}
        new_state["backoff_until"] = max(new_state.get("backoff_until", 0), state.get("backoff_until", 0))
        self.save_state(new_state)
        tag = f"Tp {info['Tp']:.2f} trim {info['trim']:+d} {info['outdoor']}"
        for e in events:   # each regime / trim / setpoint change also gets its own row
            self.append_row(self.row_from(dev, "Control change", f"{e}; {tag}", ts, temp))
        row = self.append_row(self.row_from(dev, event, action, ts, temp))
        print(f"{fmt_pt(ts)} room {room_f}F ({source}) {desired}: {action} (row {row['id']})", flush=True)
        return {"action": action, "row": row, "desired": desired, "events": events}

    # -- loop
    def write_heartbeat(self, **extra):
        hb = {"pid": os.getpid(), "updated": now_pt(self.tz).isoformat(timespec="seconds"), **extra}
        tmp = self.heartbeat.with_suffix(".tmp")
        tmp.write_text(json.dumps(hb, indent=2))
        os.replace(tmp, self.heartbeat)

    def last_ok_ts(self):
        """Time of the last cycle that produced a decision (from the state file), or None."""
        try:
            return json.loads(self.state_file.read_text()).get("last_ok_ts")
        except (OSError, ValueError, AttributeError):
            return None

    def skip_until(self, now):
        """Loop helper: if another cycle (kick.sh, --once) finished less than skip_recent_s ago (capped at
        interval_s / 2), the time to run the next loop cycle instead; else None."""
        interval = self.s.interval_s
        return deferred_due(self.last_ok_ts(), now, interval, min(self.s.skip_recent_s, interval / 2))

    def once(self, note=None):
        """One cycle with the loop's cycle lock and watchdog. Returns an exit code: 0 ok (or skipped
        because another cycle holds the lock), 2 error. Used by scripts/kick.sh."""
        def _watchdog(*_):
            raise TimeoutError(f"cycle exceeded {self.s.cycle_watchdog_s}s watchdog")

        def _stop(*_):
            raise SystemExit(2)

        old = signal.signal(signal.SIGALRM, _watchdog), signal.signal(signal.SIGTERM, _stop)
        signal.alarm(self.s.cycle_watchdog_s)
        try:
            r = self.cycle(gap_note=note)
        except Exception as err:  # noqa: BLE001
            print(f"{fmt_pt(now_pt(self.tz))} cycle crashed: {err}", flush=True)
            try:
                self.append_row({"timestamp_pt": fmt_pt(now_pt(self.tz)), "event": "Error",
                                 "action": (f"{note}; " if note else "") + f"error: {err}"[:300]})
            except Exception:  # noqa: BLE001
                pass
            return 2
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old[0])
            signal.signal(signal.SIGTERM, old[1])
        if "row" not in r:   # skipped or early error: _cycle printed nothing, so say why (kick.sh reads it)
            print(f"{fmt_pt(now_pt(self.tz))} {r.get('action')}", flush=True)
        if str(r.get("action", "")).startswith("error") or (r.get("row") or {}).get("event") == "Error":
            return 2
        return 0

    def loop(self, start_delay=0, chunk=15):
        """Run forever. Sleeps in short chunks against the wall clock so it survives suspend/resume,
        writes a heartbeat every chunk, and caps each cycle with a SIGALRM watchdog."""
        interval = self.s.interval_s
        if self.pid_file.exists():
            try:
                old = int(self.pid_file.read_text().strip())
                os.kill(old, 0)
                sys.exit(f"already running as pid {old}")
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self.pid_file.write_text(str(os.getpid()))

        def _stop(*_):
            raise SystemExit(0)

        def _watchdog(*_):
            raise TimeoutError(f"cycle exceeded {self.s.cycle_watchdog_s}s watchdog")

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGALRM, _watchdog)
        iso = lambda t: datetime.fromtimestamp(t, self.tz).isoformat(timespec="seconds")  # noqa: E731
        last = {}
        try:
            due, last_start = time.time() + start_delay, None
            while True:
                now = time.time()
                if now < due:
                    self.write_heartbeat(next_cycle=iso(due), **last)
                    time.sleep(max(0.5, min(chunk, due - now)))
                    continue
                d2 = self.skip_until(now)
                if d2 is not None:   # an outside cycle (scripts/kick.sh, --once) just ran: keep spacing
                    due = d2
                    continue
                gap_note = None
                if now - due > interval:
                    since = f"{(now - last_start) / 60:.0f} min since last cycle" if last_start else "first cycle"
                    gap_note = f"gap: cycle overdue {(now - due) / 60:.0f} min ({since}); ran immediately"
                last_start = now
                signal.alarm(self.s.cycle_watchdog_s)
                try:
                    r = self.cycle(gap_note=gap_note)
                    last = {"last_cycle": iso(now), "last_action": r.get("action")}
                except Exception as err:  # noqa: BLE001
                    last = {"last_cycle": iso(now), "last_action": f"crash: {err}"}
                    try:
                        self.append_row({"timestamp_pt": fmt_pt(now_pt(self.tz)), "event": "Error",
                                         "action": f"error: {err}"[:300]})
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    signal.alarm(0)
                due = next_due(due, now, time.time(), interval)
        finally:
            self.write_heartbeat(stopped=True, **last)
            if self.pid_file.exists() and self.pid_file.read_text().strip() == str(os.getpid()):
                self.pid_file.unlink()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="minisplit_controller", description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="run one cycle and exit")
    g.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--dry-run", action="store_true", help="with --once: decide and print, change nothing")
    ap.add_argument("--config", help="TOML config file (default: $MSC_CONFIG or ./config.toml)")
    ap.add_argument("--note", help="with --once: note added to the cycle's log row (used by scripts/kick.sh)")
    ap.add_argument("--start-delay", type=int, default=0, help="seconds to wait before the first loop cycle")
    a = ap.parse_args(argv)
    settings = config_mod.load(a.config)
    if not settings.device:
        sys.exit("set MSC_DEVICE (unit serial or name) or [controller] device in the config file")
    c = Controller(settings)
    if a.loop:
        if a.dry_run or a.note:
            sys.exit("--dry-run and --note only with --once")
        c.loop(a.start_delay)
    elif a.dry_run:
        c.cycle(dry_run=True)
    else:
        sys.exit(c.once(a.note))


if __name__ == "__main__":
    main()
