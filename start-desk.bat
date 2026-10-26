@echo off
rem ---------------------------------------------------------------------------
rem Paper desk launcher for the A/B/C freqtrade dry-run containers.
rem
rem Why this file exists: the A/B/C accounts only move while their containers
rem are running. With Docker stopped, scripts/dashboard.py keeps reporting the
rem untouched 3 x 1,000 USDT opening balances (3,000 total) forever, which looks
rem like a frozen panel. This script starts the Docker daemon if needed, brings
rem the three dry-run services up and opens the dashboard.
rem
rem Must be started by Explorer (double-click). Do not run it from an assistant
rem shell: processes spawned there die when the shell call returns.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
  echo [desk] docker CLI not found in PATH. Install Docker Desktop first.
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [desk] Docker daemon is down. Starting Docker Desktop ...
  if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
  ) else (
    echo [desk] Docker Desktop.exe not found. Start it manually, then rerun.
    pause
    exit /b 1
  )
  set /a WAITED=0
  :wait_docker
  timeout /t 5 /nobreak >nul
  set /a WAITED+=5
  docker info >nul 2>&1
  if errorlevel 1 (
    if %WAITED% GEQ 180 (
      echo [desk] Docker did not become ready in %WAITED%s. Start it manually.
      pause
      exit /b 1
    )
    goto wait_docker
  )
  echo [desk] Docker ready after %WAITED%s.
) else (
  echo [desk] Docker daemon already running.
)

echo [desk] Starting A/B/C dry-run containers ...
docker compose up -d
if errorlevel 1 (
  echo [desk] docker compose up failed. See the output above.
  pause
  exit /b 1
)

docker compose ps

echo [desk] Checking dashboard on 127.0.0.1:8765 ...
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  set "PY=%~dp0.venv\Scripts\python.exe"
  if not exist "%PY%" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
  if not exist "%PY%" set "PY=py"
  echo [desk] starting dashboard with %PY%
  start "PaperTrade Dashboard" /min "%PY%" scripts\dashboard_server.py
  timeout /t 3 /nobreak >nul
) else (
  echo [desk] dashboard already listening on 8765.
)

echo.
echo [desk] A/B/C containers are up. D and E keep their own launchers:
echo        start-copywatch.bat  ^(D - on-chain copy trading^)
echo        start-memehunter.bat ^(E - self-directed meme desk^)
echo [desk] dashboard: http://127.0.0.1:8765/
start "" http://127.0.0.1:8765/
timeout /t 3 /nobreak >nul
