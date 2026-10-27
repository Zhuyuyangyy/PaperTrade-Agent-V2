"""Money-flow shadow signals: what real on-chain money is buying, recorded
without trading it.

The operator's idea, made falsifiable: D follows wallets that spend real money
on-chain; E picks by organic score. Every symbol those wallets buy is a signal
E never saw. Recording them as SHADOWS -- no position, no order -- lets the
replay answer "would the money-flow signal have paid better than our picks?"
before any strategy change is made.

Two rules inherited from research/replay/signals.py:
- use what D OBSERVED (swaps), never what D filled: D's own gates reject most
  of what it sees, and "D did not pick it" is not evidence the token was bad
- the base price is D's own recorded fill price, so no backfill is invented

Forward prices come from E's shadow marks (the same prices calibration uses).
A symbol that never entered E's universe has no marks and stays unknown --
unknown is reported, never assumed.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS moneyflow_signals (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    wallet TEXT NOT NULL,
    usd REAL NOT NULL,
    base_price REAL NOT NULL,
    seen_at_ms INTEGER NOT NULL,
    UNIQUE (symbol, wallet, seen_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_mf_symbol ON moneyflow_signals(symbol, seen_at_ms);
CREATE TABLE IF NOT EXISTS moneyflow_marks (
    signal_id INTEGER NOT NULL,
    marked_at_ms INTEGER NOT NULL,
    price_usd REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'scan-reuse',
    PRIMARY KEY (signal_id, marked_at_ms)
);
"""

HORIZONS = (5, 15, 30, 60)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def collect_signals(root: Path, window_hours: int = 6, dedupe_minutes: int = 60) -> dict:
    """Record tracked-wallet buys from D's ledger as shadow signals in E's."""
    import shutil as _sh

    cw = root / "runtime" / "copywatch" / "copywatch.sqlite"
    if not cw.is_file():
        return {"recorded": 0, "why": "copywatch ledger missing"}
    tmp_dir = Path(tempfile.mkdtemp(prefix="mf_collect_"))
    tmp_db = tmp_dir / "cw.sqlite"
    _sh.copy(cw, tmp_db)
    src = sqlite3.connect(str(tmp_db))
    src.row_factory = sqlite3.Row
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff = now_ms - window_hours * 3_600_000
    rows = src.execute(
        "SELECT s.symbol, s.wallet, s.usd, s.price_usd, s.block_ts_ms"
        " FROM swaps s JOIN wallets w ON w.address = s.wallet AND w.network = s.network"
        " WHERE s.kind = 'buy' AND w.tracked = 1 AND s.block_ts_ms >= ?"
        "   AND s.price_usd > 0 AND s.usd > 0"
        " ORDER BY s.block_ts_ms",
        (cutoff,),
    ).fetchall()
    src.close()
    _sh.rmtree(tmp_dir, ignore_errors=True)

    # One signal per (symbol, wallet) per dedupe window: a wallet buying the
    # same token three times in an hour is one opinion, not three.
    seen: dict[tuple[str, str], int] = {}
    kept: list[tuple] = []
    for row in rows:
        key = (row["symbol"], row["wallet"])
        last = seen.get(key)
        if last is not None and int(row["block_ts_ms"]) - last < dedupe_minutes * 60_000:
            continue
        seen[key] = int(row["block_ts_ms"])
        kept.append((row["symbol"], row["wallet"], float(row["usd"]),
                     float(row["price_usd"]), int(row["block_ts_ms"])))

    mh = root / "runtime" / "memehunter" / "memehunter.sqlite"
    conn = sqlite3.connect(str(mh), timeout=30.0)
    ensure_schema(conn)
    recorded = 0
    for symbol, wallet, usd, price, ts in kept:
        cur = conn.execute(
            "INSERT OR IGNORE INTO moneyflow_signals (symbol, wallet, usd, base_price, seen_at_ms)"
            " VALUES (?,?,?,?,?)",
            (symbol, wallet, usd, price, ts),
        )
        recorded += cur.rowcount if cur.rowcount > 0 else 0
    conn.commit()
    conn.close()
    return {"recorded": recorded, "observed": len(rows), "deduped": len(kept),
            "window_hours": window_hours}


def mark_signals(root: Path, lookback_minutes: int = 70) -> dict:
    """Attach E's shadow prices to money-flow signals, same cadence as labels."""
    mh = root / "runtime" / "memehunter" / "memehunter.sqlite"
    if not mh.is_file():
        return {"marked": 0, "why": "memehunter ledger missing"}
    conn = sqlite3.connect(str(mh), timeout=30.0)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    stale_before = now_ms - 90_000
    cutoff = now_ms - lookback_minutes * 60_000

    # For every symbol E has seen, the newest candidate id per (symbol, round)
    # is what candidate_marks hangs off; take the most recent mark per symbol
    # at or after the signal as the forward price.
    pending = conn.execute(
        "SELECT s.id, s.symbol, s.seen_at_ms FROM moneyflow_signals s"
        " WHERE s.seen_at_ms >= ?"
        "   AND NOT EXISTS (SELECT 1 FROM moneyflow_marks m"
        "                   WHERE m.signal_id = s.id AND m.marked_at_ms >= ?)",
        (cutoff, stale_before),
    ).fetchall()
    marked = 0
    for row in pending:
        # Newest mark for this symbol at or after the signal time.
        mark = conn.execute(
            "SELECT m.marked_at_ms, m.price_usd FROM candidate_marks m"
            " JOIN candidates c ON c.id = m.candidate_id"
            " WHERE c.symbol = ? AND m.marked_at_ms >= ?"
            " ORDER BY m.marked_at_ms LIMIT 1",
            (row["symbol"], row["seen_at_ms"]),
        ).fetchone()
        if not mark:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO moneyflow_marks (signal_id, marked_at_ms, price_usd, source)"
            " VALUES (?,?,?,?)",
            (row["id"], int(mark["marked_at_ms"]), float(mark["price_usd"]), "scan-reuse"),
        )
        marked += 1
    conn.commit()
    conn.close()
    return {"pending": len(pending), "marked": marked}


