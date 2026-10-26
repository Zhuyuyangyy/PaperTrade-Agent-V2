"""Kill switch and log rotation: two guards the desk was running without.

Both come straight from the Polymarket playbook's "unattended death list":

* a STOP file in the project root halts the loop within one round -- without it,
  the only way to stop a runaway desk is to find and kill the process;
* logs rotate by size, because an ever-growing file eventually fills the disk
  and the desk dies of something dumber than any market.

Neither affects a trading decision. Both are checked before anything else.
"""
from __future__ import annotations

import os
from pathlib import Path

# A file literally named STOP in the project root. Creating it is the operator's
# emergency stop; deleting it resumes on the next round.
STOP_FILENAME = "STOP"
LOG_MAX_BYTES = 8 * 1024 * 1024  # 8 MB per file, 3 generations kept
LOG_KEEP = 3


def stop_file_path(root: Path) -> Path:
    return Path(root) / STOP_FILENAME


def stop_requested(root: Path) -> bool:
    """True when the operator has asked the desk to halt."""
    return stop_file_path(root).exists()


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> bool:
    """Rotate a log file when it grows past max_bytes.

    Keeps `keep` generations (log.1 .. log.keep) and drops the oldest, so disk
    use is bounded no matter how long the desk runs. Returns True when rotated.
    """
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    oldest = path.with_suffix(path.suffix + f".{keep}")
    try:
        if oldest.exists():
            oldest.unlink()
        for index in range(keep - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{index}")
            if src.exists():
                src.replace(path.with_suffix(path.suffix + f".{index + 1}"))
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        return False
    return True


def guard_round(root: Path, log_path: Path | None = None) -> tuple[bool, str]:
    """Call once per round, before any work. Returns (may_run, reason)."""
    if stop_requested(root):
        return False, f"{STOP_FILENAME} file present: desk halted (delete it to resume)"
    if log_path is not None and rotate_log(log_path):
        return True, "log rotated"
    return True, ""
