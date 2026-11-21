"""策略注册与选择（Sprint 2）。

规则：
  - 先按适用状态过滤，再调用策略函数；
  - ``FALSIFIED`` 排除；``UNTESTED`` / ``SHADOW`` 只能生成影子意图；``VERIFIED`` 才进账本；
  - 多个候选按预注册优先级（数字小优先）选择，不按"最近表现"择优；
  - 全部不可用 → HOLD，并给出可读原因（数据问题优先于"策略不适用"）。
"""
from __future__ import annotations

from decimal import Decimal

from research.market.contracts import MarketState

from .contracts import Candidate, Selection, StrategySpec, TradeIntent, make_intent_id
from .strategies import FUNCTIONS

DEFAULT_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("trend_follow", "v1", "趋势跟随", ("TREND_UP", "TREND_DOWN"), (),
                 ("LONG", "SHORT"), verification="UNTESTED", priority=10),
    StrategySpec("breakout_confirm", "v1", "突破确认", ("RANGE", "TREND_UP", "TREND_DOWN"), (),
                 ("LONG", "SHORT"), verification="UNTESTED", priority=20),
    StrategySpec("range_revert", "v1", "区间回归", ("RANGE",), (),
                 ("LONG", "SHORT"), verification="UNTESTED", priority=30),
)

DATA_HOLD_CODES = ("CRITICAL_STALE", "NON_CONTIGUOUS", "UNKNOWN_STATE")


def _hold_reason(state: MarketState, candidates: list[Candidate]) -> str:
    dq = state.data_quality
    if dq is not None and dq.critical_stale:
        return f"HOLD: 1h 数据陈旧（age={dq.per_timeframe.get('1h', {}).get('age_ms')}ms），趋势不可信"
    if dq is not None and not dq.contiguous:
        return "HOLD: 1h 数据存在缺口，趋势周期不可用"
    if state.trend == "UNKNOWN":
        return f"HOLD: {state.reason}"
    reasons = ", ".join(f"{c.strategy_id}={c.reason}" for c in candidates if not c.eligible)
    return f"HOLD: 无策略适用（trend={state.trend}, vol={state.volatility}）；{reasons}"


def select(state: MarketState, specs: tuple[StrategySpec, ...] = DEFAULT_SPECS) -> Selection:
    candidates: list[Candidate] = []
    best: TradeIntent | None = None

    for spec in sorted(specs, key=lambda s: (s.priority, s.strategy_id)):
        if spec.verification == "FALSIFIED":
            candidates.append(Candidate(spec.strategy_id, spec.version, False, "STRATEGY_FALSIFIED"))
            continue
        if not spec.applicable(state.trend, state.volatility):
            candidates.append(Candidate(
                spec.strategy_id, spec.version, False,
                f"STATE_NOT_APPLICABLE:{state.trend}/{state.volatility}"))
            continue
        fn = FUNCTIONS.get(spec.strategy_id)
        if fn is None:
            candidates.append(Candidate(spec.strategy_id, spec.version, False, "NO_IMPLEMENTATION"))
            continue
        signal, code = fn(state)
        if signal is None:
            candidates.append(Candidate(spec.strategy_id, spec.version, False, code))
            continue
        if signal.side not in spec.sides:
            candidates.append(Candidate(spec.strategy_id, spec.version, False, "SIDE_NOT_ALLOWED"))
            continue
        close = state.features.get("close")
        if not isinstance(close, Decimal) or close <= 0:
            candidates.append(Candidate(spec.strategy_id, spec.version, False, "INSUFFICIENT_FEATURES"))
            continue

        shadow = spec.verification != "VERIFIED"
        intent = TradeIntent(
            intent_id=make_intent_id(state.symbol, spec.strategy_id, spec.version,
                                     state.as_of_ms, signal.side),
            decision_ts_ms=state.as_of_ms,
            symbol=state.symbol,
            side=signal.side,
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            entry_ref_price=close,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            expected_holding_ms=spec.max_holding_ms,
            confidence=state.confidence,
            reason=signal.reason,
            evidence_codes=signal.evidence_codes,
            invalidations=state.invalidations,
            shadow=shadow,
            market_state_trend=state.trend,
            market_state_vol=state.volatility,
            market_state_risk=state.risk,
        )
        candidates.append(Candidate(spec.strategy_id, spec.version, True, code, intent))
        if best is None:
            best = intent

    if best is None:
        return Selection("HOLD", None, tuple(candidates), _hold_reason(state, candidates), shadow=True)
    return Selection(best.side, best, tuple(candidates), shadow=best.shadow)