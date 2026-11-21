"""看板测试（Sprint 4）：状态必须来自文件，缺数据就显示 UNKNOWN。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.paper.broker import PaperBroker
from research.runtime.dashboard import build_state, read_ledger, render_html
from research.runtime.heartbeat import Heartbeat, write

D = Decimal
NOW = 1_795_000_000_000


def _hb(tmp_path: Path, **over) -> Path:
    p = tmp_path / "state" / "heartbeat.json"
    write(p, Heartbeat(**over))
    return p


def test_missing_heartbeat_is_unknown(tmp_path: Path) -> None:
    state = build_state(tmp_path, NOW)
    assert state["health"] == "UNKNOWN"
    assert state["heartbeat"] is None
    assert any("NO_HEARTBEAT" in w for w in state["warnings"])


def test_healthy_and_stale_states(tmp_path: Path) -> None:
    _hb(tmp_path, updated_at_ms=NOW - 1_000, last_ok_ms=NOW - 1_000, rounds=5)
    ok = build_state(tmp_path, NOW)
    assert ok["health"] == "OK" and ok["heartbeat_age_ms"] == 1_000

    _hb(tmp_path, updated_at_ms=NOW - 10_000_000, last_ok_ms=NOW - 10_000_000)
    stale = build_state(tmp_path, NOW)
    assert stale["health"] == "STALE"
    assert any("HEARTBEAT_STALE" in w for w in stale["warnings"])


def test_halted_wins_over_ok(tmp_path: Path) -> None:
    _hb(tmp_path, updated_at_ms=NOW, last_ok_ms=NOW, halted=True, halt_reason="数据断流")
    state = build_state(tmp_path, NOW)
    assert state["health"] == "HALTED"
    assert state["heartbeat"]["halt_reason"] == "数据断流"


def test_ledger_read_is_read_only(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "ledger.sqlite", starting_cash=D("1000"))
    broker.close()
    led = read_ledger(tmp_path / "ledger.sqlite")
    assert led is not None and led["cash"] == "1000" and led["orders"] == 0
    state = build_state(tmp_path, NOW)
    assert state["ledger"] is not None and state["ledger"]["open_positions"] == 0


def test_html_escapes_and_contains_key_fields(tmp_path: Path) -> None:
    _hb(tmp_path, updated_at_ms=NOW, last_ok_ms=NOW, halted=True, halt_reason="<script>x</script>")
    html = render_html(build_state(tmp_path, NOW))
    assert "&lt;script&gt;" in html and "<script>x</script>" not in html
    assert "HALTED" in html and "halt reason" in html