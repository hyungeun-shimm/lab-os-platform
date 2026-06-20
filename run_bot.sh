#!/bin/bash
# Watchdog wrapper for slack_bot.py
# Restarts the bot automatically if it crashes or loses network.
cd "$(dirname "$0")"
source .venv/bin/activate

echo "[watchdog] Starting Lab Assistant bot..."

while true; do
    python3 -u slack_bot.py
    EXIT_CODE=$?
    echo "[watchdog] Bot exited with code $EXIT_CODE at $(date). Restarting in 10s..."
    sleep 10
done
