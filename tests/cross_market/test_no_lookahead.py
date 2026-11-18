"""P0 安全测试：无前视偏差（规格 §17 的构造用例）。

构造 t=1000 的数据：Polymarket=0.62, Binance=2000；t+5m Binance=2030。
FeatureSnapshot(t) 绝不能包含 2030 / future_return / future_volatility / future_liquidation。
任何一个出现 → FAIL: LOOK_AHEAD_BIAS。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from research.cross_market.schemas import (
    FeatureSnapshot,
    LookaheadBiasError,
    ResearchLabel,
    assert_no_lookahead,
)


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
