@echo off
cd /d "%~dp0"
echo Installing electron (one-time, ~150MB download)...
call npm install
if errorlevel 1 (
    echo Install failed.
    pause
    exit /b 1
)
echo Done. Run start.bat to launch.
pause
