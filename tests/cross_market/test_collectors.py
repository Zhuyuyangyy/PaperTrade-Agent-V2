"""P1 采集器测试：时间戳铁律 + 故障响亮 + 注入式 fetch（零网络）。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from research.cross_market.collectors.polymarket_rest import (
    CollectorError,
    PolyRestCollector,
)
from research.cross_market.storage import EventStore


def _book(ts_ms: int, bid: str = "0.61", ask: str = "0.63") -> dict:
    return {
        "market": "0xabc",
        "asset_id": "123",
        "timestamp": str(ts_ms),
        "hash": "0xdead",
        "bids": [{"price": bid, "size": "100"}],
        "asks": [{"price": ask, "size": "100"}],
    }


MARKETS = {
    "btc_above_1m": {
        "gamma_id": "1393070",
        "clob_token_ids": ["111", "222"],
    },
}


def test_poll_writes_events_with_three_timestamps(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    server_ts = 1_795_000_000_000
    recv_ts = server_ts + 800  # 本地晚 800ms 收到

    def fake_fetch(url: str) -> dict:
        assert "token_id=111" in url or "token_id=222" in url
        return _book(server_ts)

    c = PolyRestCollector(store, MARKETS, fetch_fn=fake_fetch)
    events = c.poll_once(now_ms=recv_ts)
    assert len(events) == 2  # Yes + No 两个 token
    ev = events[0]
    # 铁律：event_ts = 服务端时间，禁拿 recv 冒充
    assert ev.event_ts_ms == server_ts
    assert ev.ingest_ts_ms == recv_ts
    assert ev.exchange_ts_ms == server_ts
    # 落盘一致
    rows = store.query("polymarket", server_ts - 1, recv_ts + 1)
    assert len(rows) == 2
    assert rows[0]["event_ts_ms"] == server_ts
    assert rows[0]["ingest_ts_ms"] == recv_ts


def test_spread_computed_decimal(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "e.sqlite")
    c = PolyRestCollector(store, MARKETS, fetch_fn=lambda u: _book(1000, "0.61", "0.63"))
    ev = c.poll_once(now_ms=1200)[0]
    assert ev.spread_abs == Decimal("0.02")
    assert ev.spread_bps is not None


def test_fetch_failure_is_loud(tmp_path: Path) -> None:
    """采集失败必须抛错（loud absence），禁静默跳过。"""
    store = EventStore(tmp_path / "e.sqlite")

    def bad_fetch(url: str) -> dict:
        raise CollectorError("数据源不可达")

    c = PolyRestCollector(store, MARKETS, fetch_fn=bad_fetch)
    with pytest.raises(CollectorError):
        c.poll_once()
    assert c.stats["errors"] == 1
    # 失败不得写入任何事件
    assert store.count() == 0


def test_recorder_captures_missing_server_timestamp(tmp_path: Path) -> None:
    """服务端没给 timestamp 时：event_ts 退回 recv，但必须显式记录退化。"""
    store = EventStore(tmp_path / "e.sqlite")
    book = _book(1000)
    del book["timestamp"]
    c = PolyRestCollector(store, MARKETS, fetch_fn=lambda u: book)
    ev = c.poll_once(now_ms=5000)[0]
    assert ev.event_ts_ms == 5000  # 退化为本地时间
    # 但 ingest 与 event 相同 → 可用性门禁不会误放（无伪造的服务端时间）


def test_poll_respects_market_token_list(tmp_path: Path) -> None:
    """无 clob_token_ids 的市场跳过（fail-closed）。"""
    store = EventStore(tmp_path / "e.sqlite")
    c = PolyRestCollector(store, {"empty": {"gamma_id": "1"}}, fetch_fn=lambda u: _book(1000))
    assert c.poll_once() == []
    assert store.count() == 0
