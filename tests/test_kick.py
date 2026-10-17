"""--once (exit codes, --note, lock, SIGTERM, printed reason), the loop's skip window, the CLI exit code,
and scripts/kick.sh with stubbed commands (flock, timeout, --loop check, heartbeat handling)."""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minisplit_controller import config
from minisplit_controller import controller as ctl
from test_controller import LIVE, FakeClient, make, rows

ROOT = Path(__file__).resolve().parents[1]
KICK = ROOT / "scripts" / "kick.sh"
NOW = 1_700_000_000.0


@pytest.fixture
def settings(tmp_path):
    return config.Settings(device="TEST-UNIT", data_dir=tmp_path)


# ---- --once

def test_once_ok_and_note(settings):
    c = make(settings, FakeClient(LIVE, sensor={"temp_f": 69.0, "age_seconds": 60}))
    assert c.once("kick: heartbeat 15 min old; ran one cycle") == 0
    assert "kick: heartbeat 15 min old" in rows(settings.log_csv)[-1]["action"]
    assert c.last_ok_ts() == pytest.approx(NOW, abs=1)


def test_once_error_exit_code_prints_reason_and_note(settings, capsys):
    assert make(settings, FakeClient(LIVE, fail="network down")).once("kick: no heartbeat; ran one cycle") == 2
    row = rows(settings.log_csv)[-1]
    assert row["event"] == "Error" and "kick: no heartbeat" in row["action"] and "network down" in row["action"]
    assert "error: status failed: network down" in capsys.readouterr().out


def test_once_crash_row_carries_note(settings):
    c = make(settings, FakeClient(LIVE))
    c._cycle = lambda dry_run, gap_note: (_ for _ in ()).throw(RuntimeError("kaboom"))
    assert c.once("kick: heartbeat 20 min old; ran one cycle") == 2
    row = rows(settings.log_csv)[-1]
    assert row["event"] == "Error" and row["action"].startswith("kick: heartbeat 20 min old") and "kaboom" in row["action"]


