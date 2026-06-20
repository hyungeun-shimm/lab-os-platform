@echo off
REM ================================================================
REM   Lab OS — Disable auto-start at Windows login
REM
REM   Removes the startup shortcut. Lab OS will no longer launch
REM   automatically. You can still start it manually by double-clicking
REM   start_labos.bat.
REM ================================================================

setlocal enableextensions

set "VBS=%TEMP%\labos_unautostart_%RANDOM%.vbs"

(
echo Set sh = CreateObject("WScript.Shell"^)
echo Set fso = CreateObject("Scripting.FileSystemObject"^)
echo startupDir = sh.SpecialFolders("Startup"^)
echo lnkPath = startupDir ^& "\Lab OS Server.lnk"
echo If fso.FileExists(lnkPath^) Then
echo     fso.DeleteFile lnkPath
echo     WScript.Echo "Removed."
echo Else
echo     WScript.Echo "Auto-start was not enabled (nothing to remove)."
echo End If
) > "%VBS%"

cscript //nologo "%VBS%"
del "%VBS%" >nul 2>&1

echo.
pause
