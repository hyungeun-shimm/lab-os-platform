"""Spawn server.py in a new session (fully detached from Terminal)."""
import os
import subprocess
import sys

PORT = os.environ.get("SCHEDULER_PORT", "8766")
base = os.path.dirname(os.path.abspath(__file__))
python = os.path.join(base, ".venv", "bin", "python")
if not os.path.exists(python):
    python = sys.executable

log = open(os.path.join(base, "scheduler.log"), "a")
p = subprocess.Popen(
    [python, "server.py", "--port", PORT],
    cwd=base,
    start_new_session=True,   # setsid — fully detached from terminal
    stdout=log,
    stderr=log,
)
pid_path = os.path.join(base, "scheduler.pid")
with open(pid_path, "w") as f:
    f.write(str(p.pid))
print(p.pid)
