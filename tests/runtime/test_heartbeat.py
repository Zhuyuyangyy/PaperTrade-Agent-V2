"""心跳与熔断测试（Sprint 4）。"""
from __future__ import annotations

from research.runtime.circuit import CircuitBreaker, CircuitConfig
from research.runtime.heartbeat import Heartbeat, read, staleness_ms, write


def test_heartbeat_roundtrip(tmp_path) -> None:
    p = tmp_path / "state" / "hb.json"
    write(p, Heartbeat(updated_at_ms=10, last_ok_ms=10, last_round_ts_ms=10, rounds=1))
    hb = read(p)
    assert hb is not None and hb.rounds == 1 and hb.last_ok_ms == 10


def test_heartbeat_missing_is_none(tmp_path) -> None:
    assert read(tmp_path / "nope.json") is None


def test_staleness_none_before_first_success() -> None:
    assert staleness_ms(Heartbeat(), 100) is None
    assert staleness_ms(Heartbeat(last_ok_ms=40), 100) == 60


def test_circuit_trips_and_recovers() -> None:
    cb = CircuitBreaker(CircuitConfig(fail_threshold=3, recover_threshold=2))
    cb.record_failure("a")
    cb.record_failure("b")
    assert cb.halted is False
    cb.record_failure("c")
    assert cb.halted is True and cb.trips == 1 and "连续失败" in cb.reason

    cb.record_success()
    assert cb.halted is True, "一次成功不足以解除"
    cb.record_success()
    assert cb.halted is False and cb.reason == ""


def test_failure_resets_recovery_progress() -> None:
    cb = CircuitBreaker(CircuitConfig(3, 2))
    for i in range(3):
        cb.record_failure(str(i))
    cb.record_success()
    cb.record_failure("again")
    cb.record_success()
    assert cb.halted is True, "恢复过程中再次失败要重新计数"


def test_circuit_dict_shape() -> None:
    d = CircuitBreaker().to_dict()
    for k in ("halted", "reason", "failures", "trips", "fail_threshold", "recover_threshold"):
        assert k in d