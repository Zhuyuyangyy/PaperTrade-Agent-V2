"""Sprint 2 端到端：MarketState → StrategyRegistry → TradeIntent → Risk → Broker → Log。

覆盖：上涨 / 下跌 / 震荡 / 缺口 / 断流 / 无策略 / 影子模式 / 幂等 / 重启恢复。
全部离线、确定性、无网络。
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from research.market.contracts import Candle, TF_1H_MS
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.runtime.log import read_jsonl
from research.runtime.round import run_round
from research.strategy.registry import DEFAULT_SPECS

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS
DAY = 86_400_000


def _candle(i: int, o: float, c: float, tf: int = TF_1H_MS, vol: float = 1.0) -> Candle:
    return Candle(BASE + i * tf, D(f"{o:.6f}"), D(f"{max(o, c) + 0.3:.6f}"),
                  D(f"{min(o, c) - 0.3:.6f}"), D(f"{c:.6f}"), D(f"{vol:.6f}"), tf)


def _ramp(n: int, start: float, step: float, vol_last: float = 0.0) -> list[Candle]:
    out, price = [], start
    for i in range(n):
        o = price
        price += step
        out.append(_candle(i, o, price, vol=vol_last if i == n - 1 else 1.0))
    return out


def _range_with_last_at_low(n: int = 250) -> list[Candle]:
    out = []
    for i in range(n - 1):
        phase = i % 4
        o = 100.0 + (0.15 if phase in (0, 1) else -0.15)
        c = 100.0 + (0.15 if phase in (2, 3) else -0.15)
        out.append(_candle(i, o, c))
    out.append(_candle(n - 1, 99.75, 99.55))
    return out


def _as_of(candles: list[Candle]) -> int:
    return candles[-1].close_ms + 31_000


def _broker(tmp_path: Path) -> PaperBroker:
    return PaperBroker(tmp_path / "ledger.sqlite", starting_cash=D("1000"))


def _verified(*ids: str):
    return tuple(replace(s, verification="VERIFIED", verified_by="test-report")
                 if s.strategy_id in ids else s for s in DEFAULT_SPECS)


def test_uptrend_produces_long_and_fills(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    broker = _broker(tmp_path)
    log = tmp_path / "decisions.jsonl"
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(), log_path=log, specs=_verified("trend_follow"))
    assert res.state.trend == "TREND_UP"
    assert res.selection.decision == "LONG"
    assert res.selection.intent is not None and res.selection.intent.strategy_id == "trend_follow"
    assert res.risk is not None and res.risk.decision == "ALLOW"
    assert res.fill is not None, "VERIFIED 策略应进入模拟账本"
    assert broker.open_positions()[0]["side"] == "LONG"
    entries = read_jsonl(log)
    assert len(entries) == 1
    assert entries[0]["state"]["trend"] == "TREND_UP"
    assert entries[0]["fill"]["intent_id"] == res.fill.intent_id
    assert entries[0]["risk"]["decision"] == "ALLOW"


def test_downtrend_produces_short(tmp_path: Path) -> None:
    candles = _ramp(250, 200, -0.2)
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(), specs=_verified("trend_follow"))
    assert res.state.trend == "TREND_DOWN"
    assert res.selection.decision == "SHORT"
    assert res.fill is not None
    assert broker.open_positions()[0]["side"] == "SHORT"


def test_range_at_lower_edge_triggers_mean_reversion(tmp_path: Path) -> None:
    candles = _range_with_last_at_low()
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(), specs=_verified("range_revert"))
    assert res.state.trend == "RANGE"
    assert res.selection.decision == "LONG"
    assert res.selection.intent is not None
    assert res.selection.intent.strategy_id == "range_revert"


def test_gap_blocks_new_position(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    del candles[100]
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(), specs=_verified("trend_follow"))
    assert res.state.data_quality.contiguous is False
    assert res.risk is not None and res.risk.decision == "DENY"
    assert "DATA_GAPS" in res.risk.codes
    assert res.fill is None and broker.open_positions() == []


def test_stale_1h_freezes_and_blocks(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    as_of = candles[-1].close_ms + 4 * TF_1H_MS
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, as_of, broker,
                    RiskLimits(), specs=_verified("trend_follow"))
    assert res.state.data_quality.critical_stale is True
    assert res.risk is not None and res.risk.decision == "FREEZE"
    assert "DATA_STALE" in res.risk.codes
    assert res.fill is None


def test_unknown_state_holds_with_reason(tmp_path: Path) -> None:
    candles = _ramp(50, 100, 0.2)
    broker = _broker(tmp_path)
    log = tmp_path / "decisions.jsonl"
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(), log_path=log)
    assert res.state.trend == "UNKNOWN"
    assert res.selection.decision == "HOLD"
    assert res.risk is None and res.fill is None
    assert "不足" in res.selection.hold_reason
    assert read_jsonl(log)[0]["hold_reason"]


def test_unverified_strategy_only_shadows(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker, RiskLimits())
    assert res.selection.decision == "LONG"
    assert res.selection.intent is not None and res.selection.intent.shadow is True
    assert res.risk is not None and res.risk.decision == "ALLOW"
    assert res.fill is None, "未验证策略不得进入正式模拟账本"
    assert broker.open_positions() == []


def test_same_round_is_idempotent(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    broker = _broker(tmp_path)
    limits, specs = RiskLimits(), _verified("trend_follow")
    first = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker, limits, specs=specs)
    second = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker, limits, specs=specs)
    assert first.fill is not None and second.fill is None
    assert len(broker.open_positions()) == 1


def test_broker_restores_after_restart(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    path = tmp_path / "ledger.sqlite"
    broker = PaperBroker(path, starting_cash=D("1000"))
    run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
              RiskLimits(), specs=_verified("trend_follow"))
    cash_before = broker.cash()
    open_before = broker.open_positions()
    broker.close()

    reopened = PaperBroker(path, starting_cash=D("1000"))
    assert reopened.cash() == cash_before
    assert len(reopened.open_positions()) == len(open_before)
    assert reopened.restore()["open_positions"] == 1


def test_kill_switch_denies_everything(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(kill_switch=True), specs=_verified("trend_follow"))
    assert res.risk is not None and res.risk.decision == "FREEZE"
    assert "KILL_SWITCH" in res.risk.codes


def test_stop_loss_closes_on_mark(tmp_path: Path) -> None:
    candles = _ramp(250, 100, 0.2)
    broker = _broker(tmp_path)
    res = run_round("BTC/USDT", {"1h": candles}, _as_of(candles), broker,
                    RiskLimits(), specs=_verified("trend_follow"))
    assert res.fill is not None
    pos = broker.open_positions()[0]
    stop = Decimal(pos["stop_price"])
    closed = broker.mark("BTC/USDT", stop - Decimal("1"), _as_of(candles) + TF_1H_MS)
    assert len(closed) == 1 and closed[0].reason == "STOP_LOSS"
    assert closed[0].pnl < 0
    assert broker.open_positions() == []


def test_run_round_expires_positions_past_holding(tmp_path: Path) -> None:
    """run_round 必须执行超时退出，且用当前可用价结算。"""
    candles = _ramp(250, 100, 0.2, vol_last=1.0)
    first_as_of = _as_of(candles)
    broker = _broker(tmp_path)
    specs = tuple(replace(s, verification="VERIFIED", verified_by="test-report",
                          max_holding_ms=3_600_000)
                  for s in DEFAULT_SPECS if s.strategy_id == "trend_follow")
    r1 = run_round("BTC/USDT", {"1h": candles}, first_as_of, broker, RiskLimits(), specs=specs)
    assert r1.fill is not None

    extended = candles + [
        _candle(250, 150.0, 150.1), _candle(251, 150.1, 150.2),
    ]
    later = _as_of(extended)
    r2 = run_round("BTC/USDT", {"1h": extended}, later, broker, RiskLimits(), specs=specs)
    assert any(c.reason == "TIMEOUT" for c in r2.closed), "超时必须被执行"
    assert all(c.exit_price > 0 for c in r2.closed)


def test_submit_never_accepts_shadow_intent(tmp_path: Path) -> None:
    """即使编排层出错，broker 自己也必须拒绝影子意图。"""
    from research.strategy.contracts import TradeIntent as TI

    broker = _broker(tmp_path)
    shadow = TI("s1", BASE, "BTC/USDT", "LONG", "trend_follow", "v1",
                D("100"), D("95"), D("110"), 3_600_000, D("0.7"), "shadow",
                shadow=True)
    assert broker.submit(shadow, D("100")) is None
    assert broker.open_positions() == []