"""Shadow label collection: what happened AFTER a candidate was scored.

The candidates table already stores the full snapshot at decision time (price,
liquidity, holders, organic score, veto verdict). What it never stored is what
the token did next. This module starts collecting that, from now on.

Two rules, both non-negotiable:

* Nothing is backfilled. A candidate that has no mark after its timestamp is
  reported as "unknown", never as a plausible return.
* Rejected candidates are sampled too, at a deterministic 1-in-N rate, so the
  veto funnel can be audited later ("what did the tokens RISK threw away do?").
  Eligible candidates are all marked.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..memehunter import _get  # public ticker, same source the desk prices with

MARKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_marks (
    candidate_id INTEGER NOT NULL,
    marked_at_ms INTEGER NOT NULL,
    price_usd REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'jupiter',
    PRIMARY KEY (candidate_id, marked_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_marks_candidate ON candidate_marks(candidate_id);
CREATE INDEX IF NOT EXISTS idx_marks_time ON candidate_marks(marked_at_ms);
"""

# Only tokens seen inside this window are worth another API round-trip.
DEFAULT_LOOKBACK_MINUTES = 70
# 1-in-N of rejected candidates gets a shadow price, deterministically by mint.
DEFAULT_REJECT_SAMPLE = 10
JUP_PRICE = "https://quote-api.jup.ag/price/v3"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MARKS_SCHEMA)
    conn.commit()


def _sample_key(mint: str) -> int:
    """Stable 0..N-1 bucket for a mint, so sampling is reproducible."""
    return sum(ord(ch) for ch in mint) % 10_000


def pending_candidates(
    conn: sqlite3.Connection, now_ms: int, lookback_minutes: int, reject_sample: int
) -> list[sqlite3.Row]:
    """Candidates from the lookback window that still need a shadow price.

    "Needs" means: no mark exists yet within the window after the candidate was
    scored, and the candidate is either eligible or selected by the deterministic
    sample.
    """
    cutoff = now_ms - lookback_minutes * 60_000
    rows = conn.execute(
        "SELECT c.id, c.mint, c.symbol, c.scanned_at_ms, c.eligible"
        " FROM candidates c"
        " WHERE c.scanned_at_ms >= ?"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM candidate_marks m"
        "     WHERE m.candidate_id = c.id AND m.marked_at_ms > c.scanned_at_ms"
        "   )",
        (cutoff,),
    ).fetchall()
    out = []
    for row in rows:
        if row["eligible"]:
            out.append(row)
        elif reject_sample > 0 and _sample_key(row["mint"]) % reject_sample == 0:
            out.append(row)
    return out


def quote_mints(mints: list[str], timeout: float = 20.0) -> dict[str, float]:
    """Live USD price per mint. Missing mints are simply absent, so callers can
    tell "no quote" from "price is zero"."""
    if not mints:
        return {}
    try:
        payload = _get(f"{JUP_PRICE}?ids={','.join(mints)}", timeout=timeout)
    except Exception:  # noqa: BLE001 - a failed quote is a skipped round, not a crash
        return {}
    out: dict[str, float] = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        for mint, row in data.items():
            price = (row or {}).get("price")
            if isinstance(price, (int, float)) and float(price) > 0:
                out[mint] = float(price)
    return out


def mark_from_tokens(conn: sqlite3.Connection, tokens: list, now_ms: int,
                     lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
                     reject_sample: int = DEFAULT_REJECT_SAMPLE) -> dict:
    """Shadow-mark pending candidates from prices the desk already fetched.

    SEARCH pulls the whole universe every round; reusing those prices costs no
    extra API call, so labelling rides along with the scan that produced the
    candidates in the first place.
    """
    ensure_schema(conn)
    by_mint = {t.mint: t for t in tokens if getattr(t, "price_usd", 0) > 0}
    pending = pending_candidates(conn, now_ms, lookback_minutes, reject_sample)
    marked = 0
    for row in pending:
        token = by_mint.get(row["mint"])
        if token is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO candidate_marks"
            " (candidate_id, marked_at_ms, price_usd, source) VALUES (?,?,?,?)",
            (row["id"], now_ms, float(token.price_usd), "scan-reuse"),
        )
        marked += 1
    conn.commit()
    return {"pending": len(pending), "marked": marked, "source": "scan-reuse"}


