@echo off
cd /d "%~dp0"
if not exist "node_modules" (
    echo node_modules missing, running install...
    call npm install
)
call npm start
