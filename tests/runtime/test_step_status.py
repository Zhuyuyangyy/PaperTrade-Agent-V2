"""Step 三态语义（Sprint 4.4）：成功 / 失败 / 重复跳过。

- 只有整轮成功（取数 + run_round）才 record_success 并推进幂等键；
- 失败不推进键，同一根 K 线可以重试；
- 失败计数不会被"取数成功"提前清零；
- run() 分别统计 executed / failed / skipped。
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from research.market.contracts import Candle, TF_1H_MS
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.runtime import loop as loop_module
from research.runtime.circuit import CircuitBreaker, CircuitConfig
from research.runtime.heartbeat import read
from research.runtime.loop import LoopConfig, RunLoop
from research.runtime.sources import CandlesSource, FlakySource
from research.strategy.registry import DEFAULT_SPECS

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _candles(n: int = 320) -> list[Candle]:
    out, price = [], 100.0
    for i in range(n):
        o = price
        price += 0.1
        out.append(Candle(BASE + i * TF_1H_MS, D(f"{o:.6f}"), D(f"{max(o, price) + 0.2:.6f}"),
                          D(f"{min(o, price) - 0.2:.6f}"), D(f"{price:.6f}"), D("1"), TF_1H_MS))
    return out


def _limits() -> RiskLimits:
    return RiskLimits(min_volatility_budget_bps=D("0"), fee_bps=D("0"), slippage_bps=D("0"))


def _specs():
    return tuple(replace(s, verification="VERIFIED", verified_by="test")
                 for s in DEFAULT_SPECS if s.strategy_id == "trend_follow")


def _loop(tmp_path: Path, source, **kw) -> tuple[RunLoop, PaperBroker]:
    broker = PaperBroker(tmp_path / "l.sqlite", starting_cash=D("1000"))
    loop = RunLoop(tmp_path, broker, _limits(), source, specs=_specs(),
                   config=LoopConfig(interval_ms=TF_1H_MS, decision_period_ms=TF_1H_MS), **kw)
    return loop, broker


def test_run_round_failure_keeps_key_and_retries(tmp_path: Path, monkeypatch) -> None:
    candles = _candles()
    loop, _ = _loop(tmp_path, CandlesSource(candles),
                    circuit=CircuitBreaker(CircuitConfig(3, 2)))
    as_of = candles[265].close_ms

    def boom(*a, **kw):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(loop_module, "run_round", boom)
    assert loop.step(as_of) is None
    assert loop.last_status == "FAILED" and "ledger exploded" in loop.last_reason
    hb = read(loop.heartbeat_path)
    assert hb.last_round_key_ms == 0, "失败不能推进幂等键"
    assert hb.successful_rounds == 0
    assert loop.circuit.failures == 1

    # 同一根 K 线重试：不能被当成"已决策过"
    loop.step(as_of + 1_000)
    assert loop.last_status == "FAILED"
    assert loop.circuit.failures == 2


def test_three_failures_trip_circuit(tmp_path: Path, monkeypatch) -> None:
    candles = _candles()
    loop, _ = _loop(tmp_path, CandlesSource(candles),
                    circuit=CircuitBreaker(CircuitConfig(3, 2)))
    as_of = candles[265].close_ms

    def boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(loop_module, "run_round", boom)
    for i in range(3):
        loop.step(as_of + i * 1_000)
    assert loop.circuit.halted is True
    hb = read(loop.heartbeat_path)
    assert hb.halted is True and hb.successful_rounds == 0


def test_success_advances_key_and_counts(tmp_path: Path) -> None:
    candles = _candles()
    loop, broker = _loop(tmp_path, CandlesSource(candles))
    as_of = candles[265].close_ms
    assert loop.step(as_of) is not None
    assert loop.last_status == "OK"
    hb = read(loop.heartbeat_path)
    assert hb.successful_rounds == 1
    assert hb.last_round_key_ms == as_of // TF_1H_MS * TF_1H_MS
    assert loop.step(as_of + 1_000) is None
    assert loop.last_status == "SKIPPED" and loop.last_reason == "duplicate_key"


def test_run_summary_separates_outcomes(tmp_path: Path) -> None:
    candles = _candles(340)
    source = FlakySource(CandlesSource(candles), fail_at={1, 2})
    loop, _ = _loop(tmp_path, source)
    loop.config.max_rounds = 2
    start = candles[265].close_ms
    summary = loop.run(start, start + 10 * TF_1H_MS)
    assert summary["failed"] == 2
    assert summary["executed"] == 2
    assert summary["skipped"] == 0
    hb = read(loop.heartbeat_path)
    assert hb.successful_rounds == 2 and hb.rounds == 4