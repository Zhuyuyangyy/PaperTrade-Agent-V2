"""Counterfactual timeout replay: what would the SAME trades have earned under
a different TIMEOUT?

This is how a timeout gets calibrated -- not from how dead positions feel, but
from what the recorded shadow prices say the exit would have paid. For every
closed E trade we rebuild its price path from candidate_marks (the same prices
the calibration uses, never a later one than the simulated clock) and re-exit
it at 45/90/180/360 minutes, keeping the real exit whenever STOP_LOSS or
TAKE_PROFIT fired first.

Costs mirror the live desk: round_trip_cost_bps (default 120) split across
entry and exit, so a counterfactual exit pays the same toll the real one did.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

TIMEOUTS = (45, 90, 180, 360)


def _price_path(conn: sqlite3.Connection, symbol: str, opened_ms: int, entry: float) -> list[tuple[int, float]]:
    """(t, price) marks for the candidate that matches this fill, in order."""
    row = conn.execute(
        "SELECT id FROM candidates WHERE symbol = ? AND scanned_at_ms BETWEEN ? AND ?"
        " ORDER BY scanned_at_ms LIMIT 1",
        (symbol, opened_ms - 300_000, opened_ms + 300_000),
    ).fetchone()
    if not row:
        return []
    return [
        (int(t), float(p))
        for t, p in conn.execute(
            "SELECT marked_at_ms, price_usd FROM candidate_marks"
            " WHERE candidate_id = ? AND marked_at_ms > ? ORDER BY marked_at_ms",
            (int(row[0]), opened_ms),
        ).fetchall()
        if p
    ]


def _exit_value(path: list[tuple[int, float]], at_ms: int) -> tuple[float, bool] | None:
    """(price, was_last_visible) for the deadline.

    First price at or after at_ms is the honest fill. If the token stopped
    being quoted before the deadline, the last visible price is used instead of
    skipping the trade: a token that vanished from the universe is exactly the
    outcome a shorter timeout would have avoided, and skipping it would bias
    the whole comparison toward longer holds.
    """
    for t, p in path:
        if t >= at_ms:
            return p, False
    if path:
        return path[-1][1], True
    return None


def counterfactual(root: Path, db_name: str = "memehunter.sqlite") -> dict:
    # The live ledger is WAL mode; a read-only URI open refuses it, and copying
    # is also the honest thing to do -- a replay must not race the desk.
    import shutil
    import tempfile

    src = root / "runtime" / "memehunter" / db_name
    tmp_dir = Path(tempfile.mkdtemp(prefix="timeout_replay_"))
    tmp_db = tmp_dir / db_name
    shutil.copy(src, tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    try:
        cfg = {}
        cfg_path = root / "user_data" / "config.memehunter.json"
        if cfg_path.is_file():
            import json

            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        rt_bps = float(cfg.get("round_trip_cost_bps", 120.0))
        half_bps = rt_bps / 2.0 / 10_000.0

        rows = conn.execute(
            "SELECT symbol, opened_at_ms, entry_fill_price, qty, entry_cost_usd,"
            " exit_at_ms, exit_reason, pnl_usd FROM positions WHERE status='closed'"
            " ORDER BY opened_at_ms"
        ).fetchall()
    finally:
        pass

    per_timeout: dict[int, list[float]] = {t: [] for t in TIMEOUTS}
    skipped = 0
    vanished_count = 0
    for row in rows:
        path = _price_path(conn, row["symbol"], int(row["opened_at_ms"]), float(row["entry_fill_price"] or 0))
        opened = int(row["opened_at_ms"])
        real_reason = row["exit_reason"]
        real_exit = int(row["exit_at_ms"] or 0)
        for minutes in TIMEOUTS:
            limit = opened + minutes * 60_000
            # A risk exit that already fired inside this window keeps its real
            # outcome: the counterfactual only moves the deadline, not the stop.
            if real_reason in ("STOP_LOSS", "TAKE_PROFIT") and real_exit <= limit:
                pnl = float(row["pnl_usd"] or 0.0)
            elif real_reason in ("STOP_LOSS", "TAKE_PROFIT"):
                # The stop would have fired before this deadline anyway; the
                # recorded pnl is the outcome, the timeout never applied.
                pnl = float(row["pnl_usd"] or 0.0)
            else:
                found = _exit_value(path, limit)
                if found is None:
                    skipped += 1
                    continue  # no price at or after the deadline: unknown
                price, vanished = found
                qty = float(row["qty"] or 0.0)
                entry = float(row["entry_fill_price"] or 0.0)
                # A vanished token pays the last visible price -- the desk would
                # have had to sell into whatever was left.
                fill = price * (1 - half_bps)
                pnl = qty * (fill - entry) - float(row["entry_cost_usd"] or 0.0) - qty * price * half_bps
                if vanished:
                    vanished_count += 1
            per_timeout[minutes].append(pnl)

    conn.close()
    shutil.rmtree(tmp_dir, ignore_errors=True)

    out = {}
    for minutes in TIMEOUTS:
        vals = per_timeout[minutes]
        if not vals:
            out[f"{minutes}m"] = {"n": 0}
            continue
        wins = sum(1 for v in vals if v > 0)
        gains = sum(v for v in vals if v > 0)
        losses = -sum(v for v in vals if v < 0)
        out[f"{minutes}m"] = {
            "n": len(vals),
            "net_usd": round(sum(vals), 2),
            "mean_usd": round(sum(vals) / len(vals), 3),
            "win_rate_pct": round(wins / len(vals) * 100, 1),
            "profit_factor": round(gains / losses, 2) if losses > 0 else None,
        }
    out["skipped_no_price"] = skipped
    out["vanished_priced_at_last"] = vanished_count
    out["note"] = (
        "STOP_LOSS/TAKE_PROFIT keep their recorded outcome; only the deadline moves. "
        "Sample is NOT unbiased: only trades whose candidate has shadow marks (recorded "
        "since 08:53) can be replayed, and those skew toward tokens that stayed in the "
        "universe. Directional only."
    )
    return out


def format_report(report: dict) -> str:
    lines = ["TIMEOUT 反事实回放（同一批交易，只改平仓期限）"]
    lines.append("  期限    笔数   净盈亏     每笔均值   胜率    盈亏比")
    for key in (f"{t}m" for t in TIMEOUTS):
        r = report.get(key) or {}
        if not r.get("n"):
            lines.append(f"  {key:<6}   0")
            continue
        pf = f"{r['profit_factor']:.2f}" if r.get("profit_factor") else "  - "
        lines.append(
            f"  {key:<6} {r['n']:>4}  {r['net_usd']:>+8.2f}  {r['mean_usd']:>+8.3f}  "
            f"{r['win_rate_pct']:>5.1f}%  {pf:>6}"
        )
    if report.get("skipped_no_price"):
        lines.append(f"  （{report['skipped_no_price']} 笔因缺少到期价格未计入）")
    lines.append("  " + report.get("note", ""))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    print(format_report(counterfactual(root)))
