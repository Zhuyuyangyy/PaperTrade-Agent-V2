"""只读审计：不打开写句柄、不修改 data 目录，只输出 JSON。

用法：
    python -m scripts.audit_experiment --root data/v3_abcd_24h --out reports/audit_2026-11-22.json

审计六个已知风险点，并把每条结论附上可复核的证据计数。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

BOOKS = ("rules_5m", "deepseek_5m", "dual_5m", "deepseek_5m_manager")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _tables(db: Path) -> dict[str, list[str]]:
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: [r[1] for r in con.execute(f"PRAGMA table_info({n})")]
                for n in names}
    finally:
        con.close()


def _query(db: Path, sql: str) -> list[dict]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def audit(root: Path) -> dict:
    report: dict = {"root": str(root), "books": {}, "findings": []}
    all_decisions: dict[str, list[dict]] = {}
    for name in BOOKS:
        rows = _rows(root / f"{name}_decisions.jsonl")
        all_decisions[name] = rows
        db = root / f"{name}.sqlite"
        tables = _tables(db)
        closed = _query(db, "SELECT * FROM positions WHERE status='closed'")
        fills = _query(db, "SELECT * FROM position_fills")
        open_pos = _query(db, "SELECT * FROM positions WHERE status='open'")

        managed_actions: dict[str, int] = {}
        ai_status: dict[str, int] = {}
        ai_providers: dict[str, int] = {}
        snapshots: list[dict] = []
        entry_closed = 0
        managed_closed = 0
        for r in rows:
            e = r.get("entry") or {}
            entry_closed += len(e.get("closed") or [])
            m = e.get("managed") or {}
            if not m:
                continue
            act = str(m.get("action") or "?")
            managed_actions[act] = managed_actions.get(act, 0) + 1
            dec = m.get("decision") or {}
            st = str(dec.get("status") or "?")
            ai_status[st] = ai_status.get(st, 0) + 1
            pv = str(dec.get("provider") or "?")
            ai_providers[pv] = ai_providers.get(pv, 0) + 1
            if act == "CLOSE_ALL":
                managed_closed += 1
            if m.get("snapshot"):
                snapshots.append(m["snapshot"])

        refresh_total = refresh_ok = 0
        errors_418 = 0
        for r in rows:
            h = r.get("history_refresh") or {}
            if h.get("attempted"):
                refresh_total += 1
                refresh_ok += 1 if h.get("ok") else 0
            for x in h.get("errors") or []:
                if "418" in str(x) or "429" in str(x):
                    errors_418 += 1

        snap_fields = []
        if snapshots:
            wanted = {"news_delta", "trend", "entry_reason", "invalid_if",
                      "mfe", "mae", "risk_budget_left"}
            snap_fields = sorted({
                k for s in snapshots for k, v in s.items()
                if k in wanted and v not in (None, "", [], {}, "0", "0E-8")
            })
            zeros = {k: sum(1 for s in snapshots if str(s.get(k)) in ("0", "0.0", "0E-8"))
                     for k in ("mfe", "mae", "risk_budget_left")}

        report["books"][name] = {
            "decisions": len(rows),
            "db_tables": sorted(tables),
            "has_mgmt_stage_column": "mgmt_stage" in tables.get("positions", []),
            "closed_positions": len(closed),
            "partial_fills": len(fills),
            "open_positions": len(open_pos),
            "realized_in_closed": sum(float(r.get("pnl") or 0) for r in closed),
            "realized_in_fills": sum(float(r.get("pnl") or 0) for r in fills),
            "realized_net_column": sum(float(r.get("realized_net") or 0)
                                       for r in open_pos + closed),
            "entry_closed_count": entry_closed,
            "managed_closed_count": managed_closed,
            "managed_actions": managed_actions,
            "ai_status": ai_status,
            "ai_providers": ai_providers,
            "snapshots": len(snapshots),
            "snap_nonempty_fields": snap_fields,
            "snap_zero_counts": zeros if snapshots else {},
            "history_refresh_attempts": refresh_total,
            "history_refresh_ok": refresh_ok,
            "errors_418_429_seen": errors_418,
        }

    c = report["books"].get("deepseek_5m_manager", {})
    d = report["books"].get("dual_5m", {})
    report["findings"] = [
        {
            "id": "P0-1", "title": "C 组重复减仓风险",
            "evidence": {"c_partial_actions": c.get("managed_actions", {}).get("CLOSE_PARTIAL", 0),
                         "mgmt_stage_column": c.get("has_mgmt_stage_column", False),
                         "partial_fills": c.get("partial_fills", 0)},
            "verdict": "CONFIRMED_RISK" if c.get("managed_actions", {}).get("CLOSE_PARTIAL", 0) > 1
                       and not c.get("has_mgmt_stage_column", False) else "NOT_OBSERVED_YET",
        },
        {
            "id": "P0-2", "title": "部分平仓收益漏算",
            "evidence": {n: {"fills": v["realized_in_fills"],
                             "closed": v["realized_in_closed"],
                             "realized_net_col": v["realized_net_column"]}
                         for n, v in report["books"].items()},
            "verdict": "REPORT_MISSING_FILLS" if any(v["partial_fills"] for v in report["books"].values())
                       else "NO_FILLS_YET",
        },
        {
            "id": "P0-3", "title": "managed CLOSE_ALL 未计入 closes",
            "evidence": {n: {"entry_closed": v["entry_closed_count"],
                             "managed_closed": v["managed_closed_count"],
                             "db_closed": v["closed_positions"]}
                         for n, v in report["books"].items()},
            "verdict": "REPORT_MISSING_MANAGED" if any(v["managed_closed_count"]
                                                      for v in report["books"].values()) else "NO_MANAGED_CLOSE_YET",
        },
        {
            "id": "P1-4", "title": "D 组 AI 快照完整性",
            "evidence": {"nonempty_fields": d.get("snap_nonempty_fields", []),
                         "zero_counts": d.get("snap_zero_counts", {}),
                         "snapshots": d.get("snapshots", 0)},
            "verdict": "OK" if len(d.get("snap_nonempty_fields", [])) >= 6
                       else ("NEED_FIX" if d.get("snapshots", 0) > 0 else "NO_SNAPSHOT_YET"),
        },
        {
            "id": "P1-5", "title": "D 组是否真的调用了模型",
            "evidence": {"status": d.get("ai_status", {}), "providers": d.get("ai_providers", {})},
            "verdict": "AI_CONFIRMED" if d.get("ai_status", {}).get("OK", 0) > 0
                       else "NO_SUCCESSFUL_AI_CALL",
        },
        {
            "id": "P1-6", "title": "418/429 数据质量",
            "evidence": {n: {"attempts": v["history_refresh_attempts"],
                             "ok": v["history_refresh_ok"],
                             "errors": v["errors_418_429_seen"]}
                         for n, v in report["books"].items()},
            "verdict": "DEGRADED" if any(v["errors_418_429_seen"]
                                        for v in report["books"].values()) else "CLEAN",
        },
    ]
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    data = audit(Path(a.root))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"written -> {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
