"""Kill switch and log rotation: the two unattended-run guards."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.guards import (  # noqa: E402
    LOG_MAX_BYTES,
    guard_round,
    rotate_log,
    stop_requested,
    stop_file_path,
)


def test_stop_file_halts_the_round(tmp_path: Path) -> None:
    assert stop_requested(tmp_path) is False
    stop_file_path(tmp_path).write_text("halt\n", encoding="utf-8")
    assert stop_requested(tmp_path) is True
    may_run, reason = guard_round(tmp_path)
    assert may_run is False and "STOP" in reason


def test_deleting_stop_resumes(tmp_path: Path) -> None:
    stop_file_path(tmp_path).write_text("halt\n", encoding="utf-8")
    stop_file_path(tmp_path).unlink()
    may_run, _ = guard_round(tmp_path)
    assert may_run is True


def test_small_log_is_not_rotated(tmp_path: Path) -> None:
    log = tmp_path / "loop.log"
    log.write_text("x" * 1024, encoding="utf-8")
    assert rotate_log(log) is False
    assert log.exists()


def test_large_log_rotates_with_bounded_generations(tmp_path: Path) -> None:
    log = tmp_path / "loop.log"
    for round_no in range(6):
        log.write_text("y" * (LOG_MAX_BYTES + 10), encoding="utf-8")
        rotate_log(log)
    generations = sorted(tmp_path.glob("loop.log.*"))
    assert len(generations) <= 3
    # The freshest generation is .1 and the oldest is dropped.
    assert generations[-1].name == "loop.log.3"


def test_missing_log_is_not_an_error(tmp_path: Path) -> None:
    assert rotate_log(tmp_path / "absent.log") is False


def test_guard_round_rotates_and_keeps_running(tmp_path: Path) -> None:
    log = tmp_path / "loop.log"
    log.write_text("z" * (LOG_MAX_BYTES + 10), encoding="utf-8")
    may_run, note = guard_round(tmp_path, log)
    assert may_run is True and note == "log rotated"
