@echo off
REM ============================================================
REM  Claude Neural Graph — launch script
REM  Starts Electron visualizer; verifies hooks + daemons.
REM ============================================================
setlocal
cd /d "%~dp0"

echo [neural-graph] checking hooks in ~\.claude\settings.json...
findstr /C:"hook_handler.py" "%USERPROFILE%\.claude\settings.json" >nul 2>&1
if errorlevel 1 (
  echo [neural-graph] WARNING: hook_handler.py not registered in settings.json
  echo   Without hooks, sessions/per-session cores wont appear.
  echo   Add to settings.json under "hooks" -^> SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/SessionEnd:
  echo   "command": "py %CD%\hook_handler.py"
  echo.
) else (
  echo [neural-graph] hooks OK
)

echo [neural-graph] last graph rebuild:
for %%I in (graph_data.js) do echo   %%~tI
echo.

if not exist "electron\node_modules" (
  echo [neural-graph] node_modules missing, running npm install...
  cd electron
  call npm install
  cd ..
)

echo [neural-graph] launching Electron...
cd electron
call npm start
