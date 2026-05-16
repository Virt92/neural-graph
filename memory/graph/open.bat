@echo off
cd /d "%~dp0"
echo Building graph...
py build_graph.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)
echo Starting live watcher in background...
start "Claude Graph Watcher" /min cmd /c "py watch.py"
echo Opening visualizer...
start "" "%~dp0index.html"
