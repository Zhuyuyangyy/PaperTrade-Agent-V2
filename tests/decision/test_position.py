"""V3 持仓管理契约测试：结构校验、风险底线、规则对照组、持仓快照。"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from research.decision.position import (
    ACTIONS,
    PositionDecision,
    PositionDecisionError,
    PositionSnapshot,
    rules_position_action,
    snapshot_from_row,
    validate_against_position,
)


def snap(side="SHORT", entry="59100", mark="59000", stop="60000",
         unrealized="1", qty="0.0016") -> PositionSnapshot:
    return PositionSnapshot(
        as_of_ms=1_795_000_000_000, symbol="BTC/USDT", side=side, qty=D(qty),
        entry_price=D(entry), mark_price=D(mark), notional=D(qty) * D(mark),
        unrealized_net=D(unrealized), stop_price=D(stop), tp_price=D("57000"),
        data_as_of_ms=1_795_000_000_000,
    )


def dec(action="HOLD", **kw) -> PositionDecision:
    base = dict(provider="test", version="v1", action=action,
                rationale="test reason", data_as_of_ms=1_795_000_000_000)
    base.update(kw)
    return PositionDecision(**base)


def test_actions_are_bounded() -> None:
    assert "ADD_TO_WINNER" in ACTIONS and "MARTINGALE" not in ACTIONS
    with pytest.raises(PositionDecisionError, match="action 非法"):
        dec("DOUBLE_DOWN")


def test_missing_reason_or_timestamp_rejected() -> None:
    with pytest.raises(PositionDecisionError, match="rationale"):
        dec(rationale="")
    with pytest.raises(PositionDecisionError, match="data_as_of_ms"):
        dec(data_as_of_ms=0)


def test_partial_fraction_must_be_between_zero_and_one() -> None:
    for bad in (D("0"), D("1"), D("1.2"), D("-0.1")):
        with pytest.raises(PositionDecisionError, match="fraction"):
            dec("CLOSE_PARTIAL", fraction=bad)
    assert dec("CLOSE_PARTIAL", fraction=D("0.5")).action == "CLOSE_PARTIAL"


def test_move_stop_requires_target_and_invalid_if() -> None:
    with pytest.raises(PositionDecisionError, match="new_stop"):
        dec("MOVE_STOP")
    with pytest.raises(PositionDecisionError, match="invalid_if"):
        dec("MOVE_STOP", new_stop=D("59500"))


def test_update_tp_requires_target() -> None:
    with pytest.raises(PositionDecisionError, match="new_tp"):
        dec("UPDATE_TP")


def test_add_requires_notional() -> None:
    with pytest.raises(PositionDecisionError, match="notional"):
        dec("ADD_TO_WINNER")


def test_stop_can_only_tighten() -> None:
    s = snap(side="SHORT", mark="59000", stop="60000")
    # 空头止损下移 = 收紧
    validate_against_position(dec("MOVE_STOP", new_stop=D("59800"),
                                  invalid_if=("趋势反转",)), s)
    # 空头止损上移 = 放宽，必须拒绝
    with pytest.raises(PositionDecisionError, match="只能下移"):
        validate_against_position(dec("MOVE_STOP", new_stop=D("60100"),
                                      invalid_if=("趋势反转",)), s)


def test_long_stop_can_only_move_up() -> None:
    s = snap(side="LONG", entry="59000", mark="59200", stop="58000")
    validate_against_position(dec("MOVE_STOP", new_stop=D("58400"),
                                  invalid_if=("趋势反转",)), s)
    with pytest.raises(PositionDecisionError, match="只能上移"):
        validate_against_position(dec("MOVE_STOP", new_stop=D("57800"),
                                      invalid_if=("趋势反转",)), s)


def test_no_add_to_loser() -> None:
    s = snap(unrealized="-5")
    with pytest.raises(PositionDecisionError, match="浮盈"):
        validate_against_position(dec("ADD_TO_WINNER", notional=D("50")), s)
    validate_against_position(dec("ADD_TO_WINNER", notional=D("50")),
                              snap(unrealized="5"))


def test_tp_must_be_on_correct_side() -> None:
    s = snap(side="SHORT", mark="59000")
    validate_against_position(dec("UPDATE_TP", new_tp=D("57000")), s)
    with pytest.raises(PositionDecisionError, match="空头止盈"):
        validate_against_position(dec("UPDATE_TP", new_tp=D("59500")), s)


def test_rules_manager_holds_on_small_drawdown() -> None:
    """规则组不该因为少量浮亏就平仓——那是被修掉的旧行为。"""
    s = snap(unrealized="-2")
    d = rules_position_action(s)
    assert d.action == "HOLD"


def test_rules_manager_takes_partial_at_one_r() -> None:
    # SHORT: entry 59100, stop 60000 → R=900；mark=58000 → +1.22R
    s = snap(entry="59100", mark="58000", stop="60000", unrealized="17")
    d = rules_position_action(s)
    assert d.action == "CLOSE_PARTIAL" and d.fraction == D("0.5")
    validate_against_position(d, s)


def test_rules_manager_trails_stop_after_partial() -> None:
    # +0.5R，未到 1R 减仓线，但可移动止损
    s = snap(entry="59100", mark="58650", stop="60000", unrealized="7")
    d = rules_position_action(s)
    assert d.action == "MOVE_STOP"
    validate_against_position(d, s)


def test_snapshot_uses_actual_entry_and_fees() -> None:
    row = {"qty": "0.0016", "entry_price": "59143.7", "symbol": "BTC/USDT",
           "side": "SHORT", "entry_ts": 1_795_000_000_000 - 600_000,
           "stop_price": "60009.1", "tp_price": "57369.1", "reason": "trend"}
    s = snapshot_from_row(row=row, mark=D("59100"), as_of_ms=1_795_000_000_000,
                          fee_bps=D("4"))
    assert s.entry_price == D("59143.7")
    assert s.holding_ms == 600_000
    assert s.est_fees_roundtrip == D("0.0016") * D("59100") * D("8") / D("10000")
    assert s.entry_reason == "trend"
    assert s.summary()["side"] == "SHORT"


def test_ai_provider_rejects_bad_action_and_keeps_hold(monkeypatch) -> None:
    from research.decision.providers.position_ai import DeepSeekPositionManager
    from research.intel.news_llm import LLMConfig

    def fetch(url, key, body, timeout):
        return {"choices": [{"message": {"content": '{"action":"DOUBLE_DOWN","rationale":"x"}'}}]}

    mgr = DeepSeekPositionManager(LLMConfig(api_key="k"), fetch=fetch)
    d = mgr.decide(snap())
    assert d.action == "HOLD" and d.status == "ERROR"
    assert "position_error" in d.rationale


def test_ai_provider_parses_valid_action() -> None:
    from research.decision.providers.position_ai import DeepSeekPositionManager
    from research.intel.news_llm import LLMConfig

    def fetch(url, key, body, timeout):
        return {"choices": [{"message": {"content": json.dumps({
            "action": "MOVE_STOP", "confidence": 0.8, "rationale": "protect profit",
            "new_stop": 59800, "invalid_if": ["trend reversal"],
            "evidence": ["1h trend down"],
        })}}]}

    import json
    mgr = DeepSeekPositionManager(LLMConfig(api_key="k"), fetch=fetch)
    d = mgr.decide(snap())
    assert d.action == "MOVE_STOP" and d.new_stop == D("59800")
    assert d.evidence == ("1h trend down",)
