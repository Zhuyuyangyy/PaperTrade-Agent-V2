"""72 小时无人值守验收（离线加速）。

用 1h 步进、400 根合成 K 线模拟 72 轮：
  - 每轮都执行、都写日志、心跳连续；
  - 订单幂等（同一 intent 不重复成交）；
  - 重跑同一区间不产生新订单（重启安全）；
  - 期间注入一次断流：熔断 → 停止开仓 → 恢复后继续。
"""
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
from research.runtime.loop import LoopConfig, RunLoop
from research.runtime.sources import CandlesSource, FlakySource
from research.strategy.registry import DEFAULT_SPECS

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _candles(n: int = 400) -> list[Candle]:
    out, price = [], 100.0
    for i in range(n):
        o = price
        price += 0.05
        out.append(Candle(BASE + i * TF_1H_MS, D(f"{o:.6f}"), D(f"{max(o, price) + 0.2:.6f}"),
                          D(f"{min(o, price) - 0.2:.6f}"), D(f"{price:.6f}"), D("1"), TF_1H_MS))
    return out


def _specs():
    return tuple(replace(s, verification="VERIFIED", verified_by="test-72h")
                 for s in DEFAULT_SPECS if s.strategy_id == "trend_follow")


def _limits() -> RiskLimits:
    return RiskLimits(min_volatility_budget_bps=D("0"), fee_bps=D("0"), slippage_bps=D("0"))


def _loop(tmp_path: Path, source) -> tuple[RunLoop, PaperBroker]:
    broker = PaperBroker(tmp_path / "ledger.sqlite", starting_cash=D("1000"))
    loop = RunLoop(tmp_path, broker, _limits(), source, specs=_specs(),
                   config=LoopConfig(interval_ms=TF_1H_MS))
    return loop, broker


def test_72_hours_unattended_no_duplicate_orders(tmp_path: Path) -> None:
    candles = _candles(400)
    source = CandlesSource(candles)
    loop, broker = _loop(tmp_path, source)
    start = candles[210].close_ms + 31_000
    until = start + 71 * TF_1H_MS
    summary = loop.run(start, until)

    assert summary["executed"] == 72
    assert summary["stopped"] is False
    hb = read(loop.heartbeat_path)
    assert hb is not None and hb.rounds == 72 and hb.last_ok_ms == until
    assert len(read_jsonl(loop.log_path)) == 72

    orders = broker._db.execute("SELECT intent_id FROM orders").fetchall()
    ids = [r["intent_id"] for r in orders]
    assert len(ids) == len(set(ids)), "订单必须唯一"
    assert broker.restore()["orders"] == len(ids)


def test_rerun_same_window_does_not_duplicate(tmp_path: Path) -> None:
    """重启后重跑同一区间：心跳里的 last_round_ts 让循环跳过，订单不增。"""
    candles = _candles(300)
    source = CandlesSource(candles)
    loop, broker = _loop(tmp_path, source)
    start = candles[210].close_ms + 31_000
    until = start + 10 * TF_1H_MS
    loop.run(start, until)
    orders_before = broker.restore()["orders"]

    loop2 = RunLoop(tmp_path, broker, _limits(), source, specs=_specs(),
                    config=LoopConfig(interval_ms=TF_1H_MS))
    summary2 = loop2.run(start, until)
    assert summary2["executed"] == 0 and summary2["skipped"] == 11
    assert broker.restore()["orders"] == orders_before


def test_outage_inside_72h_trips_and_recovers(tmp_path: Path) -> None:
    candles = _candles(400)
    source = FlakySource(CandlesSource(candles), fail_at={20, 21, 22})
    loop, broker = _loop(tmp_path, source)
    loop.circuit = CircuitBreaker(CircuitConfig(fail_threshold=3, recover_threshold=2))
    start = candles[210].close_ms + 31_000
    until = start + 71 * TF_1H_MS
    summary = loop.run(start, until)

    assert loop.circuit.trips == 1
    assert loop.circuit.halted is False, "恢复后必须解除"
    hb = read(loop.heartbeat_path)
    assert hb is not None and hb.rounds == 72
    entries = read_jsonl(loop.log_path)
    assert len(entries) == 69, "失败轮不写决策日志，但心跳仍记录轮次"
    assert any(e["hold_reason"].startswith("HALTED") for e in entries)