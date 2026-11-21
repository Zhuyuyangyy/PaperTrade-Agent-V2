"""看板（Sprint 4）：只读状态快照。

数据源只有三样：心跳文件、决策日志、模拟账本。看板不做计算、不发请求、
不修改任何状态；任何缺失都显示 UNKNOWN，而不是替系统假装健康。
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from .heartbeat import Heartbeat, read as read_heartbeat, staleness_ms


def read_ledger(path: Path | str) -> dict | None:
    """只读打开账本（sqlite URI mode=ro）；不存在或不可读 → None。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cash = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
            open_pos = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
            orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            realized = conn.execute("SELECT COALESCE(SUM(CAST(pnl AS REAL)),0) FROM positions"
                                    " WHERE status='closed'").fetchone()[0]
            return {
                "cash": cash["cash"] if cash else "UNKNOWN",
                "open_positions": int(open_pos),
                "orders": int(orders),
                "realized_pnl": str(realized),
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


def _tail_log(path: Path, n: int = 5) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def build_state(root: Path | str, now_ms: int, *, broker=None,
                heartbeat_path: Path | str | None = None,
                log_path: Path | str | None = None,
                stale_after_ms: int = 2 * 3_600_000) -> dict:
    root = Path(root)
    hb_path = Path(heartbeat_path) if heartbeat_path else root / "state" / "heartbeat.json"
    log = Path(log_path) if log_path else root / "data" / "decisions.jsonl"
    hb: Heartbeat | None = read_heartbeat(hb_path)

    state: dict = {
        "as_of_ms": now_ms,
        "heartbeat": hb.to_dict() if hb else None,
        "heartbeat_age_ms": staleness_ms(hb, now_ms) if hb else None,
        "health": "UNKNOWN",
        "ledger": None,
        "last_decisions": _tail_log(log),
        "warnings": [],
    }
    if hb is None:
        state["warnings"].append("NO_HEARTBEAT: 从未运行或状态文件丢失")
    else:
        age = state["heartbeat_age_ms"]
        if hb.halted:
            state["health"] = "HALTED"
        elif age is None:
            state["health"] = "UNKNOWN"
        elif age > stale_after_ms:
            state["health"] = "STALE"
            state["warnings"].append(f"HEARTBEAT_STALE: {age}ms 未成功")
        else:
            state["health"] = "OK"

    if broker is not None:
        state["ledger"] = {
            "cash": str(broker.cash()),
            "open_positions": len(broker.open_positions()),
            "gross_exposure": str(broker.gross_exposure()),
            "realized_pnl": str(broker.realized_pnl_since(0)),
        }
    else:
        ledger = read_ledger(root / "ledger.sqlite")
        if ledger is not None:
            state["ledger"] = ledger
        elif (root / "ledger.sqlite").is_file():
            state["warnings"].append("LEDGER_UNREADABLE: 账本存在但无法只读打开")
    return state


def render_html(state: dict) -> str:
    hb = state.get("heartbeat") or {}
    ledger = state.get("ledger") or {}
    rows = []
    for d in state.get("last_decisions", []):
        st = d.get("state") or {}
        sel = d.get("selection") or {}
        risk = d.get("risk") or {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(d.get('round_ts_ms')))}</td>"
            f"<td>{html.escape(str(st.get('trend')))}/{html.escape(str(st.get('risk')))}</td>"
            f"<td>{html.escape(str(sel.get('decision')))}</td>"
            f"<td>{html.escape(str(risk.get('decision')))}</td>"
            f"<td>{html.escape(str(d.get('equity')))}</td>"
            f"<td>{html.escape(str(sel.get('hold_reason') or ''))}</td>"
            "</tr>"
        )
    warnings = "".join(f"<li>{html.escape(w)}</li>" for w in state.get("warnings", []))
    body = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>OpenAlpha runtime</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #333;padding:6px;text-align:left}}
.k{{color:#9ae6b4}}.warn{{color:#f6ad55}}</style></head><body>
<h1>OpenAlpha runtime</h1>
<p>health: <span class="k">{html.escape(str(state.get('health')))}</span> ·
heartbeat age: {html.escape(str(state.get('heartbeat_age_ms')))} ms ·
rounds: {html.escape(str(hb.get('rounds')))} ·
halted: {html.escape(str(hb.get('halted')))}</p>
<p>halt reason: {html.escape(str(hb.get('halt_reason') or '-'))}</p>
<p>cash: {html.escape(str(ledger.get('cash', 'UNKNOWN')))} ·
open positions: {html.escape(str(ledger.get('open_positions', 'UNKNOWN')))} ·
realized: {html.escape(str(ledger.get('realized_pnl', 'UNKNOWN')))}</p>
<ul class="warn">{warnings}</ul>
<table><thead><tr><th>ts</th><th>state</th><th>decision</th><th>risk</th><th>equity</th><th>hold</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    return body