@echo off
REM Restart the official Thermalright TRCC software.
REM Run this after closing the Python dashboard to get your normal display back.

setlocal
echo.
echo === Restarting TRCC ===
echo.
start "" "C:\Program Files\WindowsApps\Thermalright_TRCC_*\TRCC.exe" 2>nul
if errorlevel 1 (
    echo Could not auto-launch TRCC.
    echo Launch it manually from the Start Menu.
)
echo.
endlocal
