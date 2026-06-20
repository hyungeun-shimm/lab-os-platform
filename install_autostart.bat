@echo off
REM ================================================================
REM   Lab OS — Enable auto-start at Windows login
REM
REM   Double-click ONCE to register. From then on, Lab OS server
REM   launches automatically every time you log into Windows.
REM   The console window starts minimized (visible in the taskbar).
REM
REM   To disable later: double-click uninstall_autostart.bat
REM ================================================================

setlocal enableextensions
cd /d "%~dp0"

if not exist start_labos.bat (
    echo [X] start_labos.bat not found in this folder.
    echo     Run this from inside the Lab_OS folder.
    pause
    exit /b 1
)

set "VBS=%TEMP%\labos_autostart_%RANDOM%.vbs"
set "TARGET=%~dp0start_labos.bat"
set "WORKDIR=%~dp0"
REM Strip trailing backslash so VBScript doesn't see an escape
if "%WORKDIR:~-1%"=="\" set "WORKDIR=%WORKDIR:~0,-1%"

(
echo Set sh = CreateObject("WScript.Shell"^)
echo startupDir = sh.SpecialFolders("Startup"^)
echo Set link = sh.CreateShortcut(startupDir ^& "\Lab OS Server.lnk"^)
echo link.TargetPath = "%TARGET%"
echo link.WorkingDirectory = "%WORKDIR%"
echo link.WindowStyle = 7
echo link.Description = "Lab OS local server"
echo link.Save
echo WScript.Echo "OK"
) > "%VBS%"

echo Registering Lab OS to start at Windows login...
echo Target: %TARGET%
echo.

cscript //nologo "%VBS%"
set RC=%ERRORLEVEL%
del "%VBS%" >nul 2>&1

if %RC% neq 0 (
    echo.
    echo [X] Failed to create the startup shortcut (cscript error %RC%^).
    echo     Try right-clicking this file and choosing "Run as administrator".
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Auto-start enabled.
echo.
echo  Verify it: press Win+R, type   shell:startup   and press Enter.
echo  You should see a file called  "Lab OS Server"  in that folder.
echo.
echo  To remove later, double-click uninstall_autostart.bat
echo ============================================================
echo.
pause
