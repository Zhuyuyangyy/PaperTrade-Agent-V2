"""P0 安全测试：无前视偏差（规格 §17 的构造用例 + 2026-11-18 审查加固）。

两层防御：
  1. FeatureSnapshot(t) 绝不能包含未来数据（字段级）；
  2. **可用性门禁**：event_ts=1000 但 ingest_ts=5000 的事件，
     在 decision=2000 时刻不可用（数据还没到）——延迟到达 = 隐蔽前视。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from research.cross_market.replay import ReplayEngine, usable_at
from research.cross_market.schemas import (
    FeatureSnapshot,
    LookaheadBiasError,
    ResearchLabel,
    assert_no_lookahead,
)
from research.cross_market.storage import EventStore


def _feature_at_t1000() -> FeatureSnapshot:
    """t=1000 时已知的全部信息。"""
    return FeatureSnapshot(
        feature_ts_ms=1000,
        polymarket={"probability": Decimal("0.62"), "spread_bps": Decimal("20")},
        binance={"price": Decimal("2000")},
    )


def test_feature_snapshot_excludes_future() -> None:
    f = _feature_at_t1000()
    # 禁词检查：特征里禁止出现任何未来字段
    forbidden = ("2030", "future_return", "future_volatility", "future_liquidation", "future_price")
    for side in (f.polymarket, f.binance):
        for k in side:
            assert k not in forbidden, f"FAIL: LOOK_AHEAD_BIAS — 特征含未来字段 {k}"
    assert f.binance["price"] == Decimal("2000")


def test_label_origin_before_feature_raises() -> None:
    f = _feature_at_t1000()
    bad = ResearchLabel(origin_ts_ms=999, horizon="5m", future_return=Decimal("0.015"))
    with pytest.raises(LookaheadBiasError, match="LOOK_AHEAD_BIAS"):
        assert_no_lookahead(f, bad)


def test_label_origin_after_feature_passes() -> None:
    f = _feature_at_t1000()
    ok = ResearchLabel(origin_ts_ms=1000, horizon="5m", future_return=Decimal("0.015"))
    assert_no_lookahead(f, ok)  # 不抛异常


def test_label_from_future_window_only() -> None:
    """标签必须来自 t+H：用 2030（t+5m）生成标签合法，但它只能待在 Label 里。"""
    label = ResearchLabel(origin_ts_ms=1000, horizon="5m", future_return=Decimal("0.015"))
    assert label.future_return == Decimal("0.015")
    # 而特征里永远只有 2000
    assert _feature_at_t1000().binance["price"] == Decimal("2000")


# ---------- 可用性门禁（审查 P0：延迟到达 = 隐蔽前视） ----------

def test_delayed_arrival_is_not_available_at_decision_time(tmp_path) -> None:
    """审查用例：event_ts=1000 / ingest_ts=5000 / decision=2000 → 2000 时刻不可用。"""
    store = EventStore(tmp_path / "events.sqlite")
    store.append("binance", "snapshot", "ETHUSDT",
                 event_ts_ms=1000, ingest_ts_ms=5000, payload={"price": "2000"})
    engine = ReplayEngine(store)
    state = engine.run(0, 6000, decision_now_ms=2000)
    # 2000 时刻决策：该行情尚未到达 → 特征里必须没有它
    assert state.seen_events == 0
    assert state.skipped_not_yet_available == 1
    f = engine.feature_at("t1", "ETHUSDT")
    assert f["binance"] is None, "FAIL: LOOK_AHEAD_BIAS — 用了尚未收到的行情"
    store.close()


def test_delayed_arrival_available_after_ingest(tmp_path) -> None:
    """同一条事件，decision=5000（已到达）→ 可用。"""
    store = EventStore(tmp_path / "events.sqlite")
    store.append("binance", "snapshot", "ETHUSDT",
                 event_ts_ms=1000, ingest_ts_ms=5000, payload={"price": "2000"})
    engine = ReplayEngine(store)
    state = engine.run(0, 6000, decision_now_ms=5000)
    assert state.seen_events == 1
    assert state.skipped_not_yet_available == 0
    assert engine.feature_at("t1", "ETHUSDT")["binance"]["price"] == "2000"
    store.close()


def test_usable_at_is_max_of_event_and_ingest() -> None:
    assert usable_at(1000, 5000) == 5000
    assert usable_at(5000, 1000) == 5000
    assert usable_at(1000, 1000) == 1000


def test_no_lookahead_window_boundary(tmp_path) -> None:
    """窗口边界：恰好在 decision 时刻可用的事件可用；晚 1ms 不可用。"""
    store = EventStore(tmp_path / "events.sqlite")
    store.append("binance", "snapshot", "ETHUSDT",
                 event_ts_ms=1000, ingest_ts_ms=2000, payload={"price": "2000"})
    store.append("binance", "snapshot", "ETHUSDT",
                 event_ts_ms=1000, ingest_ts_ms=2001, payload={"price": "2001"})
    engine = ReplayEngine(store)
    state = engine.run(0, 3000, decision_now_ms=2000)
    assert state.seen_events == 1
    assert state.skipped_not_yet_available == 1
    store.close()


def test_replay_rejects_out_of_order_availability(tmp_path) -> None:
    """按可用时间乱序直喂 → 拒绝。"""
    store = EventStore(tmp_path / "events.sqlite")
    engine = ReplayEngine(store)
    engine._apply({"source": "binance", "event_type": "snapshot", "key": "ETHUSDT",
                   "event_ts_ms": 1000, "ingest_ts_ms": 5000, "payload": {}}, usable_at(1000, 5000))
    with pytest.raises(ValueError, match="乱序事件"):
        engine._apply({"source": "binance", "event_type": "snapshot", "key": "ETHUSDT",
                       "event_ts_ms": 2000, "ingest_ts_ms": 2000, "payload": {}}, usable_at(2000, 2000))
    store.close()
