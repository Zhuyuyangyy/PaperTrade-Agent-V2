#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
PY="C:/Users/联想/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
for i in $(seq 1 30); do
  echo "=== cycle $i $(date -u +%H:%M:%S) ==="
  "$PY" scripts/memehunter.py scan 2>&1 | tail -4
  "$PY" scripts/memehunter.py update 2>&1 | tail -4
  sleep 100
done
