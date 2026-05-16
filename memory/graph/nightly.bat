@echo off
REM Nightly reflection + graph rebuild. Register in Task Scheduler for daily 03:13 run.
REM
REM One-time setup (run PowerShell as admin):
REM   schtasks /Create /SC DAILY /ST 03:13 /TN "ClaudeNeuralGraph_Nightly" ^
REM     /TR "C:\Users\PC\.claude\projects\C--Users-PC\memory\graph\nightly.bat"
REM
cd /d "%~dp0"
echo [%date% %time%] Starting nightly reflection >> nightly.log
py reflect.py >> nightly.log 2>&1
py decisions.py >> nightly.log 2>&1
py baselines.py >> nightly.log 2>&1
py anomalies.py >> nightly.log 2>&1
py process_watcher.py >> nightly.log 2>&1
py embed.py >> nightly.log 2>&1
py build_graph.py >> nightly.log 2>&1
py health.py >> nightly.log 2>&1
py growth.py >> nightly.log 2>&1
py signals.py >> nightly.log 2>&1
py curiosity_loop.py >> nightly.log 2>&1
py gaps.py >> nightly.log 2>&1
py causality.py >> nightly.log 2>&1
py snapshot.py >> nightly.log 2>&1
echo [%date% %time%] Done >> nightly.log
