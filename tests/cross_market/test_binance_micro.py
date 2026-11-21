"""Binance 微结构采集器测试：时间戳/CVD 本地计算/强平 UNKNOWN/OI delta/注入式 fetch。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from research.cross_market.collectors.binance_microstructure import (
    BinanceMicroCollector,
    oi_deltas_from_store,
)
from research.cross_market.storage import EventStore


def _ticker() -> dict:
    return {"lastPrice": "67000.50", "volume": "1234.5", "closeTime": 1_795_000_000_000}


def _oi() -> dict:
    return {"openInterest": "5678.9", "time": 1_795_000_000_500}


def _premium() -> dict:
    return {"lastFundingRate": "0.0001"}


def _kline() -> list:
    # [openTime, O, H, L, C, V, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
    now = 1_795_000_000_000
    return [[now - 60000, "1", "1", "1", "1", "100.0", now - 1000, "100", "50", "70.0", "70", "0"]]


def test_snapshot_three_timestamps(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    c = BinanceMicroCollector(store, ["BTCUSDT"], fetch_fn=lambda u: (
        _oi() if "openInterest" in u else _premium() if "premiumIndex" in u
        else _kline() if "klines" in u else _ticker()
    ))
    snap = c.snapshot("BTCUSDT", now_ms=1_795_000_001_000)
    # event_ts = 服务端 time（oi.time），禁拿 recv 冒充
    assert snap.event_ts_ms == 1_795_000_000_500
    assert snap.ingest_ts_ms == 1_795_000_001_000
    assert snap.price == Decimal("67000.50")
    assert snap.oi_snapshot == Decimal("5678.9")
    rows = store.query("binance", 0, 2_000_000_000_000)
    assert len(rows) == 1
    assert rows[0]["payload"]["funding_rate"] == "0.0001"


def test_cvd_delta_computed_locally(tmp_path: Path) -> None:
    """CVD = 2×taker_buy − volume = 140 − 100 = 40（本地计算，不 pretend 服务端）。"""
    store = EventStore(tmp_path / "e.sqlite")
    c = BinanceMicroCollector(store, ["BTCUSDT"], fetch_fn=lambda u: (
        _oi() if "openInterest" in u else _premium() if "premiumIndex" in u
        else _kline() if "klines" in u else _ticker()
    ))
    snap = c.snapshot("BTCUSDT", now_ms=1_795_000_001_000)
    assert snap.cvd_delta == Decimal("40.0")


def test_liquidation_is_unknown_not_fake(tmp_path: Path) -> None:
    """REST 无强平端点 → 恒记 UNKNOWN 覆盖的零观测，禁伪造全网清算。"""
    store = EventStore(tmp_path / "e.sqlite")
    c = BinanceMicroCollector(store, ["BTCUSDT"], fetch_fn=lambda u: (
        _oi() if "openInterest" in u else _premium() if "premiumIndex" in u
        else _kline() if "klines" in u else _ticker()
    ))
    c.snapshot("BTCUSDT", now_ms=1_795_000_001_000)
    row = store.query("binance", 0, 2_000_000_000_000)[0]
    assert row["payload"]["liquidation_observed_usdt"] == "0"
    assert row["payload"]["liquidation_coverage"] == "UNKNOWN"


def test_fetch_failure_loud(tmp_path: Path) -> None:
    from research.cross_market.collectors.binance_microstructure import CollectorError

    store = EventStore(tmp_path / "e.sqlite")

    def bad(u: str) -> dict:
        raise CollectorError("数据源不可达")

    c = BinanceMicroCollector(store, ["BTCUSDT"], fetch_fn=bad)
    with pytest.raises(CollectorError):
        c.snapshot("BTCUSDT")
    assert store.count() == 0


def test_oi_deltas_from_store(tmp_path: Path) -> None:
    """OI 变化从本地存储计算（规格 §3：不 pretend 服务端有 1m 历史）。"""
    store = EventStore(tmp_path / "e.sqlite")
    base = 1_795_000_000_000

    def put(ts: int, oi: str) -> None:
        store.append("binance", "snapshot", "BTCUSDT", ts, ts,
                     {"oi_snapshot": oi, "price": "1", "volume": "1", "cvd_delta": "0",
                      "funding_rate": "0", "liquidation_observed_usdt": "0",
                      "liquidation_coverage": "UNKNOWN"})

    put(base, "100")            # t0
    put(base + 60_000, "105")   # t0+1m
    put(base + 300_000, "110")  # t0+5m
    d = oi_deltas_from_store(store, "BTCUSDT", base + 300_001)
    # 引用=标记前最新快照：1m 前引用 t0+1m(105) → +4.76%；5m 前引用 t0(100) → +10%
    assert d["oi_delta_1m_pct"] == (Decimal("110") - Decimal("105")) / Decimal("105") * 100
    assert d["oi_delta_5m_pct"] == Decimal("10")
    # 15m 标记前没有任何快照（最早的是 5min 前）→ None（不拿 5min 前冒充 15m 前）
    assert d["oi_delta_15m_pct"] is None


def test_oi_deltas_none_without_history(tmp_path: Path) -> None:
    """没有任何历史快照 → 全部 None（不猜）。"""
    store = EventStore(tmp_path / "e.sqlite")
    d = oi_deltas_from_store(store, "BTCUSDT", 1_795_000_000_000)
    assert all(v is None for v in d.values())
