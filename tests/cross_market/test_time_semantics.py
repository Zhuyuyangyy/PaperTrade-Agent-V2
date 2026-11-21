"""时间语义与统计依赖的反例测试（Sprint 0 复审，Round 2）。

每一项对应复审提出的一个缺口。规则同前：断言必须先在当前实现上失败。

  R2-01 迟到旧行情不能当未来标签（event 时间必须晚于 origin+H）
  R2-02 回放窗口必须在 end 时刻的可用性处截止
  R2-03 point-in-time 状态优先使用事件时间更新的数据，迟到旧数据不得覆盖
  R2-04 过期/规则不明的市场不可研究（fail-closed）
  R2-05 重叠采样下 bootstrap 块必须随视界变长，样本量按独立窗口折算
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.cross_market.experiment import Hypothesis, run_abc
from research.cross_market.feature_engine import align_features
from research.cross_market.label_engine import label_from_store
from research.cross_market.market_mapping import is_researchable, load_mapping, researchable_markets
from research.cross_market.replay import ReplayEngine
from research.cross_market.storage import EventStore

BASE = 1_795_000_000_000


def _snap(store: EventStore, event_ts: int, ingest_ts: int, price: str) -> None:
    store.append("binance", "snapshot", "BTCUSDT", event_ts, ingest_ts,
                 {"price": price, "oi_snapshot": "100", "volume": "1", "cvd_delta": "0",
                  "funding_rate": "0", "liquidation_observed_usdt": "0",
                  "liquidation_coverage": "UNKNOWN"})


# ---------- R2-01 标签：迟到旧价不能当未来收益 ----------

def test_late_old_snapshot_is_not_a_future_label(tmp_path: Path) -> None:
    """12:01 的报价 12:06 才到：它仍然发生在 12:05 之前，不能当 12:05 之后的价格。"""
    store = EventStore(tmp_path / "e.sqlite")
    _snap(store, BASE, BASE, "100")
    # 事件发生在 origin 之前 10 秒，却在 horizon 之后才到达
    _snap(store, BASE - 10_000, BASE + 330_000, "90")
    assert label_from_store(store, "BTCUSDT", BASE, 300_000) is None
    store.close()


def test_future_label_requires_both_event_and_availability_after_horizon(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _snap(store, BASE, BASE, "100")
    _snap(store, BASE + 300_000, BASE + 300_000, "110")          # 合法：事后发生、事后可用
    _snap(store, BASE + 290_000, BASE + 300_000, "105")          # 合法但更早，不应被选
    lab = label_from_store(store, "BTCUSDT", BASE, 300_000)
    assert lab is not None and lab.origin_price == Decimal("100")
    assert lab.future_return == (Decimal("110") - Decimal("100")) / Decimal("100")
    store.close()


# ---------- R2-02 回放：窗口结束时刻的可用性截止 ----------

def test_replay_does_not_apply_events_available_after_window_end(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _snap(store, 1000, 5000, "1")   # 发生在窗口内，窗口结束后才到达
    state = ReplayEngine(store).run(0, 2000)
    assert state.seen_events == 0, "FAIL: 回放用了 end 之后才到达的数据"
    assert state.skipped_not_yet_available == 1
    store.close()


def test_replay_window_end_is_inclusive_for_availability(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _snap(store, 1000, 2000, "1")   # 恰好在 end 时刻可用
    state = ReplayEngine(store).run(0, 2000)
    assert state.seen_events == 1
    store.close()


# ---------- R2-03 PIT：迟到旧行情不得覆盖更新行情 ----------

def _three_snaps(store: EventStore) -> None:
    _snap(store, BASE, BASE, "100")
    _snap(store, BASE + 60_000, BASE + 60_000, "200")
    # 事件时间夹在中间、但最后到达
    _snap(store, BASE + 30_000, BASE + 120_000, "150")


def test_pit_state_prefers_newer_event_over_late_arrival(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _three_snaps(store)
    pts = align_features(store, "m", ["YES"], "BTCUSDT",
                         BASE + 120_000, BASE + 180_000, step_ms=60_000, lookback_ms=60_000)
    assert pts and pts[0].feature.binance["price"] == Decimal("200")
    store.close()


def test_replay_state_keeps_the_newer_event(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _three_snaps(store)
    engine = ReplayEngine(store)
    engine.run(0, BASE + 180_000)
    assert engine.feature_at("x", "BTCUSDT")["binance"]["price"] == "200"
    store.close()


# ---------- R2-04 市场可研究性 fail-closed ----------

def _mapping_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "m.yaml"
    p.write_text(
        "live_market:\n"
        "  underlying_asset: BTC\n  event_threshold: 100000\n  event_comparator: ABOVE\n"
        "  event_expiry: '2099-01-01T00:00:00Z'\n  resolution_source: uma\n"
        "  target_horizons: [5m]\n  primary_horizon: 5m\n"
        "dead_market:\n"
        "  underlying_asset: BTC\n  event_threshold: 50000\n  event_comparator: BELOW\n"
        "  event_expiry: '2020-01-01T00:00:00Z'\n  resolution_source: uma\n"
        "  target_horizons: [5m]\n  primary_horizon: 5m\n"
        "unknown_expiry:\n"
        "  underlying_asset: BTC\n  event_threshold: 60000\n  event_comparator: BELOW\n"
        "  event_expiry: '不是日期'\n  resolution_source: uma\n"
        "  target_horizons: [5m]\n  primary_horizon: 5m\n",
        encoding="utf-8",
    )
    return p


def test_expired_market_is_not_researchable(tmp_path: Path) -> None:
    m = load_mapping(_mapping_yaml(tmp_path))
    assert m["dead_market"].expired is True
    assert is_researchable("dead_market", m) is False
    assert "dead_market" not in researchable_markets(m)
    assert is_researchable("live_market", m) is True


def test_unparseable_expiry_is_fail_closed(tmp_path: Path) -> None:
    m = load_mapping(_mapping_yaml(tmp_path))
    assert m["unknown_expiry"].expiry_unknown is True
    assert is_researchable("unknown_expiry", m) is False
    assert "unknown_expiry" not in researchable_markets(m)


def test_unregistered_market_stays_unresearchable(tmp_path: Path) -> None:
    m = load_mapping(_mapping_yaml(tmp_path))
    assert is_researchable("nope", m) is False


# ---------- R2-05 重叠采样与 bootstrap 块 ----------

def _overlapping_points(n: int, step_ms: int = 5_000) -> list[dict]:
    points = []
    for i in range(n):
        up = i % 2 == 0
        a_dir = Decimal("1")
        points.append({
            "decision_ts_ms": BASE + i * step_ms,
            "binance": {"price": "67000", "price_delta_bps": a_dir * Decimal("30"),
                        "oi_snapshot": "100"},
            "polymarket": {"yes_mid": Decimal("0.60"),
                           "yes_mid_delta_bps": (a_dir * Decimal("30")) if up else Decimal("0")},
        })
    return points


def test_abc_block_scales_with_horizon_over_step() -> None:
    """5 分钟视界、5 秒采样 → 一个独立窗口覆盖 60 个点，bootstrap 块也应是 60。"""
    h = Hypothesis(hypothesis_id="h", market_key="m", underlying="BTC",
                   primary_horizon_ms=300_000)
    labels = [Decimal("1") if i % 2 == 0 else Decimal("-1") for i in range(300)]
    v = run_abc(_overlapping_points(300), labels, h)
    assert v.step_ms == 5_000
    assert v.block == 60
    assert v.aligned_n == 150
    assert v.effective_aligned_n == 2          # 150 / 60
    assert v.verdict == "INCONCLUSIVE", "重叠样本不能靠点数过门槛"


def test_abc_disjoint_windows_still_supported() -> None:
    """视界等于采样间隔（k=1）时，门槛不应被虚假抬高。"""
    h = Hypothesis(hypothesis_id="h", market_key="m", underlying="BTC",
                   primary_horizon_ms=60_000)
    points, labels = [], []
    for i in range(300):
        up = i % 2 == 0
        points.append({
            "decision_ts_ms": BASE + i * 60_000,
            "binance": {"price": "67000", "price_delta_bps": Decimal("30"),
                        "oi_snapshot": "100"},
            "polymarket": {"yes_mid": Decimal("0.60"),
                           "yes_mid_delta_bps": Decimal("30") if up else Decimal("0")},
        })
        labels.append(Decimal("1") if up else Decimal("-1"))
    v = run_abc(points, labels, h)
    assert v.block == 1
    assert v.effective_aligned_n == v.aligned_n
    assert v.verdict == "SUPPORTED", v.reason