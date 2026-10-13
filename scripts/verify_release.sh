#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
LABOS_PYTHON="${LABOS_PYTHON:-.venv/bin/python}"
"$LABOS_PYTHON" -m pytest tests -q
"$LABOS_PYTHON" -m modules.autoschedule.test_engine
"$LABOS_PYTHON" -m modules.autoschedule.test_service
"$LABOS_PYTHON" scripts/check_frontend.py
node scripts/test_frontend.cjs
git diff --check
