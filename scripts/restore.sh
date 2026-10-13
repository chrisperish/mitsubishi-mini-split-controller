#!/usr/bin/env bash
# Stop the controller loop and put the unit back to stock settings.
# Defaults: auto mode, heat 67F, cool 73F, fan auto, vane swing. Override with RESTORE_* env vars.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
: "${MSC_DEVICE:?set MSC_DEVICE}"
DATA="${MSC_DATA_DIR:-var}"
if [ -f "$DATA/controller.pid" ]; then
  PID=$(cat "$DATA/controller.pid")
  kill "$PID" 2>/dev/null && echo "stopping controller pid $PID"
  for _ in $(seq 1 15); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -0 "$PID" 2>/dev/null && { echo "still running; sending SIGKILL"; kill -9 "$PID"; }
fi
python3 client/mitsubishi.py -d "$MSC_DEVICE" set --mode "${RESTORE_MODE:-auto}" \
  --heat-temp "${RESTORE_HEAT:-67}" --cool-temp "${RESTORE_COOL:-73}" \
  --fan "${RESTORE_FAN:-auto}" --vane "${RESTORE_VANE:-swing}"
rc=$?
sleep 3
python3 client/mitsubishi.py -d "$MSC_DEVICE" status
exit $rc
