#!/usr/bin/env bash
# Backup kick for the controller loop. Run it on a schedule (cron or a systemd timer, every 10 minutes
# by default; see README "Pause resilience"). Idempotent and quick:
#  (a) loop not running             -> start it with scripts/run_loop.sh
#  (b) heartbeat older than STALE   -> run one cycle now (--once, which takes the loop's cycle lock,
#                                      so it never overlaps a loop cycle), capped by a timeout
#  (c) otherwise                    -> nothing
# Prints one status line (ok / restarted / ran-cycle / error) and exits 1 on error.
# Settings (environment or ./.env):
#   MSC_KICK_STALE_S    heartbeat age that triggers a cycle (default 720 = 12 min)
#   MSC_KICK_TIMEOUT_S  hard timeout for that cycle (default 360 = 6 min)
#   MSC_DATA_DIR        state, heartbeat, pid and log directory (default ./var)
# Credentials come from the environment or ./.env, as for scripts/run_loop.sh.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
DATA="${MSC_DATA_DIR:-var}"
STALE_S="${MSC_KICK_STALE_S:-720}"
TIMEOUT_S="${MSC_KICK_TIMEOUT_S:-360}"
RUN_LOOP="${MSC_KICK_RUN_LOOP:-scripts/run_loop.sh}"   # overridable for tests
PY="${MSC_KICK_PYTHON:-python3}"
mkdir -p "$DATA" || { echo "error: cannot create $DATA"; exit 1; }

exec 9>"$DATA/kick.lock"
flock -n 9 || { echo "ok: another kick is running"; exit 0; }

stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
loop_alive() {  # pid file names a live process that is the controller loop
  local p; p=$(cat "$DATA/controller.pid" 2>/dev/null) || return 1
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null || return 1
  [ -r "/proc/$p/cmdline" ] || return 0   # no /proc: trust the live pid
  tr '\0' ' ' <"/proc/$p/cmdline" | grep -q -- '--loop'
}
hb_age() {  # seconds since heartbeat.json was written (the loop rewrites it every 15 s while idle)
  local m
  m=$(stat -c %Y "$DATA/heartbeat.json" 2>/dev/null || stat -f %m "$DATA/heartbeat.json" 2>/dev/null) \
    || { echo 999999; return; }
  echo $(( $(date +%s) - m ))
}

if ! loop_alive; then
  age=$(hb_age)
  rm -f "$DATA/controller.pid"
  echo "$(stamp) kick: loop not running (heartbeat ${age}s old); restarting" >> "$DATA/controller.out"
  out=$("$RUN_LOOP" 0 9>&- 2>&1)
  if loop_alive; then
    echo "restarted: loop was not running (heartbeat ${age}s old); pid $(cat "$DATA/controller.pid")"
    exit 0
  fi
  echo "error: restart failed: $(printf '%s\n' "$out" | tail -1 | cut -c1-160)"
  exit 1
fi

age=$(hb_age)
if [ "$age" -le "$STALE_S" ]; then
  echo "ok: loop pid $(cat "$DATA/controller.pid") alive, heartbeat ${age}s old"
  exit 0
fi

note="kick: heartbeat $((age / 60)) min old; ran one cycle"
echo "$(stamp) $note" >> "$DATA/controller.out"
out=$(PYTHONPATH=src timeout -k 15 "$TIMEOUT_S" "$PY" -u -m minisplit_controller --once --note "$note" 9>&- 2>&1)
rc=$?
printf '%s\n' "$out" >> "$DATA/controller.out"
last=$(printf '%s\n' "$out" | grep -v '^\s*$' | tail -1 | cut -c1-240)
if printf '%s' "$out" | grep -q 'another cycle is running; skipped'; then
  echo "ok: heartbeat ${age}s old but a cycle is already running (lock held); skipped"
elif [ "$rc" -eq 0 ]; then
  echo "ran-cycle: heartbeat $((age / 60)) min old; $last"
else
  echo "error: cycle exit $rc (heartbeat $((age / 60)) min old): $last"
  exit 1
fi
