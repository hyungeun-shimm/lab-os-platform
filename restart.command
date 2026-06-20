#!/bin/bash
cd "$(dirname "$0")"

echo "🔄 Restarting Lab Scheduler + Bot..."

# Stop existing processes
pkill -f "server.py" 2>/dev/null
pkill -f "run_bot.sh" 2>/dev/null
pkill -f "slack_bot.py" 2>/dev/null
sleep 1

# Activate venv
source .venv/bin/activate

# Start server
nohup python3 -u server.py --port 8766 > scheduler.log 2>&1 &
echo $! > scheduler.pid
echo "✅ Server started (PID $(cat scheduler.pid))"

sleep 2

# Start bot watchdog (auto-restarts on crash/network drop)
if grep -q "xoxb-" .env 2>/dev/null; then
    nohup bash run_bot.sh > slack_bot.log 2>&1 &
    echo $! > slack_bot.pid
    echo "✅ Bot watchdog started (PID $(cat slack_bot.pid))"
else
    echo "⚠️  Bot skipped (.env not configured)"
fi

sleep 2
echo ""
echo "🌐 http://localhost:8766/scheduler.html"
echo "📋 Logs: tail -f scheduler.log"
echo "🤖 Bot log: tail -f slack_bot.log"
