"""Cross-Market v2.2 P0 测试：时间戳 / 模式 / 存储 / 回放 / 映射。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.cross_market.schemas import (
    BinanceSnapshot,
    FeatureSnapshot,
    LiquidationObservation,
    PolyBookEvent,
    classify_oi_state,
)
from research.cross_market.timestamp import Timestamped, align, is_stale, lag_ms
from research.cross_market.storage import EventStore
from research.cross_market.replay import ReplayEngine
from research.cross_market.market_mapping import MarketMapping, is_researchable, load_mapping


# ---------- 时间戳（规格 §8） ----------

def test_align_uses_exchange_ts_when_present() -> None:
    a = Timestamped(event_ts_ms=1000, exchange_ts_ms=2000, ingest_ts_ms=2500)
    b = Timestamped(event_ts_ms=1500, exchange_ts_ms=2100, ingest_ts_ms=2600)
    assert align(a, b, tolerance_ms=200)
    assert not align(a, b, tolerance_ms=50)


def test_is_stale() -> None:
    e = Timestamped(event_ts_ms=1000, exchange_ts_ms=None, ingest_ts_ms=1000)
    assert is_stale(e, now_ms=10_000, max_age_ms=5_000)
    assert not is_stale(e, now_ms=4_000, max_age_ms=5_000)


def test_ingest_lag_is_diagnostic_only() -> None:
    e = Timestamped(event_ts_ms=1000, exchange_ts_ms=1100, ingest_ts_ms=1800)
    assert lag_ms(e) == 800


# ---------- schemas（规格 §2/§3/§4/§5） ----------

def test_decimal_prices() -> None:
    ev = PolyBookEvent(
        market_id="m1", token_id="t1", event_type="book",
        bid=Decimal("0.61"), ask=Decimal("0.63"),
        spread_abs=Decimal("0.02"), spread_bps=Decimal("328"),
        event_ts_ms=1000, exchange_ts_ms=1000, ingest_ts_ms=1200,
    )
    assert ev.spread_abs == Decimal("0.02")
    assert ev.spread_bps == Decimal("328")


def test_liquidation_is_partial_observation() -> None:
    liq = LiquidationObservation(symbol="ETHUSDT", observed_notional_usdt=Decimal("120000"), observation_count=3)
    text = liq.describe()
    assert "PARTIAL" in text
    assert "BINANCE" in text
    assert "120000" in text


def test_oi_state_is_observational() -> None:
    s = classify_oi_state(Decimal("0.5"), Decimal("0.3"))
    assert s == "PRICE_UP_OI_EXPANSION_BUY_FLOW"
    s2 = classify_oi_state(Decimal("-0.5"), Decimal("0.2"))
    assert s2 == "PRICE_DOWN_OI_EXPANSION_SELL_FLOW"
    s3 = classify_oi_state(Decimal("0"), Decimal("0"))
    assert s3 == "PRICE_FLAT_OI_FLAT"


# ---------- 存储（append-only + 时间查询） ----------

def test_event_store_roundtrip(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    store.append("polymarket", "book", "t1", event_ts_ms=1000, ingest_ts_ms=1100, payload={"bid": "0.61"})
    store.append("binance", "snapshot", "ETHUSDT", event_ts_ms=2000, ingest_ts_ms=2100, payload={"price": "2000"})
    rows = store.query(None, 0, 3000)
    assert len(rows) == 2
    assert rows[0]["source"] == "polymarket"
    assert rows[1]["payload"]["price"] == "2000"
    store.close()


def test_event_store_time_filter(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    store.append("binance", "snapshot", "ETHUSDT", 1000, 1000, {"price": "1"})
    store.append("binance", "snapshot", "ETHUSDT", 5000, 5000, {"price": "2"})
    rows = store.query("binance", 0, 2000)
    assert len(rows) == 1
    store.close()


# ---------- 回放（确定性 + 乱序拒绝） ----------

def test_replay_deterministic(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    for ts in (1000, 2000, 3000):
        store.append("binance", "snapshot", "ETHUSDT", ts, ts, {"price": str(ts)})
    store.close()

    st1 = EventStore(tmp_path / "events.sqlite")
    st2 = EventStore(tmp_path / "events.sqlite")
    r1 = ReplayEngine(st1).run(0, 4000)
    r2 = ReplayEngine(st2).run(0, 4000)
    assert r1.binance_last == r2.binance_last
    assert r1.now_ms == 3000
    assert r1.seen_events == 3
    st1.close()
    st2.close()


def test_replay_rejects_out_of_order(tmp_path: Path) -> None:
    """存储层保证查询有序；引擎守卫防的是实时流直喂（_apply 层）。"""
    store = EventStore(tmp_path / "events.sqlite")
    engine = ReplayEngine(store)
    engine._apply({"source": "binance", "event_type": "snapshot", "key": "ETHUSDT",
                   "event_ts_ms": 5000, "ingest_ts_ms": 5000, "payload": {}})
    with pytest.raises(ValueError, match="乱序事件"):
        engine._apply({"source": "binance", "event_type": "snapshot", "key": "ETHUSDT",
                       "event_ts_ms": 1000, "ingest_ts_ms": 1000, "payload": {}})
    store.close()


def test_store_query_always_sorted(tmp_path: Path) -> None:
    """存储层铁律：任何写入顺序，查询必须按 event_ts 正序。"""
    store = EventStore(tmp_path / "events.sqlite")
    store._db.execute(
        "INSERT INTO raw_events (source, event_type, key, event_ts_ms, ingest_ts_ms, payload) VALUES (?,?,?,?,?,?)",
        ("binance", "snapshot", "ETHUSDT", 5000, 5000, "{}"),
    )
    store._db.execute(
        "INSERT INTO raw_events (source, event_type, key, event_ts_ms, ingest_ts_ms, payload) VALUES (?,?,?,?,?,?)",
        ("binance", "snapshot", "ETHUSDT", 1000, 1000, "{}"),
    )
    store._db.commit()
    rows = store.query(None, 0, 9000)
    assert [r["event_ts_ms"] for r in rows] == [1000, 5000]
    store.close()


def test_feature_at_is_point_in_time(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.sqlite")
    store.append("polymarket", "book", "t1", 1000, 1000, {"probability": "0.62"})
    store.append("binance", "snapshot", "ETHUSDT", 1000, 1000, {"price": "2000"})
    engine = ReplayEngine(store)
    engine.run(0, 2000)
    f = engine.feature_at("t1", "ETHUSDT")
    assert f["polymarket"]["probability"] == "0.62"
    assert f["binance"]["price"] == "2000"
    store.close()


# ---------- market mapping（规格 §6） ----------

def _write_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "market_mapping.yaml"
    p.write_text(
        "eth_above_2000:\n"
        "  underlying_asset: ETH\n"
        "  event_threshold: 2000\n"
        "  event_comparator: ABOVE\n"
        "  event_expiry: '2026-11-20T00:00:00Z'\n"
        "  resolution_source: binance\n"
        "  target_horizons: [30s, 1m, 5m, 15m, 1h]\n"
        "  primary_horizon: 5m\n",
        encoding="utf-8",
    )
    return p


def test_mapping_load_and_researchable(tmp_path: Path) -> None:
    mapping = load_mapping(_write_yaml(tmp_path))
    m = mapping["eth_above_2000"]
    assert isinstance(m, MarketMapping)
    assert m.primary_horizon == "5m"
    assert is_researchable("eth_above_2000", mapping)
    assert not is_researchable("unregistered_market", mapping)  # fail-closed


def test_mapping_rejects_bad_comparator(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        "m:\n  underlying_asset: ETH\n  event_threshold: 1\n  event_comparator: SIDEWAYS\n"
        "  event_expiry: '2026-11-20T00:00:00Z'\n  resolution_source: x\n"
        "  target_horizons: [5m]\n  primary_horizon: 5m\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="event_comparator"):
        load_mapping(p)
