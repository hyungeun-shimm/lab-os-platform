@echo off
REM ================================================================
REM   Lab OS — One-shot rig setup (Windows)
REM
REM   Double-click this from inside the Lab_OS folder. It will:
REM     1) Create a Python virtual environment in .venv\
REM     2) Install all Python dependencies
REM     3) Ask you for the machine ID + Google Drive folder path
REM     4) Initialize the local DB and import snapshot.json from main
REM
REM   Run this ONCE on each rig (slice-rig, behavior-rig).
REM   After it finishes, double-click start_labos.bat to launch the server.
REM ================================================================

setlocal enableextensions
cd /d "%~dp0"

echo.
echo === Lab OS rig setup ===
echo.

REM --- 1. Check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python is not installed or not on PATH.
    echo     Install it from https://www.python.org/downloads/
    echo     and re-run the installer with "Add Python to PATH" checked.
    pause
    exit /b 1
)

REM --- 2. Create venv if missing ---
if not exist .venv (
    echo [1/4] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Virtual environment already exists.
)

REM --- 3. Install dependencies ---
echo [2/4] Installing Python dependencies (this may take a few minutes)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Failed to install dependencies. Scroll up to see the error.
    pause
    exit /b 1
)

REM --- 4. Ask for config ---
echo [3/4] Configuration
echo.
echo This rig needs a unique ID. Examples:  slice-rig  behavior-rig
set /p MID="    Machine ID for this rig: "
if "%MID%"=="" (
    echo [X] Machine ID cannot be empty.
    pause
    exit /b 1
)

echo.
echo Find the ELN_sync folder in Explorer ^(Google Drive^) and copy its
echo full path. Examples:
echo     G:\My Drive\ELN_sync
echo     C:\Users\you\My Drive\ELN_sync
echo.
set /p SROOT="    Sync folder path: "
if "%SROOT%"=="" (
    echo [X] Sync folder path cannot be empty.
    pause
    exit /b 1
)

REM --- 5. Bootstrap ---
echo.
echo [4/4] Bootstrapping local DB and importing snapshot from main...
echo.
python bootstrap_rig.py --machine-id "%MID%" --sync-root "%SROOT%"
if errorlevel 1 (
    echo.
    echo [X] Bootstrap failed. See the error above.
    echo     Common causes:
    echo       - snapshot.json hasn't finished downloading from Drive yet
    echo       - sync folder path is wrong (check Drive icon in Explorer)
    echo       - main hasn't run "Export now" yet
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Setup complete.
echo.
echo  To start the server, double-click:  start_labos.bat
echo ============================================================
echo.
pause
