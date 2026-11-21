"""验收脚本测试（Sprint 4.4）：只在数据真的对得上时 PASS。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from research.paper.broker import PaperBroker
from research.runtime.verify_run import verify
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000 // 3_600_000 * 3_600_000
HOUR = 3_600_000


def _entry(ts: int, fill: dict | None = None, shadow_fill: dict | None = None) -> dict:
    return {"round_ts_ms": ts, "selection": {"decision": "LONG"},
            "fill": fill, "shadow_fill": shadow_fill}


def _intent(iid: str, shadow: bool) -> TradeIntent:
    return TradeIntent(iid, BASE, "BTC/USDT", "LONG", "trend_follow", "v1",
                       D("100"), D("95"), D("110"), HOUR, D("0.7"), "t", shadow=shadow)


def _root(tmp_path: Path, entries: list[dict], successful: int, rounds: int,
          shadow_orders: int = 1, verified_orders: int = 0) -> Path:
    root = tmp_path / "run"
    (root / "data").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "data" / "decisions.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    (root / "state" / "heartbeat.json").write_text(json.dumps({
        "rounds": rounds, "successful_rounds": successful, "last_error": "",
        "last_round_key_ms": entries[-1]["round_ts_ms"] if entries else 0,
    }), encoding="utf-8")
    shadow = PaperBroker(root / "shadow.sqlite", shadow_mode="shadow_only")
    verified = PaperBroker(root / "verified.sqlite")
    for i in range(shadow_orders):
        shadow.submit(_intent(f"s{i}", True), D("100"))
    for i in range(verified_orders):
        verified.submit(_intent(f"v{i}", False), D("100"))
    shadow.close()
    verified.close()
    return root


def _with_fills(entries: list[dict], ids: list[str]) -> list[dict]:
    out = []
    for i, e in enumerate(entries):
        e = dict(e)
        if i < len(ids):
            e["shadow_fill"] = {"intent_id": ids[i]}
        out.append(e)
    return out


def test_passes_when_everything_matches(tmp_path: Path) -> None:
    ts0 = BASE
    entries = [_entry(ts0 + i * HOUR) for i in range(3)]
    entries = _with_fills(entries, ["s0"])
    root = _root(tmp_path, entries, successful=3, rounds=3, shadow_orders=1)
    out = verify(root, expected_successful=3, expected_hours=2)
    assert out["passed"] is True, out
    assert out["stats"]["verified_orders"] == 0
    assert out["stats"]["shadow_orders"] == 1


def test_fails_when_log_and_heartbeat_disagree(tmp_path: Path) -> None:
    entries = [_entry(BASE + i * HOUR) for i in range(2)]
    root = _root(tmp_path, entries, successful=1, rounds=2)
    out = verify(root, expected_successful=2, expected_hours=1)
    assert out["passed"] is False
    assert out["checks"]["successful_rounds_matches_log"]["ok"] is False


def test_fails_when_verified_book_has_orders(tmp_path: Path) -> None:
    entries = _with_fills([_entry(BASE + i * HOUR) for i in range(2)], ["v0"])
    root = _root(tmp_path, entries, successful=2, rounds=2, verified_orders=1)
    out = verify(root, expected_successful=2, expected_hours=1)
    assert out["passed"] is False
    assert out["checks"]["verified_book_empty"]["ok"] is False


def test_fails_when_order_missing_from_log(tmp_path: Path) -> None:
    entries = [_entry(BASE + i * HOUR) for i in range(2)]      # 没有 fill 记录
    root = _root(tmp_path, entries, successful=2, rounds=2, shadow_orders=1)
    out = verify(root, expected_successful=2, expected_hours=1)
    assert out["passed"] is False
    assert out["checks"]["orders_traceable"]["ok"] is False


def test_fails_when_elapsed_too_short(tmp_path: Path) -> None:
    entries = [_entry(BASE + i * HOUR) for i in range(3)]      # 只跨 2 小时
    root = _root(tmp_path, entries, successful=3, rounds=3, shadow_orders=0)
    out = verify(root, expected_successful=3, expected_hours=72)
    assert out["passed"] is False
    assert out["checks"]["elapsed_hours"]["ok"] is False