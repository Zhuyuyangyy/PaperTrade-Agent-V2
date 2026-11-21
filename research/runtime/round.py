"""一轮完整决策（Sprint 2）。

顺序固定，每一步都能单独测试：

    MarketState → StrategyRegistry → TradeIntent → RiskDecision → PaperBroker → DecisionLog

要点：
  - 先 mark 已有持仓（止损/止盈），再决定新仓；
  - 策略产出的是 ``TradeIntent``，不是订单；
  - 风控是独立函数，策略/LLM 无权绕过；
  - 只有 ``ALLOW`` 且非影子意图才会真正提交给 PaperBroker；
  - 影子意图仍走完整风控并写入日志（``shadow_allowed``），供后续独立验证。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from research.market.contracts import Candle, MarketState
from research.market.klines import closed_only
from research.market.state import analyze
from research.paper.broker import ClosedPosition, Fill, PaperBroker
from research.risk.engine import (
    AccountSnapshot, RiskDecision, RiskLimits, evaluate, evaluate_market_gate,
)
from research.strategy.contracts import Selection, StrategySpec
from research.strategy.registry import DEFAULT_SPECS, select

from .log import append_jsonl


@dataclass(frozen=True)
class RoundResult:
    state: MarketState
    selection: Selection
    risk: RiskDecision | None
    fill: Fill | None
    closed: tuple[ClosedPosition, ...]
    equity: Decimal
    log: dict


def _mark_price(state: MarketState) -> Decimal | None:
    for tf in ("1h", "15m", "5m", "4h"):
        view = state.timeframes.get(tf)
        if view is not None:
            return view.close
    return None


def run_round(symbol: str, candles_by_tf: Mapping[str, list[Candle]], as_of_ms: int,
              broker: PaperBroker, limits: RiskLimits,
              log_path: Path | str | None = None,
              specs: tuple[StrategySpec, ...] = DEFAULT_SPECS,
              spread_bps: Decimal = Decimal("5"),
              day_start_ms: int = 0,
              settle_ms: int = 30_000,
              intel=None,
              allow_new_entries: bool = True,
              halt_reason: str = "") -> RoundResult:
    state = analyze(symbol, candles_by_tf, as_of_ms, settle_ms=settle_ms)

    mark = _mark_price(state)
    closed: list[ClosedPosition] = []
    if mark is not None:
        # 用 1h 最后一根已收盘 K 线做保守 mark（跳空按开盘价），再处理超时
        h1 = closed_only(sorted(candles_by_tf.get("1h") or [], key=lambda c: c.open_ms),
                         as_of_ms, settle_ms)
        if h1:
            bar = h1[-1]
            closed += broker.mark_bar(symbol, bar.open, bar.high, bar.low, as_of_ms)
        closed += broker.expire(as_of_ms, {symbol: mark})

    selection = select(state, specs)
    if not allow_new_entries:
        selection = Selection("HOLD", None, selection.candidates,
                              f"HALTED: {halt_reason or 'circuit open'}")
    risk: RiskDecision | None = None
    fill: Fill | None = None
    if selection.intent is not None:
        account: AccountSnapshot = broker.account(day_start_ms)
        risk = evaluate(selection.intent, state, account, limits, spread_bps)
        if risk.decision == "ALLOW" and not selection.intent.shadow:
            fill = broker.submit(selection.intent, risk.notional)
    else:
        # 没有意图时也要执行市场级门禁：数据陈旧/缺口必须在风控层留痕
        risk = evaluate_market_gate(state, limits)

    equity = broker.equity(as_of_ms, {symbol: mark} if mark is not None else {})
    log = {
        "schema_version": 1,
        "round_ts_ms": as_of_ms,
        "symbol": symbol,
        "state": {
            "trend": state.trend,
            "volatility": state.volatility,
            "momentum": state.momentum,
            "risk": state.risk,
            "confidence": str(state.confidence),
            "reason": state.reason,
            "evidence": [e.to_dict() for e in state.evidence],
            "contradictions": [c.to_dict() for c in state.contradictions],
            "invalidations": list(state.invalidations),
            "data_quality": state.data_quality.to_dict() if state.data_quality else None,
            "features": {k: (str(v) if v is not None else None) for k, v in state.features.items()},
        },
        "selection": selection.to_dict(),
        "risk": risk.to_dict() if risk else None,
        "fill": fill.to_dict() if fill else None,
        "closed": [c.to_dict() for c in closed],
        "equity": str(equity),
        "hold_reason": selection.hold_reason,
        "intel": intel.to_dict() if intel is not None else None,
    }
    if log_path is not None:
        append_jsonl(log_path, log)

    return RoundResult(state, selection, risk, fill, tuple(closed), equity, log)