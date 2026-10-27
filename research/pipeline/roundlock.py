"""At most one manual round at a time.

`orchestrator once` is how an operator (or a watchdog probing for liveness)
runs a single round by hand. Two of them on the same ledger is not a small
problem: both write the same tables, and the loser of the race reports
"database is locked" on whichever step happened to be mid-write -- the JUDGE
step has been seen failing that way while both probes ran happily.

The lock is a file with a pid and a timestamp. A pid that no longer exists,
or a lock older than STALE_S, is taken over rather than obeyed, so a crashed
round cannot block manual rounds forever.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

LOCK_NAME = ".once.pid"
STALE_S = 600.0


def _lock_path(root: Path) -> Path:
    path = root / "runtime" / "memehunter" / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def acquire(root: Path) -> tuple[bool, str]:
    """(ok, reason). ok=False means another round is already running."""
    path = _lock_path(root)
    if path.is_file():
        try:
            pid_str, ts_str = path.read_text(encoding="utf-8").split()[:2]
            pid, ts = int(pid_str), float(ts_str)
        except (OSError, ValueError):
            pid, ts = 0, 0.0
        age = time.time() - ts
        if _alive(pid) and age < STALE_S:
            return False, f"另一轮正在进行（pid {pid}，已跑 {age:.0f} 秒）"
    path.write_text(f"{os.getpid()} {time.time():.3f}", encoding="utf-8")
    return True, ""


def release(root: Path) -> None:
    path = _lock_path(root)
    try:
        if path.is_file() and path.read_text(encoding="utf-8").split()[:1] == [str(os.getpid())]:
            path.unlink()
    except OSError:
        pass
