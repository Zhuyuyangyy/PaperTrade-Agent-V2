"""运行循环测试（Sprint 4）：幂等、断流熔断、恢复、STOP、心跳。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from research.market.contracts import Candle, TF_1H_MS
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.runtime.circuit import CircuitBreaker, CircuitConfig
from research.runtime.heartbeat import read
from research.runtime.log import read_jsonl
from research.runtime.loop import LoopConfig, LoopStopped, RunLoop
from research.runtime.sources import CandlesSource, FlakySource
from research.strategy.registry import DEFAULT_SPECS

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _candles(n: int = 260, step: float = 0.2) -> list[Candle]:
    out, price = [], 100.0
    for i in range(n):
        o = price
        price += step
        out.append(Candle(BASE + i * TF_1H_MS, D(f"{o:.6f}"), D(f"{max(o, price) + 0.3:.6f}"),
                          D(f"{min(o, price) - 0.3:.6f}"), D(f"{price:.6f}"), D("1"), TF_1H_MS))
    return out


def _verified():
    return tuple(replace(s, verification="VERIFIED", verified_by="test-report")
                 for s in DEFAULT_SPECS if s.strategy_id == "trend_follow")


def _limits() -> RiskLimits:
    return RiskLimits(min_volatility_budget_bps=D("0"), fee_bps=D("0"), slippage_bps=D("0"))


def _loop(tmp_path: Path, source, **kw) -> tuple[RunLoop, PaperBroker]:
    broker = PaperBroker(tmp_path / "l.sqlite", starting_cash=D("1000"))
    loop = RunLoop(tmp_path, broker, _limits(), source, **kw)
    return loop, broker


def test_step_is_idempotent_for_same_timestamp(tmp_path: Path) -> None:
    source = CandlesSource(_candles())
    loop, broker = _loop(tmp_path, source, specs=_verified())
    as_of = source.candles(BASE + 260 * TF_1H_MS)["1h"][-1].close_ms + 31_000

    first = loop.step(as_of)
    second = loop.step(as_of)
    assert first is not None and second is None
    assert len(broker.open_positions()) == 1
    assert broker.restore()["orders"] == 1


def test_outage_trips_circuit_and_blocks_entries(tmp_path: Path) -> None:
    source = FlakySource(CandlesSource(_candles()), fail_at={1, 2, 3, 4, 5})
    loop, broker = _loop(tmp_path, source, specs=_verified(),
                        circuit=CircuitBreaker(CircuitConfig(3, 2)))
    as_of = BASE + 260 * TF_1H_MS
    for i in range(5):
        loop.step(as_of + i * TF_1H_MS)
    assert loop.circuit.halted is True
    assert broker.open_positions() == []
    hb = read(loop.heartbeat_path)
    assert hb is not None and hb.halted is True and hb.consecutive_failures >= 3


def test_recovery_after_two_successes(tmp_path: Path) -> None:
    candles = _candles(300)
    source = FlakySource(CandlesSource(candles), fail_at={1, 2, 3})
    loop, broker = _loop(tmp_path, source, specs=_verified(),
                        circuit=CircuitBreaker(CircuitConfig(3, 2)))
    as_of = candles[265].close_ms + 31_000
    for i in range(3):
        loop.step(as_of + i * TF_1H_MS)
    assert loop.circuit.halted is True
    loop.step(as_of + 3 * TF_1H_MS)      # 成功 1：仍未解除，且不开仓
    assert loop.circuit.halted is True and broker.open_positions() == []
    loop.step(as_of + 4 * TF_1H_MS)      # 成功 2：解除，可以开仓
    assert loop.circuit.halted is False
    assert len(broker.open_positions()) == 1


def test_halted_round_still_logs_halted_reason(tmp_path: Path) -> None:
    source = FlakySource(CandlesSource(_candles()), fail_at={1, 2, 3})
    loop, _ = _loop(tmp_path, source, specs=_verified(),
                    circuit=CircuitBreaker(CircuitConfig(3, 2)))
    as_of = BASE + 260 * TF_1H_MS
    for i in range(3):
        loop.step(as_of + i * TF_1H_MS)
    loop.step(as_of + 3 * TF_1H_MS)      # halted，但数据可取 → 写 HOLD 日志
    entries = read_jsonl(loop.log_path)
    assert entries and entries[-1]["hold_reason"].startswith("HALTED")


def test_stop_file_halts_loop(tmp_path: Path) -> None:
    source = CandlesSource(_candles())
    loop, _ = _loop(tmp_path, source)
    (tmp_path / "STOP").write_text("stop", encoding="utf-8")
    try:
        loop.step(BASE + 260 * TF_1H_MS)
        raise AssertionError("应当抛出 LoopStopped")
    except LoopStopped:
        pass


def test_run_reports_counts_and_heartbeat(tmp_path: Path) -> None:
    source = CandlesSource(_candles(400))
    loop, _ = _loop(tmp_path, source, specs=_verified(),
                    config=LoopConfig(interval_ms=TF_1H_MS, max_rounds=3))
    start = BASE + 260 * TF_1H_MS
    summary = loop.run(start, start + 5 * TF_1H_MS)
    assert summary["executed"] == 3 and summary["stopped"] is False
    hb = read(loop.heartbeat_path)
    assert hb is not None and hb.rounds == 3 and hb.last_ok_ms == start + 2 * TF_1H_MS
    assert len(read_jsonl(loop.log_path)) == 3