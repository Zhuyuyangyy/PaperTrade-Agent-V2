@echo off
REM Six-agent desk: SEARCH -> RISK -> GMGN -> GROK -> FOMO -> EXIT, forever.
REM Only FOMO opens positions and only EXIT closes them; the other four
REM analyse and route. Paper only: no wallet, no key, no real order.
REM Run this by double-clicking: an independent console window survives the
REM agent session, so the desk keeps trading after this chat closes.
cd /d "%~dp0"
set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m research.pipeline.orchestrator loop --sleep 100
