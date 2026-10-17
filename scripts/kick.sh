#!/usr/bin/env bash
# Backup kick for the controller loop. Run it on a schedule (cron or a systemd timer, every 10 minutes
# by default; see README "Pause resilience"). Idempotent and quick:
#  (a) loop not running             -> restart it (MSC_KICK_RESTART_CMD, default scripts/run_loop.sh 0)
#  (b) heartbeat missing or stale   -> run one cycle now (--once, which takes the loop's cycle lock,
#                                      so it never overlaps a loop cycle), capped by a timeout
#  (c) otherwise                    -> nothing
# Prints one status line (ok / restarted / ran-cycle / error) and exits 1 on error.
# Settings (environment or ./.env):
#   MSC_KICK_STALE_S      heartbeat age that triggers a cycle (default 720 = 12 min)
#   MSC_KICK_TIMEOUT_S    hard timeout for that cycle (default 420 = 7 min). Must exceed the cycle
#                         watchdog + 10 s (5 s SIGTERM grace + 5 s SIGKILL wait for the client), so the
#                         cycle can log its own watchdog error before the timeout kills it.
#   MSC_KICK_WATCHDOG_S   the controller's cycle_watchdog_s, for that check (default 360)
#   MSC_KICK_RESTART_CMD  command that restarts the loop (default "scripts/run_loop.sh 0"; with systemd,
#                         "systemctl --user restart minisplit-loop.service")
#   MSC_KICK_PYTHON       Python that runs --once (default python3 from PATH, e.g. your venv)
#   MSC_DATA_DIR          state, heartbeat, pid and log directory (default ./var)
# Credentials come from the environment or ./.env, as for scripts/run_loop.sh.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
DATA="${MSC_DATA_DIR:-var}"
STALE_S="${MSC_KICK_STALE_S:-720}"
TIMEOUT_S="${MSC_KICK_TIMEOUT_S:-420}"
WATCHDOG_S="${MSC_KICK_WATCHDOG_S:-360}"
RESTART_CMD="${MSC_KICK_RESTART_CMD:-scripts/run_loop.sh 0}"
PY="${MSC_KICK_PYTHON:-python3}"
CLIENT_GRACE_S=10   # controller.py: TERM_GRACE_S (5) + SIGKILL wait (5)
if [ "$TIMEOUT_S" -le $(( WATCHDOG_S + CLIENT_GRACE_S )) ]; then
  echo "error: MSC_KICK_TIMEOUT_S ($TIMEOUT_S) must exceed the cycle watchdog ($WATCHDOG_S) + ${CLIENT_GRACE_S} s"
  exit 1
fi
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
hb_age() {  # seconds since heartbeat.json was written (the loop rewrites it every 15 s); empty if missing
  local m
  m=$(stat -c %Y "$DATA/heartbeat.json" 2>/dev/null || stat -f %m "$DATA/heartbeat.json" 2>/dev/null) || return 0
  echo $(( $(date +%s) - m ))
}
hb_text() {  # "no heartbeat" or "heartbeat N min old" / "heartbeat Ns old"
  local a=$1 unit=${2:-s}
  if [ -z "$a" ]; then echo "no heartbeat"
  elif [ "$unit" = m ]; then echo "heartbeat $((a / 60)) min old"
  else echo "heartbeat ${a}s old"; fi
}

if ! loop_alive; then
  age=$(hb_age)
  rm -f "$DATA/controller.pid"
  echo "$(stamp) kick: loop not running ($(hb_text "$age")); restarting" >> "$DATA/controller.out"
  out=$(bash -c "$RESTART_CMD" 9>&- 2>&1)
  for _ in 1 2 3 4 5 6 7 8 9 10; do loop_alive && break; sleep 1; done
  if loop_alive; then
    echo "restarted: loop was not running ($(hb_text "$age")); pid $(cat "$DATA/controller.pid")"
    exit 0
  fi
  echo "error: restart failed: $(printf '%s\n' "$out" | tail -1 | cut -c1-160)"
  exit 1
fi

age=$(hb_age)
if [ -n "$age" ] && [ "$age" -le "$STALE_S" ]; then
  echo "ok: loop pid $(cat "$DATA/controller.pid") alive, $(hb_text "$age")"
  exit 0
fi

note="kick: $(hb_text "$age" m); ran one cycle"
echo "$(stamp) $note" >> "$DATA/controller.out"
out=$(PYTHONPATH=src timeout -k 15 "$TIMEOUT_S" "$PY" -u -m minisplit_controller --once --note "$note" 9>&- 2>&1)
rc=$?
printf '%s\n' "$out" >> "$DATA/controller.out"
last=$(printf '%s\n' "$out" | grep -v '^\s*$' | tail -1 | cut -c1-240)
if printf '%s' "$out" | grep -q 'another cycle is running; skipped'; then
  echo "ok: $(hb_text "$age") but a cycle is already running (lock held); skipped"
elif [ "$rc" -eq 0 ]; then
  echo "ran-cycle: $(hb_text "$age" m); $last"
elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  echo "error: cycle killed by the ${TIMEOUT_S}s timeout ($(hb_text "$age" m)): $last"
  exit 1
else
  echo "error: cycle exit $rc ($(hb_text "$age" m)): $last"
  exit 1
fi
