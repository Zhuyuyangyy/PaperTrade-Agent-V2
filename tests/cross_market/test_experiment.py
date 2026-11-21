"""P1 对齐/标签/成本/A-B-C 测试（零网络）。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.cross_market.cost_model import CostInput, evaluate_cost, funding_drag_bps
from research.cross_market.experiment import (
    FalsificationAdapter,
    Hypothesis,
    arm_signal,
    block_bootstrap_ci,
    hit_rate,
    run_abc,
)
from research.cross_market.feature_engine import align_features
from research.cross_market.label_engine import label_from_store, make_label
from research.cross_market.storage import EventStore


# ---------- 特征对齐 ----------

def _seed_store(p: Path) -> EventStore:
    store = EventStore(p / "e.sqlite")
    base = 1_795_000_000_000
    # poly book：event 1000 / ingest 1200（晚 200ms 到）
    store.append("polymarket", "book", "tok1", base, base + 200,
                 {"market_id": "m", "bid": "0.60", "ask": "0.62", "spread_abs": "0.02", "spread_bps": "328"})
    # binance snapshot：event = ingest（无延迟）
    store.append("binance", "snapshot", "BTCUSDT", base + 60_000, base + 60_000,
                 {"price": "67000", "volume": "1", "oi_snapshot": "100",
                  "cvd_delta": "5", "funding_rate": "0.0001",
                  "liquidation_observed_usdt": "0", "liquidation_coverage": "UNKNOWN"})
    return store


def test_align_respects_availability_gate(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    base = 1_795_000_000_000
    # 决策在 base+100：poly 未到（ingest=base+200）、binance 未发生（event=base+60k）
    # → 两侧都不可用 → 一个点都不该有（可用性门禁全挡）
    pts = align_features(store, "m", ["tok1"], "BTCUSDT", base, base + 120_000,
                         decision_now_ms=base + 100, step_ms=60_000)
    assert len(pts) == 0, "FAIL: LOOK_AHEAD — 决策时刻用了不可用数据"
    store.close()


def test_align_clock_skew_is_conservative(tmp_path: Path) -> None:
    """服务端时钟超前（实测 ~4s）→ 本地决策时刻被可用性门禁保守挡下。"""
    store = EventStore(tmp_path / "e.sqlite")
    base = 1_795_000_000_000
    # poly 服务端时间戳比本地快 4s
    store.append("polymarket", "book", "tok1", base + 4_000, base,
                 {"market_id": "m", "bid": "0.60", "ask": "0.62"})
    # 决策在 base+1000（本地）：服务端时间戳 base+4000 还在'未来' → 挡
    pts = align_features(store, "m", ["tok1"], "BTCUSDT", base, base + 10_000,
                         decision_now_ms=base + 1_000, step_ms=60_000)
    assert len(pts) == 0, "时钟超前的数据不得在本地决策时刻可用"
    # 决策点推进到 base+4000 之后 → 数据在 t=base+4000 才可用（保守延迟 4s）
    pts2 = align_features(store, "m", ["tok1"], "BTCUSDT", base, base + 10_000,
                          decision_now_ms=base + 8_000, step_ms=4_000)
    assert len(pts2) == 2, "t=base 时数据未到，不得被未来到达的数据回填"
    assert pts2[0].decision_ts_ms == base + 4_000
    assert pts2[0].feature.polymarket.get("yes_bid") == Decimal("0.60")
    store.close()


def test_align_after_ingest_includes_poly(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    base = 1_795_000_000_000
    pts = align_features(store, "m", ["tok1"], "BTCUSDT", base, base + 120_000,
                         decision_now_ms=base + 60_000, step_ms=60_000)
    f = pts[0].feature
    assert f.polymarket.get("yes_bid") == Decimal("0.60")
    assert f.binance.get("price") == Decimal("67000")
    store.close()


def test_feature_never_contains_ingest(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    base = 1_795_000_000_000
    pts = align_features(store, "m", ["tok1"], "BTCUSDT", base, base + 120_000,
                         decision_now_ms=base + 60_000, step_ms=60_000)
    for side in (pts[0].feature.polymarket, pts[0].feature.binance):
        assert "ingest_ts_ms" not in side
        assert "ingest" not in side
    store.close()


# ---------- 标签 ----------

def test_label_from_store_future_only(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    base = 1_795_000_000_000
    store.append("binance", "snapshot", "BTCUSDT", base, base,
                 {"price": "67000", "oi_snapshot": "1", "volume": "1", "cvd_delta": "0",
                  "funding_rate": "0", "liquidation_observed_usdt": "0", "liquidation_coverage": "UNKNOWN"})
    store.append("binance", "snapshot", "BTCUSDT", base + 300_000, base + 300_000,
                 {"price": "67500", "oi_snapshot": "1", "volume": "1", "cvd_delta": "0",
                  "funding_rate": "0", "liquidation_observed_usdt": "0", "liquidation_coverage": "UNKNOWN"})
    lab = label_from_store(store, "BTCUSDT", base, 300_000)
    assert lab is not None
    expected = (Decimal("67500") - Decimal("67000")) / Decimal("67000")
    assert abs(lab.future_return - expected) < Decimal("1e-18"), "标签必须是相对收益，不是价格"
    # 起点必须等于 origin
    assert lab.origin_ts_ms == base
    store.close()


# ---------- 成本模型 ----------

def test_cost_model_net() -> None:
    v = evaluate_cost(CostInput(
        ev_gross_bps=Decimal("30"), taker_fee_bps=Decimal("2.5"), spread_bps=Decimal("5"),
        slippage_bps=Decimal("1"), latency_cost_bps=Decimal("0.5"), impact_bps=Decimal("2"),
        horizon_ms=300_000,
    ))
    # 30 − (5+5+1+0.5+2) = 16.5
    assert v.ev_net_bps == Decimal("16.5")
    assert v.economic is True


def test_cost_model_negative_blocks() -> None:
    v = evaluate_cost(CostInput(
        ev_gross_bps=Decimal("5"), taker_fee_bps=Decimal("2.5"), spread_bps=Decimal("5"),
        slippage_bps=Decimal("1"), latency_cost_bps=Decimal("0.5"), impact_bps=Decimal("2"),
        horizon_ms=300_000,
    ))
    assert v.ev_net_bps < 0 and v.economic is False


def test_funding_drag() -> None:
    # 0.01%/8h × (1h/8h) = 0.00125% = 0.125 bps
    assert funding_drag_bps(Decimal("0.0001"), 3_600_000) == Decimal("0.125")


# ---------- A/B/C ----------

def _h() -> Hypothesis:
    return Hypothesis(hypothesis_id="h1", market_key="m", underlying="BTC", primary_horizon_ms=300_000)


def test_arm_signals_abstain_when_missing() -> None:
    h = _h()
    assert arm_signal("A", {"binance": {}}, h) is None
    assert arm_signal("B", {"polymarket": {}}, h) is None


def test_hit_rate_skips_abstain() -> None:
    hit, n = hit_rate([Decimal("1"), None, Decimal("-1")], [Decimal("5"), Decimal("5"), Decimal("-3")])
    assert n == 2 and hit == 1.0


def test_block_bootstrap_detects_positive_increment() -> None:
    x = [1.0] * 50          # C 全对
    y = [0.6] * 50          # A 六成
    diff, lo, hi = block_bootstrap_ci(x, y, block=10, iters=200)
    assert diff > 0 and lo > 0


def test_abc_supported_when_c_beats_a() -> None:
    h = _h()
    # A 永远看涨；真值交替；B 只在看涨点同向 → C 在跌点弃权。
    # 正确实现：C 的 payoff 明显高于 A（躲开了 A 在跌点的全部亏损）。
    # 300 个 1 分钟点、5 分钟视界 → 块长 5，有效独立共同点 150/5 = 30，刚好过门槛。
    points = []
    labels: list[Decimal | None] = []
    for i in range(300):
        up = i % 2 == 0
        points.append({
            "decision_ts_ms": 1_795_000_000_000 + i * 60_000,
            "binance": {"price": "67000", "price_delta_bps": Decimal("30"), "oi_snapshot": "1"},
            "polymarket": {"yes_mid": Decimal("0.60"),
                           "yes_mid_delta_bps": Decimal("30") if up else Decimal("-30")},
        })
        labels.append(Decimal("1") if up else Decimal("-1"))
    v = run_abc(points, labels, h)
    assert v.verdict == "SUPPORTED", v.reason
    assert v.increment_ci is not None and v.increment_ci[1] > 0
    assert v.block == 5
    assert v.aligned_n == 150
    assert v.effective_aligned_n == 30
    assert v.non_overlap_increment_ci is not None
    ev = FalsificationAdapter.to_evidence(v)
    assert ev["kind"] == "cross_market_abc"
    assert "A" in ev["arms"] and "C" in ev["arms"]
    assert ev["aligned_n"] == 150
    assert ev["block"] == 5


def test_abc_inconclusive_without_samples() -> None:
    v = run_abc([], [], _h())
    assert v.verdict == "INCONCLUSIVE"
