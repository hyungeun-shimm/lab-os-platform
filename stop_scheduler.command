#!/usr/bin/env bash
# Stop the background Lab Scheduler server.

cd "$(dirname "$0")"
PORT="${SCHEDULER_PORT:-8766}"
PIDFILE="scheduler.pid"

stopped=0

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "▸ Stopping PID $PID …"
    kill "$PID" || true
    sleep 0.5
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" || true
    fi
    stopped=1
  fi
  rm -f "$PIDFILE"
fi

# Fallback: any uvicorn listening on the port
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  PIDS="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t)"
  echo "▸ Killing process(es) on port $PORT: $PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 0.3
  kill -9 $PIDS 2>/dev/null || true
  stopped=1
fi

if [ "$stopped" -eq 1 ]; then
  echo "▸ Lab Scheduler stopped."
else
  echo "▸ Lab Scheduler was not running."
fi
sleep 1
