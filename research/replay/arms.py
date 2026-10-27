"""One table for every ledger on the desk.

Five books are running at once -- the live desk, the control group, and three
experiment arms -- and each one lives in its own SQLite file. Reading them one
query at a time is how an experiment quietly becomes a story: whatever book you
looked at last looks best. This prints them side by side, with the same
columns, at the same moment, and it lists a book even when its numbers are
still nothing (an arm that has not closed a trade is not allowed to disappear
from the table).

Net is reported twice: at the config's assumed cost and at the modelled one,
because the difference between those two is currently most of the book.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .costs import _config_cost, _liquidity_at, modelled_round_trip

DEFAULT_DB = "memehunter.sqlite"
CONTROL_DB = "counterpart.sqlite"


def _book(conn: sqlite3.Connection, assumed: float) -> dict:
    rows = list(conn.execute(
        "SELECT mint, opened_at_ms, notional_usd, qty, entry_fill_price,"
        " exit_fill_price, pnl_usd, exit_reason FROM positions WHERE status='closed'"
    ))
    gross = toll_assumed = toll_model = 0.0
    reasons: dict[str, int] = {}
    for r in rows:
        notional = float(r["notional_usd"] or 0.0)
        qty = float(r["qty"] or 0.0)
        gross += qty * (float(r["exit_fill_price"] or 0.0) - float(r["entry_fill_price"] or 0.0))
        toll_assumed += notional * assumed
        depth = _liquidity_at(conn, str(r["mint"] or ""), int(r["opened_at_ms"] or 0))
        toll_model += modelled_round_trip(notional, depth) * notional if depth else notional * assumed
        reasons[str(r["exit_reason"])] = reasons.get(str(r["exit_reason"]), 0) + 1
    return {
        "n_closed": len(rows),
        "gross": gross,
        "toll_assumed": toll_assumed,
        "toll_model": toll_model,
        "net_assumed": gross - toll_assumed,
        "net_model": gross - toll_model,
        "reasons": reasons,
    }


def compare(root: Path) -> list[dict]:
    """One row per ledger under runtime/memehunter, control group included."""
    runtime = root / "runtime" / "memehunter"
    if not runtime.is_dir():
        return []
    assumed = _config_cost(root)
    out = []
    for path in sorted(runtime.glob("*.sqlite")):
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            ).fetchone() is None:
                continue
            row = conn.execute("SELECT equity_usd FROM equity ORDER BY at_ms DESC LIMIT 1").fetchone()
            equity = float(row[0] or 0.0) if row else 0.0
            open_n = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
            label = path.name.replace(".sqlite", "")
            if path.name == DEFAULT_DB:
                label = "E 现役"
            elif path.name == CONTROL_DB:
                label = "C 对照组"
            out.append({
                "label": label,
                "file": path.name,
                "equity": equity,
                "open": int(open_n),
                **_book(conn, assumed),
            })
        finally:
            conn.close()
    return out


def format_compare(rows: list[dict]) -> str:
    if not rows:
        return "无账本（runtime/memehunter 下没有 .sqlite）"
    lines = ["账本横向对比（同一时刻，同列口径）"]
    lines.append(f"  {'账本':<14}{'权益':>9}{'持仓':>5}{'平仓':>5}{'毛利':>9}{'净(假设)':>10}{'净(模型)':>10}   退出分布")
    for r in rows:
        reasons = " ".join(f"{k}×{v}" for k, v in sorted(r["reasons"].items(), key=lambda kv: -kv[1]))
        lines.append(
            f"  {r['label']:<14}{r['equity']:>9.2f}{r['open']:>5}{r['n_closed']:>5}"
            f"{r['gross']:>+9.2f}{r['net_assumed']:>+10.2f}{r['net_model']:>+10.2f}   {reasons}"
        )
    lines.append("  毛利 = 价格变动；净(假设) 用配置 120bps，净(模型) 用池深度建模的往返成本。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    print(format_compare(compare(root)))
