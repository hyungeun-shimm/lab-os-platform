#!/usr/bin/env bash
# Double-click to launch (or open) the Lab Scheduler.
# Server runs in a fully detached session — closing this Terminal window is safe.
# Logs: scheduler.log    Stop: stop_scheduler.command

set -e
cd "$(dirname "$0")"

PORT="${SCHEDULER_PORT:-8766}"
URL="http://127.0.0.1:${PORT}/scheduler.html"
VENV=".venv"

# Already running on this port? Just open the browser.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "▸ Already running on port ${PORT}. Opening browser…"
  open "$URL"
  sleep 1
  exit 0
fi

# Create venv on first run
if [ ! -d "$VENV" ]; then
  echo "▸ Creating virtual environment…"
  python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

echo "▸ Installing / updating dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "▸ Starting server (detached)…"
python3 launch_bg.py
PID="$(cat scheduler.pid)"
echo "▸ Server PID: $PID"

# Wait up to 5s for server to accept connections
for _ in $(seq 1 25); do
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

open "$URL"
echo "▸ Done. This window can be closed."
sleep 1
