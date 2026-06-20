@echo off
REM ================================================================
REM   Lab OS — Start the local server (Windows)
REM
REM   Double-click to launch. The console window stays open so you
REM   can see the server log. Close the window to stop the server.
REM
REM   Then open in your browser:    http://localhost:8766/eln/
REM ================================================================

setlocal enableextensions
cd /d "%~dp0"

if not exist .venv (
    echo [X] No virtual environment found. Run setup_rig.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo === Lab OS server starting on http://localhost:8766/eln/ ===
echo     Close this window to stop the server.
echo.

python server.py --port 8766

REM Pause so the window doesn't vanish if the server crashes
pause
