"""Forward Test 验收（Sprint 4.4）：不能只看 rounds=72。

校验项：
  1. 真实经过时间（首末决策日志时间差，或心跳 updated_at）≥ 目标小时数；
  2. `successful_rounds` == 决策日志行数（成功决策可追溯）；
  3. `successful_rounds` ≥ 目标轮次；
  4. 验证盘订单数 == 0（没有 VERIFIED 策略就不该有单）；
  5. 每笔订单都能在日志里找到对应的 fill / shadow_fill；
  6. 订单 intent_id 唯一；
  7. 失败尝试数 = rounds - successful_rounds，且全部有 last_error 记录。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_TOLERANCE = 0.02      # 允许 2% 的时间误差（启动/调度抖动）


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _count_orders(path: Path) -> tuple[int, int, list[str]]:
    """(orders, shadow_orders, intent_ids)。只读打开。"""
    if not path.is_file():
        return 0, 0, []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT intent_id, shadow FROM orders").fetchall()
        ids = [r["intent_id"] for r in rows]
        shadow = sum(1 for r in rows if r["shadow"])
        return len(rows), shadow, ids
    finally:
        conn.close()


def verify(root: Path | str, *, expected_successful: int, expected_hours: float,
           tolerance: float = DEFAULT_TOLERANCE, verified_db: str = "verified.sqlite",
           shadow_db: str = "shadow.sqlite") -> dict:
    root = Path(root)
    hb_path = root / "state" / "heartbeat.json"
    log_path = root / "data" / "decisions.jsonl"
    checks: dict[str, dict] = {}

    hb = None
    if hb_path.is_file():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
        except ValueError:
            hb = None
    entries = _read_jsonl(log_path)
    v_orders, _, v_ids = _count_orders(root / verified_db)
    s_orders, s_shadow, s_ids = _count_orders(root / shadow_db)

    first_ts = entries[0]["round_ts_ms"] if entries else 0
    last_ts = entries[-1]["round_ts_ms"] if entries else 0
    elapsed_h = (last_ts - first_ts) / 3_600_000 if entries else 0.0

    log_fill_ids = set()
    for e in entries:
        for key in ("fill", "shadow_fill"):
            f = e.get(key)
            if f:
                log_fill_ids.add(f["intent_id"])
    all_order_ids = set(v_ids) | set(s_ids)

    successful = int((hb or {}).get("successful_rounds", 0))
    attempts = int((hb or {}).get("rounds", 0))
    failed_attempts = max(0, attempts - successful)

    checks["heartbeat_present"] = {"ok": hb is not None}
    checks["successful_rounds_matches_log"] = {
        "ok": successful == len(entries),
        "successful_rounds": successful, "log_lines": len(entries),
    }
    checks["successful_rounds_reached"] = {
        "ok": successful >= expected_successful,
        "successful_rounds": successful, "expected": expected_successful,
    }
    checks["elapsed_hours"] = {
        "ok": elapsed_h >= expected_hours * (1 - tolerance),
        "elapsed_hours": round(elapsed_h, 3), "expected": expected_hours,
    }
    checks["verified_book_empty"] = {
        "ok": v_orders == 0, "verified_orders": v_orders,
    }
    checks["orders_traceable"] = {
        "ok": all_order_ids.issubset(log_fill_ids) or not all_order_ids,
        "orders": len(all_order_ids), "logged_fills": len(log_fill_ids),
        "missing": sorted(all_order_ids - log_fill_ids)[:5],
    }
    checks["orders_unique"] = {
        "ok": len(v_ids) == len(set(v_ids)) and len(s_ids) == len(set(s_ids)),
        "verified": len(v_ids), "shadow": len(s_ids),
    }
    last_error = (hb or {}).get("last_error", "")
    checks["failures_recorded"] = {
        "ok": failed_attempts == 0 or bool(last_error),
        "failed_attempts": failed_attempts,
        "last_error": last_error,
    }

    passed = all(c["ok"] for c in checks.values())
    return {
        "passed": passed,
        "checks": checks,
        "stats": {
            "rounds_attempted": attempts,
            "successful_rounds": successful,
            "failed_attempts": failed_attempts,
            "log_lines": len(entries),
            "elapsed_hours": round(elapsed_h, 3),
            "verified_orders": v_orders,
            "shadow_orders": s_orders,
            "shadow_share": s_shadow,
        },
    }