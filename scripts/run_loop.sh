#!/usr/bin/env bash
# Start the controller loop detached from the terminal. Optional arg: start delay in seconds.
# Reads MSC_* and MITSUBISHI_COMFORT_* from the environment (or ./.env if present).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
DATA="${MSC_DATA_DIR:-var}"
mkdir -p "$DATA"
if [ -f "$DATA/controller.pid" ] && kill -0 "$(cat "$DATA/controller.pid")" 2>/dev/null; then
  echo "already running: pid $(cat "$DATA/controller.pid")"; exit 0
fi
PYTHONPATH=src setsid nohup python3 -u -m minisplit_controller --loop --start-delay "${1:-0}" \
  >> "$DATA/controller.out" 2>&1 < /dev/null &
sleep 2
echo "started pid $(cat "$DATA/controller.pid" 2>/dev/null || echo '?')"
