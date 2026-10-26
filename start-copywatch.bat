@echo off
rem CopyWatch D account launcher.
rem Must be started by Explorer (double-click or "explorer start-copywatch.bat"),
rem not from the assistant shell: anything spawned under that shell is killed
rem when the shell call returns.
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=py"
if not exist "%PY%" set "PY=python"

echo [CopyWatch] python = %PY%
echo [CopyWatch] cwd    = %CD%

start "CopyWatch Agent" /min "%PY%" -m research.copywatch.agent loop
timeout /t 2 /nobreak >nul
start "CopyWatch Dashboard" /min "%PY%" scripts\dashboard_server.py

echo [CopyWatch] started. dashboard http://127.0.0.1:8765/
timeout /t 3 /nobreak >nul