def test_once_lock_held_is_skipped_and_printed(settings, capsys):
    c = make(settings, FakeClient(LIVE))
    with open(c.cycle_lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert c.once() == 0
    assert "another cycle is running; skipped" in capsys.readouterr().out
    assert not settings.log_csv.exists()


def test_cycle_lock_is_exclusive(settings):
    """A shared holder must block the cycle; fails if the cycle lock is taken LOCK_SH."""
    c = make(settings, FakeClient(LIVE, sensor={"temp_f": 69.0, "age_seconds": 60}))
    with open(c.cycle_lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert c.cycle()["action"] == "another cycle is running; skipped"
    assert "row" in c.cycle()


def test_once_sigterm_exits_nonzero(settings):
    class Term(FakeClient):
        def status(self):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
            return super().status()

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as e:
        make(settings, Term(LIVE)).once()
    assert e.value.code == 2
    assert signal.getsignal(signal.SIGTERM) is before   # handler restored


# ---- CLI exit code

@pytest.mark.parametrize("code", [0, 2])
def test_cli_once_exit_code(monkeypatch, tmp_path, code):
    monkeypatch.setattr(ctl.config_mod, "load", lambda path=None: config.Settings(device="X", data_dir=tmp_path))
    seen = {}

    def fake_once(self, note=None):
        seen["note"] = note
        return code

    monkeypatch.setattr(ctl.Controller, "once", fake_once)
    with pytest.raises(SystemExit) as e:
        ctl.main(["--once", "--note", "hello"])
    assert e.value.code == code and seen["note"] == "hello"


def test_note_needs_once():
    with pytest.raises(SystemExit):
        ctl.main(["--loop", "--note", "x"])


# ---- loop skip window

def _write_state(settings, last_ok):
    (settings.data_dir / "state.json").write_text(json.dumps({"regime": "heat", "last_ok_ts": last_ok}))


def _run_loop_until_first_cycle(monkeypatch, settings, t0):
    """Run Controller.loop on a fake clock; return the clock time of the first cycle."""
    clock = {"t": t0}
    monkeypatch.setattr(ctl.time, "time", lambda: clock["t"])
    monkeypatch.setattr(ctl.time, "sleep", lambda d: clock.__setitem__("t", clock["t"] + d))
    c = make(settings, FakeClient(LIVE))
    started = []

    def fake_cycle(gap_note=None):
        started.append(clock["t"])
        raise SystemExit(0)

    c.cycle = fake_cycle
    with pytest.raises(SystemExit):
        c.loop(start_delay=0)
    return started[0]


def test_loop_skips_after_recent_outside_cycle(monkeypatch, settings):
    settings.interval_s, settings.skip_recent_s = 600, 300
    _write_state(settings, NOW - 100)                 # kick cycle 100 s ago
    assert _run_loop_until_first_cycle(monkeypatch, settings, NOW) == pytest.approx(NOW + 500, abs=1)


def test_loop_runs_now_when_outside_cycle_is_old(monkeypatch, settings):
    settings.interval_s, settings.skip_recent_s = 600, 300
    _write_state(settings, NOW - 400)
    assert _run_loop_until_first_cycle(monkeypatch, settings, NOW) == pytest.approx(NOW, abs=1)


def test_loop_caps_skip_window_at_half_interval(monkeypatch, settings):
    settings.interval_s, settings.skip_recent_s = 600, 500   # unvalidated Settings: the loop still caps at 300
    _write_state(settings, NOW - 400)
    assert _run_loop_until_first_cycle(monkeypatch, settings, NOW) == pytest.approx(NOW, abs=1)
    c = make(settings, FakeClient(LIVE))
    assert c.skip_until(NOW) is None
    _write_state(settings, NOW - 200)
    assert c.skip_until(NOW) == pytest.approx(NOW + 400, abs=1)


def test_skip_recent_config_and_clamp(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[controller]\nskip_recent_s = 120\n")
    assert config.load(p, env={}).skip_recent_s == 120
    assert config.load(p, env={"MSC_SKIP_RECENT_S": "0"}).skip_recent_s == 0
    p.write_text("[controller]\ninterval_s = 300\nskip_recent_s = 280\n")
    assert config.load(p, env={}).skip_recent_s == 150      # clamped to interval_s / 2
    assert config.load(None, env={"MSC_INTERVAL_S": "120"}).skip_recent_s == 60
    with pytest.raises(ValueError):
        config.load(p, env={"MSC_SKIP_RECENT_S": "-1"})


# ---- scripts/kick.sh

def _fake_proc(*extra):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *extra])


def _stub(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def _kick(data, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("MSC_KICK_")}
    e.update(MSC_DATA_DIR=str(data), **env)
    return subprocess.run(["bash", str(KICK)], capture_output=True, text=True, env=e, timeout=90)


def _starter(tmp_path):
    return _stub(tmp_path / "start.sh", f'{sys.executable} -c "import time; time.sleep(60)" --loop '
                 f'>/dev/null 2>&1 &\necho $! > "$MSC_DATA_DIR/controller.pid"\n')


def _old_heartbeat(tmp_path, age_s=15 * 60):
    hb = tmp_path / "heartbeat.json"
    hb.write_text("{}")
    os.utime(hb, (time.time() - age_s, time.time() - age_s))


def _cleanup_pid(tmp_path):
    try:
        os.kill(int((tmp_path / "controller.pid").read_text()), 9)
    except (OSError, ValueError):
        pass


def test_kick_ok_when_loop_alive_and_heartbeat_fresh(tmp_path):
    p = _fake_proc("--loop")
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        (tmp_path / "heartbeat.json").write_text("{}")
        r = _kick(tmp_path)
        assert r.returncode == 0 and r.stdout.startswith("ok: loop pid")
    finally:
        p.kill()


def test_kick_restarts_dead_loop_and_reports_no_heartbeat(tmp_path):
    (tmp_path / "controller.pid").write_text("999999")   # stale pid file, no heartbeat file
    r = _kick(tmp_path, MSC_KICK_RESTART_CMD=f"{_starter(tmp_path)} 0")
    try:
        assert r.returncode == 0 and r.stdout.startswith("restarted:") and "no heartbeat" in r.stdout
        assert "999999" not in r.stdout
    finally:
        _cleanup_pid(tmp_path)


def test_kick_restarts_when_pid_is_not_the_loop(tmp_path):
    """A live pid without --loop on its command line (pid reuse) is not the loop."""
    p = _fake_proc()
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        (tmp_path / "heartbeat.json").write_text("{}")
        r = _kick(tmp_path, MSC_KICK_RESTART_CMD=f"{_starter(tmp_path)} 0")
        assert r.stdout.startswith("restarted:"), r.stdout
    finally:
        p.kill()
        _cleanup_pid(tmp_path)


def test_kick_exits_when_another_kick_holds_the_lock(tmp_path):
    marker = tmp_path / "ran"
    with open(tmp_path / "kick.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _kick(tmp_path, MSC_KICK_RESTART_CMD=f"touch {marker}")
    assert r.returncode == 0 and r.stdout.startswith("ok: another kick is running")
    assert not marker.exists()


@pytest.mark.parametrize("rc, prefix, code", [(0, "ran-cycle:", 0), (2, "error: cycle exit 2", 1)])
def test_kick_runs_one_cycle_when_heartbeat_stale(tmp_path, rc, prefix, code):
    args = tmp_path / "args.txt"
    py = _stub(tmp_path / "py.sh", f'printf "%s\\n" "$@" > "{args}"\necho "the reason"\nexit {rc}\n')
    p = _fake_proc("--loop")
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        _old_heartbeat(tmp_path)
        r = _kick(tmp_path, MSC_KICK_PYTHON=str(py))
        assert r.returncode == code and r.stdout.startswith(prefix) and "the reason" in r.stdout
        a = args.read_text().split("\n")
        assert "--once" in a and "--note" in a and a[a.index("--note") + 1].startswith("kick: heartbeat 15 min old")
        assert "kick: heartbeat 15 min old" in (tmp_path / "controller.out").read_text()
    finally:
        p.kill()


def test_kick_runs_cycle_when_heartbeat_missing(tmp_path):
    py = _stub(tmp_path / "py.sh", 'echo done\n')
    p = _fake_proc("--loop")
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        r = _kick(tmp_path, MSC_KICK_PYTHON=str(py))
        assert r.stdout.startswith("ran-cycle: no heartbeat"), r.stdout
    finally:
        p.kill()


def test_kick_reports_lock_held_skip_from_real_once(tmp_path):
    """End to end: kick.sh -> real --once, which finds the cycle lock held and says so."""
    p = _fake_proc("--loop")
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        _old_heartbeat(tmp_path)
        with open(tmp_path / "cycle.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = _kick(tmp_path, MSC_KICK_PYTHON=sys.executable, MSC_DEVICE="TEST-UNIT", MSC_CONFIG=os.devnull)
        assert r.returncode == 0 and "(lock held); skipped" in r.stdout, r.stdout + r.stderr
    finally:
        p.kill()


def test_kick_timeout_kills_a_hung_cycle(tmp_path):
    py = _stub(tmp_path / "py.sh", "exec sleep 60\n")
    p = _fake_proc("--loop")
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        _old_heartbeat(tmp_path)
        t = time.time()
        r = _kick(tmp_path, MSC_KICK_PYTHON=str(py), MSC_KICK_WATCHDOG_S="-8", MSC_KICK_TIMEOUT_S="3")
        assert r.returncode == 1 and "timeout" in r.stdout and time.time() - t < 30, r.stdout
    finally:
        p.kill()


@pytest.mark.parametrize("timeout", ["360", "370"])
def test_kick_rejects_timeout_not_above_watchdog_plus_grace(tmp_path, timeout):
    r = _kick(tmp_path, MSC_KICK_TIMEOUT_S=timeout)
    assert r.returncode == 1 and "must exceed" in r.stdout


def test_kick_stale_threshold_is_configurable(tmp_path):
    p = _fake_proc("--loop")
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        _old_heartbeat(tmp_path)
        assert _kick(tmp_path, MSC_KICK_STALE_S="3600").stdout.startswith("ok: loop pid")
    finally:
        p.kill()
