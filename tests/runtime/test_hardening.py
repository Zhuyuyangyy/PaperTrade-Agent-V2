"""Sprint 4.1 反例：live 必须走 RunLoop；幂等绑 K 线；熔断跨重启；无提权参数。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from research.market.contracts import Candle, TF_1H_MS
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.runtime.circuit import CircuitBreaker, CircuitConfig
from research.runtime.heartbeat import read
from research.runtime.loop import LoopConfig, RunLoop
from research.runtime.sources import CandlesSource, FlakySource
from research.strategy.registry import DEFAULT_SPECS

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _candles(n: int = 300) -> list[Candle]:
    out, price = [], 100.0
    for i in range(n):
        o = price
        price += 0.1
        out.append(Candle(BASE + i * TF_1H_MS, D(f"{o:.6f}"), D(f"{max(o, price) + 0.2:.6f}"),
                          D(f"{min(o, price) - 0.2:.6f}"), D(f"{price:.6f}"), D("1"), TF_1H_MS))
    return out


def _limits() -> RiskLimits:
    return RiskLimits(min_volatility_budget_bps=D("0"), fee_bps=D("0"), slippage_bps=D("0"))


def _verified():
    return tuple(replace(s, verification="VERIFIED", verified_by="test")
                 for s in DEFAULT_SPECS if s.strategy_id == "trend_follow")


def _loop(tmp_path: Path, source, **kw) -> tuple[RunLoop, PaperBroker]:
    broker = PaperBroker(tmp_path / "l.sqlite", starting_cash=D("1000"))
    loop = RunLoop(tmp_path, broker, _limits(), source, specs=_verified(),
                   config=LoopConfig(interval_ms=TF_1H_MS, decision_period_ms=TF_1H_MS), **kw)
    return loop, broker


def test_same_candle_key_blocks_duplicate_decision(tmp_path: Path) -> None:
    candles = _candles()
    loop, broker = _loop(tmp_path, CandlesSource(candles))
    key = candles[265].close_ms // TF_1H_MS * TF_1H_MS
    first = loop.step(key + 1_000)
    second = loop.step(key + 30_000)      # 同一根 1h 内
    assert first is not None and second is None
    assert broker.restore()["orders"] == 1


def test_next_candle_allows_new_decision(tmp_path: Path) -> None:
    candles = _candles()
    loop, _ = _loop(tmp_path, CandlesSource(candles))
    key = candles[265].close_ms // TF_1H_MS * TF_1H_MS
    assert loop.step(key + 1_000) is not None
    assert loop.step(key + TF_1H_MS + 1_000) is not None


def test_circuit_survives_restart(tmp_path: Path) -> None:
    candles = _candles()
    flaky = FlakySource(CandlesSource(candles), fail_at={1, 2, 3})
    loop1, _ = _loop(tmp_path, flaky)
    start = candles[265].close_ms + 31_000
    for i in range(3):
        loop1.step(start + i * TF_1H_MS)
    assert loop1.circuit.halted is True
    hb = read(loop1.heartbeat_path)
    assert hb is not None and hb.circuit.get("halted") is True

    # 重启：新循环不传 circuit，应从心跳恢复"已熔断"
    loop2, broker2 = _loop(tmp_path, CandlesSource(candles))
    assert loop2.circuit.halted is True, "重启不能把熔断状态清零"
    r = loop2.step(start + 10 * TF_1H_MS)
    assert r is not None and r.selection.hold_reason.startswith("HALTED")
    assert broker2.open_positions() == []


def test_live_cli_has_no_privilege_flag(capsys) -> None:
    from research.runtime.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["live", "--verified", "trend_follow"])
    assert exc.value.code == 2
    assert "--verified" in capsys.readouterr().err