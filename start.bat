@echo off
REM ============================================================
REM Trofeo Vision Dashboard launcher
REM Kills TRCC and starts the live Python dashboard.
REM Ctrl+C in this window stops the dashboard (LCD will go blank).
REM ============================================================

REM Anchor to this script's directory regardless of where it's invoked from.
setlocal
pushd "%~dp0"

echo.
echo === Trofeo Vision Dashboard ===
echo.

REM Kill any leftover TRCC and stale Python processes that might hold the USB device.
echo [1/2] Closing TRCC and any stale Python...
taskkill /F /IM TRCC.exe /T >nul 2>&1
taskkill /F /IM python.exe /FI "MEMUSAGE gt 50000" >nul 2>&1

REM Give Windows a moment to release the USB device.
timeout /t 1 /nobreak >nul

echo [2/2] Starting dashboard at 4 fps (verbose)...
echo.
echo Close this window (or press Ctrl+C) to stop the dashboard.
echo Restart TRCC afterward via the Start Menu if you want it back.
echo.

REM Run the dashboard with --diagnose to print LHM status + USB state,
REM then --rate 4 for the actual push. If something is broken the diagnostic
REM output will show it before the live loop starts.
"C:\Program Files\Python314\python.exe" -m trofeo_vision.main --rate 4 --diagnose

REM If we get here, the script exited. Show any error and pause so the window doesn't vanish.
echo.
if %ERRORLEVEL% neq 0 (
    echo Dashboard exited with error code %ERRORLEVEL%.
) else (
    echo Dashboard stopped cleanly.
)
echo.
pause

popd
endlocal
