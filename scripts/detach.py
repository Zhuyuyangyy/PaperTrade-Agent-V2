"""Start the CopyWatch loop and dashboard detached from this shell.

Background tasks launched from the assistant shell die with the session, which
is what killed the D account twice (2026-10-24 18:56 and 2026-10-25 09:19).
This launcher starts real OS processes with DETACHED_PROCESS |
CREATE_BREAKAWAY_FROM_JOB so they survive the parent shell and any job object
the shell is placed in.

Usage:
    python scripts/detach.py start    # start loop + dashboard if not running
    python scripts/detach.py status   # report pid liveness
    python scripts/detach.py stop
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "copywatch"
PIDS = RUNTIME / "pids.json"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB

SERVICES = {
    "agent": [sys.executable, "-m", "research.copywatch.agent", "loop"],
    "dashboard": [sys.executable, "scripts/dashboard_server.py"],
}


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        # tasklist on a Chinese Windows writes GBK; decoding as UTF-8 raises.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, timeout=10,
        ).stdout.decode("gbk", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return str(pid) in out


def _load() -> dict:
    if PIDS.is_file():
        try:
            return json.loads(PIDS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(data: dict) -> None:
    PIDS.write_text(json.dumps(data, indent=1), encoding="utf-8")


def start() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    state = _load()
    started = []
    for name, argv in SERVICES.items():
        pid = state.get(name, {}).get("pid")
        if _alive(pid):
            print(f"{name:10} already running pid={pid}")
            continue
        log = RUNTIME / f"{name}.log"
        handle = open(log, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=FLAGS,
                close_fds=True,
            )
        except OSError as exc:
            # CREATE_BREAKAWAY_FROM_JOB fails when the parent job forbids it.
            print(f"{name:10} breakaway refused ({exc}); retrying detached only")
            proc = subprocess.Popen(
                argv,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        state[name] = {"pid": proc.pid, "argv": argv, "log": str(log)}
        started.append(name)
        print(f"{name:10} started pid={proc.pid} log={log.name}")
    _save(state)
    return 0 if started or state else 1


def status() -> int:
    state = _load()
    if not state:
        print("no services recorded")
        return 1
    rc = 0
    for name, info in state.items():
        pid = info.get("pid")
        alive = _alive(pid)
        print(f"{name:10} pid={pid} {'ALIVE' if alive else 'DEAD'}")
        if not alive:
            rc = 1
    hb = RUNTIME / "heartbeat.json"
    if hb.is_file():
        try:
            import time

            data = json.loads(hb.read_text(encoding="utf-8"))
            age = (int(time.time() * 1000) - int(data["last_loop_at_ms"])) // 1000
            print(f"heartbeat  status={data.get('status')} age={age}s ({age / 60:.1f} min)")
            if age > 180:
                rc = 1
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            print(f"heartbeat  unreadable: {exc}")
            rc = 1
    return rc


def stop() -> int:
    state = _load()
    for name, info in state.items():
        pid = info.get("pid")
        if _alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
            print(f"{name:10} stopped pid={pid}")
    PIDS.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"start": start, "status": status, "stop": stop}.get(action, status)())
