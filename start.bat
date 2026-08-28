@echo off
REM ============================================================
REM Trofeo Vision Dashboard launcher — TRAY MODE (no console).
REM
REM Kills TRCC, then spawns the dashboard as a background process
REM with a system tray icon. No console window appears — right-click
REM the tray icon (it lives next to the clock) to pause/resume, open
REM the log folder, or quit.
REM
REM Logs: <repo>\logs\trofeo_vision.log
REM ============================================================

setlocal
pushd "%~dp0"

echo.
echo === Trofeo Vision Dashboard (tray mode) ===
echo.

REM Kill any leftover TRCC and stale Python processes that might hold the USB device.
echo [1/2] Closing TRCC and any stale Python...
taskkill /F /IM TRCC.exe /T >nul 2>&1
taskkill /F /IM python.exe /FI "MEMUSAGE gt 50000" >nul 2>&1

REM Give Windows a moment to release the USB device.
timeout /t 1 /nobreak >nul

echo [2/2] Launching tray icon in background...
echo.
echo The dashboard is now running in the background.
echo Look for the TrofeoVision icon in the system tray (^⇧ next to the clock).
echo Right-click it for Pause / Resume / Quit.
echo Logs: %~dp0logs\trofeo_vision.log
echo.

REM Launch detached. ``start ""`` with no /B closes this console window
REM once pythonw.exe takes over. pythonw.exe itself opens no console,
REM so the dashboard runs purely in the background.
start "" "C:\Program Files\Python314\pythonw.exe" "%~dp0scripts\run_tray.py" --rate 4

popd
endlocal
