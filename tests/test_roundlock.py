"""One manual round at a time -- the watchdog's probes proved it is needed."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pipeline.roundlock import STALE_S, acquire, release  # noqa: E402


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "runtime" / "memehunter").mkdir(parents=True)
    return tmp_path


def test_a_second_round_is_refused_while_one_runs(root: Path):
    assert acquire(root)[0] is True
    ok, why = acquire(root)
    assert ok is False
    assert "另一轮" in why
    release(root)
    assert acquire(root)[0] is True


def test_release_only_removes_our_own_lock(root: Path):
    acquire(root)
    path = root / "runtime" / "memehunter" / ".once.pid"
    path.write_text("999999 0.0", encoding="utf-8")  # somebody else's lock
    release(root)
    assert path.is_file(), "must not delete a lock we do not own"


def test_a_stale_lock_is_taken_over(root: Path):
    """A crashed round must not block manual rounds forever."""
    path = root / "runtime" / "memehunter" / ".once.pid"
    path.write_text(f"{os.getpid()} {time.time() - STALE_S - 60:.3f}", encoding="utf-8")
    ok, _ = acquire(root)
    assert ok is True


def test_a_dead_holders_lock_is_taken_over(root: Path):
    path = root / "runtime" / "memehunter" / ".once.pid"
    path.write_text("999999 0.0", encoding="utf-8")  # a pid that is not us
    ok, _ = acquire(root)
    assert ok is True
