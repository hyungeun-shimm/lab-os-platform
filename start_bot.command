#!/bin/bash
# Lab Assistant Slack Bot launcher
# Double-click to start the Slack bot alongside the scheduler.

cd "$(dirname "$0")"

# Activate venv (created by start_scheduler.command)
if [ -d ".venv" ]; then
    source .venv/bin/activate
else
    echo "⚠️  No .venv found. Run start_scheduler.command first."
    read -p "Press Enter to exit..."
    exit 1
fi

# Install/update bot dependencies
pip install -q slack-bolt apscheduler python-dotenv

# Check .env
if [ ! -f ".env" ]; then
    echo "⚠️  No .env file found."
    echo "Copy .env.example → .env and fill in your Slack tokens."
    echo ""
    cat .env.example
    read -p "Press Enter to exit..."
    exit 1
fi

# Check if scheduler is running
if ! curl -s http://127.0.0.1:8766/api/animals/sync-status > /dev/null 2>&1; then
    echo "⚠️  Scheduler API not running. Start it with start_scheduler.command first."
    echo "Starting anyway (bot will retry API calls)..."
fi

echo "🤖 Starting Lab Assistant Slack Bot..."
python3 slack_bot.py
