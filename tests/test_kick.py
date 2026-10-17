"""--once exit codes and --note, the loop's skip window, and scripts/kick.sh (with stubbed commands)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minisplit_controller import config
from minisplit_controller.controller import Controller
from test_controller import LIVE, FakeClient, make, rows

ROOT = Path(__file__).resolve().parents[1]
KICK = ROOT / "scripts" / "kick.sh"


@pytest.fixture
def settings(tmp_path):
    return config.Settings(device="TEST-UNIT", data_dir=tmp_path)


def test_once_ok_and_note(settings):
    c = make(settings, FakeClient(LIVE, sensor={"temp_f": 69.0, "age_seconds": 60}))
    assert c.once("kick: heartbeat 15 min old; ran one cycle") == 0
    assert "kick: heartbeat 15 min old" in rows(settings.log_csv)[-1]["action"]
    assert c.last_ok_ts() == pytest.approx(1_700_000_000.0)


def test_once_error_exit_code(settings):
    assert make(settings, FakeClient(LIVE, fail="network down")).once() == 2
    assert rows(settings.log_csv)[-1]["event"] == "Error"


def test_once_lock_held_is_not_an_error(settings):
    import fcntl
    c = make(settings, FakeClient(LIVE))
    with open(c.cycle_lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert c.once() == 0


def test_skip_recent_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[controller]\nskip_recent_s = 120\n")
    assert config.load(p, env={}).skip_recent_s == 120
    assert config.load(p, env={"MSC_SKIP_RECENT_S": "0"}).skip_recent_s == 0
    with pytest.raises(ValueError):
        config.load(p, env={"MSC_SKIP_RECENT_S": "-1"})


def test_note_needs_once():
    from minisplit_controller.controller import main
    with pytest.raises(SystemExit):
        main(["--loop", "--note", "x"])


# ---- scripts/kick.sh

def _fake_loop():
    """A live process whose command line contains --loop, standing in for the controller loop."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--loop"])


def _stub(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def _kick(data, **env):
    e = dict(os.environ, MSC_DATA_DIR=str(data), **env)
    return subprocess.run(["bash", str(KICK)], capture_output=True, text=True, env=e, timeout=60)


def test_kick_ok_when_loop_alive_and_heartbeat_fresh(tmp_path):
    p = _fake_loop()
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        (tmp_path / "heartbeat.json").write_text("{}")
        r = _kick(tmp_path)
        assert r.returncode == 0 and r.stdout.startswith("ok: loop pid")
    finally:
        p.kill()


def test_kick_restarts_dead_loop(tmp_path):
    starter = _stub(tmp_path / "start.sh", f'{sys.executable} -c "import time; time.sleep(60)" --loop '
                    f'>/dev/null 2>&1 &\necho $! > "$MSC_DATA_DIR/controller.pid"\n')
    (tmp_path / "controller.pid").write_text("999999")   # stale pid file
    r = _kick(tmp_path, MSC_KICK_RUN_LOOP=str(starter))
    pid = int((tmp_path / "controller.pid").read_text())
    try:
        assert r.returncode == 0 and r.stdout.startswith("restarted:")
    finally:
        os.kill(pid, 9)


@pytest.mark.parametrize("rc, prefix, code", [(0, "ran-cycle:", 0), (2, "error: cycle exit 2", 1)])
def test_kick_runs_one_cycle_when_heartbeat_stale(tmp_path, rc, prefix, code):
    args = tmp_path / "args.txt"
    py = _stub(tmp_path / "py.sh", f'printf "%s\\n" "$@" > "{args}"\necho "cycle done"\nexit {rc}\n')
    p = _fake_loop()
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        hb = tmp_path / "heartbeat.json"
        hb.write_text(json.dumps({}))
        old = time.time() - 15 * 60
        os.utime(hb, (old, old))
        r = _kick(tmp_path, MSC_KICK_PYTHON=str(py), MSC_KICK_STALE_S="720", MSC_KICK_TIMEOUT_S="30")
        assert r.returncode == code and r.stdout.startswith(prefix)
        a = args.read_text().split("\n")
        assert "--once" in a and "--note" in a and a[a.index("--note") + 1].startswith("kick: heartbeat 15 min old")
        assert "kick: heartbeat 15 min old" in (tmp_path / "controller.out").read_text()
    finally:
        p.kill()


def test_kick_stale_threshold_is_configurable(tmp_path):
    p = _fake_loop()
    try:
        (tmp_path / "controller.pid").write_text(str(p.pid))
        hb = tmp_path / "heartbeat.json"
        hb.write_text("{}")
        old = time.time() - 15 * 60
        os.utime(hb, (old, old))
        assert _kick(tmp_path, MSC_KICK_STALE_S="3600").stdout.startswith("ok: loop pid")
    finally:
        p.kill()