def collect_once(
    conn: sqlite3.Connection,
    now_ms: int,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    reject_sample: int = DEFAULT_REJECT_SAMPLE,
    batch: int = 100,
) -> dict:
    """Add one shadow price per pending candidate. Returns what happened."""
    ensure_schema(conn)
    pending = pending_candidates(conn, now_ms, lookback_minutes, reject_sample)
    marked, skipped = 0, 0
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        quotes = quote_mints([row["mint"] for row in chunk])
        for row in chunk:
            price = quotes.get(row["mint"])
            if price is None:
                skipped += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO candidate_marks"
                " (candidate_id, marked_at_ms, price_usd, source) VALUES (?,?,?,?)",
                (row["id"], now_ms, price, "jupiter"),
            )
            marked += 1
    conn.commit()
    return {
        "pending": len(pending),
        "marked": marked,
        "no_quote": skipped,
        "lookback_minutes": lookback_minutes,
        "reject_sample": reject_sample,
    }


def candidate_returns(conn: sqlite3.Connection, candidate_id: int) -> dict:
    """Forward returns for one candidate, in minutes, from its own marks.

    Only horizons with a mark at or after that horizon are reported; the rest
    stay absent. There is no interpolation and no carrying forward.
    """
    # Index access, not row["col"]: callers may open the ledger without a
    # row_factory and a replay must never depend on the caller's cursor setup.
    row = conn.execute(
        "SELECT scanned_at_ms, price_usd FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if not row or not row[1]:
        return {}
    base_t, base_p = int(row[0]), float(row[1])
    marks = conn.execute(
        "SELECT marked_at_ms, price_usd FROM candidate_marks"
        " WHERE candidate_id = ? AND marked_at_ms > ? ORDER BY marked_at_ms",
        (candidate_id, base_t),
    ).fetchall()
    if not marks:
        return {}
    out: dict[str, float] = {}
    for horizon_min in (5, 15, 30, 60):
        target = base_t + horizon_min * 60_000
        # A mark counts for this horizon only if it is at or past it and within
        # a tolerance of half the horizon (minimum 5 minutes). Otherwise a
        # 16-minute-old price would be reported as the 5-minute return, which
        # is a different number wearing the wrong label.
        tolerance = max(horizon_min * 0.5, 5.0) * 60_000
        best = None
        for mark in marks:
            mark_t = int(mark[0])
            if mark_t >= target and mark_t - target <= tolerance:
                best = mark
                break
        if best is not None:
            out[f"{horizon_min}m"] = round((float(best[1]) / base_p - 1.0) * 100.0, 4)
    out["marks"] = len(marks)
    return out


def label_coverage(conn: sqlite3.Connection) -> dict:
    """How much of the candidate history has any forward price at all."""
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    with_marks = conn.execute(
        "SELECT COUNT(DISTINCT candidate_id) FROM candidate_marks"
    ).fetchone()[0]
    eligible = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE eligible = 1"
    ).fetchone()[0]
    eligible_marked = conn.execute(
        "SELECT COUNT(DISTINCT c.id) FROM candidates c"
        " JOIN candidate_marks m ON m.candidate_id = c.id WHERE c.eligible = 1"
    ).fetchone()[0]
    return {
        "candidates": total,
        "with_marks": with_marks,
        "eligible": eligible,
        "eligible_with_marks": eligible_marked,
        "coverage_pct": round(with_marks / total * 100.0, 2) if total else 0.0,
    }


def default_db(root: Path) -> Path:
    return root / "runtime" / "memehunter" / "memehunter.sqlite"
