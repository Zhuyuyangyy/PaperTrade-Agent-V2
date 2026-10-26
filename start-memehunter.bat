@echo off
REM Six-agent desk: SEARCH -> RISK -> GMGN -> GROK -> FOMO -> EXIT, forever.
REM Only FOMO opens positions and only EXIT closes them; the other four
REM analyse and route. Paper only: no wallet, no key, no real order.
cd /d "%~dp0"
set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python
:loop
"%PY%" -m research.pipeline.orchestrator once
timeout /t 100 /nobreak >nul
goto loop
