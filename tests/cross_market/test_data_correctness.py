"""Sprint 0 数据正确性回归测试（先证伪，再修复）。

每一项都对应审计报告里的一个 S0 缺陷。规则：这些断言必须在修复前的
实现上失败；修复后通过。禁止为了让测试通过而放宽断言。

覆盖：
  S0-01 标签语义：future_return 必须是相对收益，不是价格
  S0-02 标签窗口：未来价格必须落在 origin+H 之后
  S0-03 A/C 比较：必须在共同决策时点对齐，不是按列表索引截断
  S0-04 方向信号：必须用变化量，不是"价格>0 / 概率>0.5"
  S0-05 事件方向映射：BELOW 市场的概率上升 = 标的看跌
  S0-06 point-in-time：每个决策点只用该时刻可用的数据
  S0-07 Polymarket YES/NO 归一化：NO token 的价格不是 YES 概率
  S0-08 盘口有序性：best bid = max(bids)，best ask = min(asks)
  S0-09 缺失服务端时间戳：禁止用本地接收时间冒充 event_ts
  S0-10 重复采集：同一 book hash 不重复入库
  S0-11 可用性排序：回放按 max(event_ts, ingest_ts) 排序，不按 event_ts
  S0-12 CVD：只用已收盘 K 线
  S0-13 成本模型：资金费必须计入 EV_net
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.cross_market.collectors.binance_microstructure import BinanceMicroCollector
from research.cross_market.collectors.polymarket_rest import PolyRestCollector
from research.cross_market.cost_model import CostInput, evaluate_cost
from research.cross_market.experiment import Hypothesis, arm_signal, run_abc
from research.cross_market.feature_engine import align_features
from research.cross_market.label_engine import label_from_store, make_label
from research.cross_market.replay import ReplayEngine
from research.cross_market.storage import EventStore

BASE = 1_795_000_000_000


# ---------- S0-01 / S0-02 标签 ----------

def test_label_is_relative_return_not_price() -> None:
    lab = make_label(BASE, "5m", origin_price=Decimal("67000"), future_price=Decimal("67500"))
    expected = (Decimal("67500") - Decimal("67000")) / Decimal("67000")
    assert abs(lab.future_return - expected) < Decimal("1e-18")
    assert lab.future_return != Decimal("67500"), "FAIL: 把价格当成了收益"


def test_label_from_store_computes_forward_return(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    store.append("binance", "snapshot", "BTCUSDT", BASE, BASE, {"price": "67000"})
    store.append("binance", "snapshot", "BTCUSDT", BASE + 300_000, BASE + 300_000, {"price": "67500"})
    lab = label_from_store(store, "BTCUSDT", BASE, 300_000)
    assert lab is not None
    assert abs(lab.future_return - (Decimal("67500") - Decimal("67000")) / Decimal("67000")) < Decimal("1e-18")
    assert lab.origin_ts_ms == BASE
    assert lab.origin_price == Decimal("67000")
    store.close()


def test_label_requires_future_beyond_horizon(tmp_path: Path) -> None:
    """t+H 之前的价格不能当标签（否则等于偷看 horizon 内的未来）。"""
    store = EventStore(tmp_path / "e.sqlite")
    store.append("binance", "snapshot", "BTCUSDT", BASE, BASE, {"price": "67000"})
    store.append("binance", "snapshot", "BTCUSDT", BASE + 60_000, BASE + 60_000, {"price": "99000"})
    assert label_from_store(store, "BTCUSDT", BASE, 300_000) is None
    store.close()


# ---------- S0-03 A/C 共同决策点 ----------

def _h(**kw) -> Hypothesis:
    return Hypothesis(hypothesis_id="h", market_key="m", underlying="BTC",
                      primary_horizon_ms=300_000, **kw)


def test_abc_compares_only_common_decision_points() -> None:
    """A 在 60 个点出手、C 只在其中 30 个点出手，且两者在这 30 点上表现相同。

    正确实现：共同点 C−A = 0（aligned_n=30）。
    错误实现（按列表索引截断）：拿 C 的 30 个命中点去比 A 的前 30 个点（含 15 个
    未命中），会伪造出 +0.5 的"增量"。
    """
    points = []
    labels = []
    for i in range(60):
        up = (i % 2 == 0)                       # 偶数点：真实方向向上
        a_dir = Decimal("1") if up else Decimal("-1")
        if up:
            labels.append(a_dir)                # 偶数点 A 看对
        else:
            # 奇数点 A 看错一半、看对一半（防止"前 30 个点"截断值恰好相同）
            labels.append(Decimal("-1") if i % 4 == 3 else Decimal("1"))
        # C 只在偶数点出手；奇数点 B 动量为 0 → C 弃权
        b_delta = a_dir * Decimal("30") if up else Decimal("0")
        points.append({
            "decision_ts_ms": BASE + i * 60_000,
            "binance": {"price": "67000", "price_delta_bps": a_dir * Decimal("30"),
                        "oi_snapshot": "100"},
            "polymarket": {"yes_mid": Decimal("0.60"), "yes_mid_delta_bps": b_delta},
        })
    v = run_abc(points, labels, _h())
    assert v.aligned_n == 30, f"FAIL: 共同决策点应为 30，实得 {v.aligned_n}"
    assert v.common_hit_ci is not None
    assert v.common_hit_ci[0] == 0.0, f"FAIL: 共同点命中率差应为 0，实得 {v.common_hit_ci[0]}"
    assert v.increment_ci is not None
    assert v.increment_ci[0] == 0.0, f"FAIL: 共同样本上 C−A payoff 应为 0，实得 {v.increment_ci[0]}"
    assert v.verdict == "INCONCLUSIVE"


# ---------- S0-04 / S0-05 方向信号 ----------

def test_arm_a_uses_price_change_not_level() -> None:
    f = {"binance": {"price": "67000", "price_delta_bps": Decimal("-30"), "oi_snapshot": "100"}}
    assert arm_signal("A", f, _h()) == Decimal("-1")


def test_arm_a_abstains_inside_deadband() -> None:
    f = {"binance": {"price": "67000", "price_delta_bps": Decimal("0.5"), "oi_snapshot": "100"}}
    assert arm_signal("A", f, _h()) is None


def test_arm_b_abstains_when_books_inconsistent() -> None:
    f = {"polymarket": {"yes_mid": Decimal("0.70"), "yes_mid_delta_bps": Decimal("40"),
                        "prob_usable": False}}
    assert arm_signal("B", f, _h()) is None


def test_arm_b_maps_probability_change_to_underlying_direction() -> None:
    f = {"polymarket": {"yes_mid": Decimal("0.70"), "yes_mid_delta_bps": Decimal("-40")}}
    # ABOVE 市场：概率下跌 = 标的看跌
    assert arm_signal("B", f, _h(event_comparator="ABOVE")) == Decimal("-1")
    # BELOW 市场：概率下跌 = 标的看涨
    assert arm_signal("B", f, _h(event_comparator="BELOW")) == Decimal("1")


# ---------- S0-06 point-in-time ----------

def _snap(store: EventStore, ts: int, price: str) -> None:
    store.append("binance", "snapshot", "BTCUSDT", ts, ts,
                 {"price": price, "oi_snapshot": "100", "volume": "1", "cvd_delta": "0",
                  "funding_rate": "0", "liquidation_observed_usdt": "0",
                  "liquidation_coverage": "UNKNOWN"})


def _book(store: EventStore, token: str, ts: int, bid: str, ask: str) -> None:
    store.append("polymarket", "book", token, ts, ts,
                 {"market_id": "m", "bid": bid, "ask": ask, "token_role": "YES",
                  "spread_abs": str(Decimal(ask) - Decimal(bid))})


def test_align_each_point_uses_data_available_then(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _snap(store, BASE, "100")
    _snap(store, BASE + 60_000, "200")
    _snap(store, BASE + 120_000, "300")
    pts = align_features(store, "m", ["YES", "NO"], "BTCUSDT", BASE, BASE + 180_000,
                         step_ms=60_000, decision_now_ms=BASE + 120_000)
    prices = [p.feature.binance["price"] for p in pts]
    assert prices == [Decimal("100"), Decimal("200"), Decimal("300")], (
        f"FAIL: 每个决策点必须用当时可用的数据，实得 {prices}"
    )
    store.close()


def test_align_stops_at_realtime_ceiling(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _snap(store, BASE, "100")
    _snap(store, BASE + 60_000, "200")
    pts = align_features(store, "m", ["YES", "NO"], "BTCUSDT", BASE, BASE + 180_000,
                         step_ms=60_000, decision_now_ms=BASE + 60_000)
    assert [p.decision_ts_ms for p in pts] == [BASE, BASE + 60_000]
    store.close()


# ---------- S0-07 YES/NO 归一化 ----------

def test_poly_yes_no_tokens_are_normalized(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _book(store, "YES", BASE, "0.60", "0.62")
    _book(store, "NO", BASE, "0.36", "0.39")
    pts = align_features(store, "m", ["YES", "NO"], "BTCUSDT", BASE, BASE + 60_000,
                         step_ms=60_000)
    poly = pts[0].feature.polymarket
    assert poly["yes_mid"] == Decimal("0.61")
    assert poly["no_mid"] == Decimal("0.375")
    assert poly["consistent"] is True
    assert poly["prob_usable"] is True
    store.close()


def test_poly_inconsistent_books_are_flagged(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    _book(store, "YES", BASE, "0.60", "0.62")
    _book(store, "NO", BASE, "0.55", "0.58")   # yes+no = 1.19 → 不合一价
    pts = align_features(store, "m", ["YES", "NO"], "BTCUSDT", BASE, BASE + 60_000,
                         step_ms=60_000)
    poly = pts[0].feature.polymarket
    assert poly["consistent"] is False
    assert poly["prob_usable"] is False, "FAIL: 不一致的盘口不得当作概率使用"
    store.close()


# ---------- S0-08 / S0-09 / S0-10 采集器 ----------

def _book_payload(ts_ms: int | None, bid: str = "0.60", ask: str = "0.62",
                  hash_: str = "0xh") -> dict:
    p = {"market": "0xm", "asset_id": "111", "hash": hash_,
         "bids": [{"price": "0.50", "size": "10"}, {"price": bid, "size": "10"}],
         "asks": [{"price": "0.70", "size": "10"}, {"price": ask, "size": "10"}]}
    if ts_ms is not None:
        p["timestamp"] = str(ts_ms)
    return p


def test_best_bid_ask_not_level_zero(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    c = PolyRestCollector(store, {"m": {"clob_token_ids": ["111"]}},
                          fetch_fn=lambda u: _book_payload(BASE))
    ev = c.poll_once(now_ms=BASE + 200)[0]
    assert ev.bid == Decimal("0.60"), "FAIL: best bid 必须取最高买价"
    assert ev.ask == Decimal("0.62"), "FAIL: best ask 必须取最低卖价"
    assert ev.spread_abs == Decimal("0.02")
    store.close()


def test_missing_server_timestamp_is_rejected(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    c = PolyRestCollector(store, {"m": {"clob_token_ids": ["111", "222"]}},
                          fetch_fn=lambda u: _book_payload(None))
    events = c.poll_once(now_ms=BASE + 5_000)
    assert events == []
    assert store.count() == 0, "FAIL: 缺失服务端时间戳时禁止用本地时间冒充 event_ts"
    assert c.stats["skipped_missing_server_ts"] == 2
    store.close()


def test_duplicate_book_hash_not_stored_twice(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    c = PolyRestCollector(store, {"m": {"clob_token_ids": ["111"]}},
                          fetch_fn=lambda u: _book_payload(BASE))
    c.poll_once(now_ms=BASE + 200)
    c.poll_once(now_ms=BASE + 2_200)
    assert store.count() == 1, "FAIL: 同一 book hash 重复入库（重复采集）"
    assert c.stats["duplicates_skipped"] == 1
    store.close()


# ---------- S0-11 可用性排序 ----------

def test_replay_orders_by_availability_not_event_ts(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    store.append("binance", "snapshot", "BTCUSDT", 1000, 3000, {"price": "1"})
    store.append("binance", "snapshot", "BTCUSDT", 2000, 2000, {"price": "2"})
    store.append("binance", "snapshot", "BTCUSDT", 3000, 2500, {"price": "3"})
    state = ReplayEngine(store).run(0, 4000)
    assert state.seen_events == 3, "FAIL: 按 event_ts 排序会在迟到数据上抛乱序异常"
    # 同一可用时刻（3000）两条：事件时间更新的那条最后应用
    assert state.binance_last["BTCUSDT"]["price"] == "3"
    store.close()


def test_store_query_by_availability(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    store.append("binance", "snapshot", "BTCUSDT", 1000, 3000, {"price": "1"})
    store.append("binance", "snapshot", "BTCUSDT", 2000, 2000, {"price": "2"})
    rows = store.query_available("binance", 0, 4000)
    assert [r["payload"]["price"] for r in rows] == ["2", "1"]
    store.close()


def test_query_available_keeps_late_events_before_window(tmp_path: Path) -> None:
    """窗口前发生、窗口内才到达的事件不能凭空消失（否则看起来像数据缺失）。"""
    store = EventStore(tmp_path / "e.sqlite")
    store.append("binance", "snapshot", "BTCUSDT", 1000, 5000, {"price": "1"})
    rows = store.query_available("binance", 4000, 6000, as_of_ms=5000)
    assert len(rows) == 1 and rows[0]["payload"]["price"] == "1"
    store.close()


# ---------- S0-12 CVD 已收盘 ----------

def test_cvd_uses_closed_kline_only(tmp_path: Path) -> None:
    closed = [BASE - 60_000, "1", "1", "1", "1", "100", BASE - 1, "100", "5", "70", "70", "0"]
    open_ = [BASE, "1", "1", "1", "1", "100", BASE + 60_000, "100", "5", "10", "10", "0"]
    c = BinanceMicroCollector(
        store=EventStore(tmp_path / "e.sqlite"), symbols=["BTCUSDT"],
        fetch_fn=lambda u: (
            {"openInterest": "100", "time": BASE} if "openInterest" in u
            else {"lastFundingRate": "0", "time": BASE} if "premiumIndex" in u
            else [closed, open_] if "klines" in u
            else {"lastPrice": "67000", "volume": "1"}
        ),
    )
    snap = c.snapshot("BTCUSDT", now_ms=BASE + 100)
    assert snap.cvd_delta == Decimal("40"), "FAIL: CVD 用了未收盘 K 线（前视/噪声）"
    c.store.close()


# ---------- S0-13 成本模型 ----------

def test_cost_model_includes_funding() -> None:
    v = evaluate_cost(CostInput(
        ev_gross_bps=Decimal("30"), taker_fee_bps=Decimal("2.5"), spread_bps=Decimal("5"),
        slippage_bps=Decimal("1"), latency_cost_bps=Decimal("0.5"), impact_bps=Decimal("2"),
        horizon_ms=300_000, funding_bps=Decimal("2"),
    ))
    assert v.ev_net_bps == Decimal("14.5"), f"FAIL: 资金费未计入 EV_net，实得 {v.ev_net_bps}"