def compare_sources(root: Path) -> dict:
    """Forward returns of money-flow shadows vs E's own fills, same horizons."""
    empty_side = {"n": 0, "mean": None, "median": None, "hit": None}
    mh = root / "runtime" / "memehunter" / "memehunter.sqlite"
    if not mh.is_file():
        out = {f"{h}m": {"moneyflow": dict(empty_side), "e_picks": dict(empty_side)} for h in HORIZONS}
        out["note"] = "memehunter ledger missing: unknown, not zero"
        return out
    conn = sqlite3.connect(str(mh), timeout=30.0)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    # Money-flow side: marks per signal.
    mf = []
    for row in conn.execute(
        "SELECT s.id, s.symbol, s.base_price, s.seen_at_ms FROM moneyflow_signals s"
    ).fetchall():
        marks = conn.execute(
            "SELECT marked_at_ms, price_usd FROM moneyflow_marks WHERE signal_id = ?"
            " ORDER BY marked_at_ms", (row["id"],)
        ).fetchall()
        for horizon in HORIZONS:
            target = int(row["seen_at_ms"]) + horizon * 60_000
            tol = max(horizon * 0.5, 5.0) * 60_000
            for m in marks:
                t = int(m["marked_at_ms"])
                if t >= target and t - target <= tol:
                    mf.append({
                        "side": "D-moneyflow", "symbol": row["symbol"], "h": horizon,
                        "ret": round((float(m["price_usd"]) / float(row["base_price"]) - 1.0) * 100.0, 3),
                    })
                    break

    # E side: its own fills, from candidate_marks on the matched candidate.
    e_side = []
    for pos in conn.execute(
        "SELECT symbol, opened_at_ms, entry_fill_price FROM positions WHERE status='closed'"
    ).fetchall():
        cand = conn.execute(
            "SELECT id FROM candidates WHERE symbol = ? AND scanned_at_ms BETWEEN ? AND ?"
            " ORDER BY scanned_at_ms LIMIT 1",
            (pos["symbol"], int(pos["opened_at_ms"]) - 300_000, int(pos["opened_at_ms"]) + 300_000),
        ).fetchone()
        if not cand:
            continue
        marks = conn.execute(
            "SELECT marked_at_ms, price_usd FROM candidate_marks WHERE candidate_id = ?"
            " AND marked_at_ms > ? ORDER BY marked_at_ms",
            (int(cand["id"]), int(pos["opened_at_ms"])),
        ).fetchall()
        for horizon in HORIZONS:
            target = int(pos["opened_at_ms"]) + horizon * 60_000
            tol = max(horizon * 0.5, 5.0) * 60_000
            for m in marks:
                t = int(m["marked_at_ms"])
                if t >= target and t - target <= tol:
                    e_side.append({
                        "side": "E-picks", "symbol": pos["symbol"], "h": horizon,
                        "ret": round((float(m["price_usd"]) / float(pos["entry_fill_price"]) - 1.0) * 100.0, 3),
                    })
                    break
    conn.close()

    def summarise(rows):
        if not rows:
            return {"n": 0, "mean": None, "median": None, "hit": None}
        rets = sorted(r["ret"] for r in rows)
        mid = len(rets) // 2
        median = rets[mid] if len(rets) % 2 else (rets[mid - 1] + rets[mid]) / 2
        return {
            "n": len(rets),
            "mean": round(sum(rets) / len(rets), 3),
            "median": round(median, 3),
            "hit": round(sum(1 for r in rets if r > 0) / len(rets) * 100.0, 1),
        }

    out = {}
    for horizon in HORIZONS:
        out[f"{horizon}m"] = {
            "moneyflow": summarise([r for r in mf if r["h"] == horizon]),
            "e_picks": summarise([r for r in e_side if r["h"] == horizon]),
        }
    out["note"] = ("D-moneyflow uses D's observed buys at their own fill price; E-picks uses "
                   "E's actual entries. Both forward-priced from the same shadow marks. "
                   "Samples accumulate from 2026-10-27 08:53 onward.")
    return out


def format_compare(report: dict) -> str:
    lines = ["资金流 vs 自选（前向收益对比，同口径影子价）"]
    lines.append("  期限   资金流 n/均值/命中        自选 n/均值/命中")
    for horizon in HORIZONS:
        cell = report.get(f"{horizon}m") or {}
        mf, ep = cell.get("moneyflow", {}), cell.get("e_picks", {})
        fmt = lambda d: ("%4d %+7.2f%% %5.1f%%" % (d["n"], d["mean"] or 0, d["hit"] or 0)) if d.get("n") else "   — 无样本 —"
        lines.append(f"  {horizon:<4}m  {fmt(mf)}   {fmt(ep)}")
    lines.append("  " + report.get("note", ""))
    return "\n".join(lines)
