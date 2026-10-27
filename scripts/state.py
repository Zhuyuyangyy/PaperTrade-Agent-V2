"""One JSON state for the office wall: every wallet and every agent, right now.

The dashboard used to be a page that rebuilt itself on a 15-second meta
refresh. That is fine for a report and useless for watching a desk: you cannot
see a step happen, only its aftermath. This is the feed the office page polls
instead -- five books side by side, and for each book the six agents with
whatever they last did.

Read-only, loopback, and it never touches a config or an order path.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "memehunter"

DEFAULT_DB = "memehunter.sqlite"
CONTROL_DB = "counterpart.sqlite"

AGENT_ORDER = ["SEARCH", "RISK", "GMGN", "JUDGE", "FOMO", "EXIT"]
# What each desk is for, in the words of the operator rather than the code.
AGENT_JOBS = {
    "SEARCH": "扫盘：把 universe 拉进来",
    "RISK": "否决 + 打分：硬门槛一票否决",
    "GMGN": "聪明钱：链上地址在买什么",
    "JUDGE": "研判：给候选打结构分",
    "FOMO": "开仓：按分数与额度下单",
    "EXIT": "盯盘：标记、止盈止损、超时",
}
STARTING_CASH = 1000.0


def _connect(path: Path) -> sqlite3.Connection:
    """Open a live ledger read-only, retrying.

    Four processes are writing these files at once. On Windows a reader can
    still hit SQLITE_CANTOPEN on the -shm file for a moment, so the feed
    retries a couple of times before giving up on one ledger -- a single book
    dropping out for three seconds is fine, the whole page 500-ing is not.
    """
    uri = f"file:{path.as_posix()}?mode=ro"
    last: Exception | None = None
    for _ in range(3):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:  # noqa: PERF203 - bounded retry
            last = exc
            time.sleep(0.4)
    raise sqlite3.OperationalError(f"cannot open {path.name}: {last}")


def _ledgers(root: Path | None = None) -> list[Path]:
    runtime = (root or ROOT) / "runtime" / "memehunter"
    if not runtime.is_dir():
        return []
    return sorted(p for p in runtime.glob("*.sqlite") if not p.name.endswith(("-wal", "-shm")))


def _label(path: Path) -> str:
    if path.name == DEFAULT_DB:
        return "E 现役"
    if path.name == CONTROL_DB:
        return "C 对照组"
    return path.name.replace(".sqlite", "")


def _wallet(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT cash_usd, positions_usd, equity_usd, at_ms FROM equity ORDER BY at_ms DESC LIMIT 1").fetchone()
    equity = float(row[2]) if row else STARTING_CASH
    cash = float(row[0]) if row else STARTING_CASH
    holdings = float(row[1]) if row else 0.0
    at_ms = int(row[3]) if row else 0

    open_rows = list(conn.execute(
        "SELECT symbol, notional_usd, entry_fill_price, mark_price, qty FROM positions WHERE status='open'"
    ))
    closed = list(conn.execute(
        "SELECT notional_usd, qty, entry_fill_price, exit_fill_price, pnl_usd, exit_reason, exit_at_ms"
        " FROM positions WHERE status='closed'"
    ))
    gross = sum(float(r[1] or 0) * (float(r[3] or 0) - float(r[2] or 0)) for r in closed)
    net = sum(float(r[4] or 0) for r in closed)
    reasons: dict[str, int] = {}
    for r in closed:
        reasons[str(r[5])] = reasons.get(str(r[5]), 0) + 1
    unreal = sum(
        (float(r[3] or 0) - float(r[2] or 0)) * float(r[4] or 0) for r in open_rows
    )
    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "holdings": round(holdings, 2),
        "pnl_vs_start": round(equity - STARTING_CASH, 2),
        "pnl_pct": round((equity / STARTING_CASH - 1) * 100, 2),
        "open": len(open_rows),
        "closed": len(closed),
        "gross": round(gross, 2),
        "realized_net": round(net, 2),
        "unrealized": round(unreal, 2),
        "reasons": reasons,
        "positions": [
            {"symbol": str(r[0]), "notional": round(float(r[1] or 0), 2),
             "pct": round((float(r[3] or 0) / float(r[2] or 1) - 1) * 100, 1)}
            for r in open_rows[:8]
        ],
        "at_ms": at_ms,
    }


def _agents(conn: sqlite3.Connection) -> dict:
    """The latest round's six steps, plus when the desk last moved.

    The control group is a plain Desk ledger with no pipeline tables -- it is
    driven by the pipeline but keeps no run rows. That is not an error, it
    just has no agents, and the office view filters those out when it builds
    its tabs.
    """
    has = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_runs'"
    ).fetchone()
    if not has:
        return {"run_id": None, "steps": {}, "started_ms": None, "finished_ms": None}
    # The newest run row may still be in flight with no steps written yet; an
    # empty office for ninety seconds is worse than showing the last finished
    # round, so prefer the newest run that actually has steps.
    run = conn.execute(
        "SELECT id, started_ms, finished_ms FROM pipeline_runs"
        " WHERE id IN (SELECT run_id FROM pipeline_steps) ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not run:
        return {"run_id": None, "steps": {}, "started_ms": None, "finished_ms": None}
    steps = {}
    for r in conn.execute(
        "SELECT seq, agent, ok, summary, elapsed_ms, decided_at_ms FROM pipeline_steps"
        " WHERE run_id = ? ORDER BY seq", (int(run[0]),),
    ):
        steps[str(r[1])] = {
            "seq": int(r[0]),
            "ok": bool(r[2]),
            "summary": str(r[3] or ""),
            "elapsed_ms": int(r[4] or 0),
            "decided_at_ms": int(r[5] or 0),
        }
    return {
        "run_id": int(run[0]),
        "started_ms": int(run[1]) if run[1] else None,
        "finished_ms": int(run[2]) if run[2] else None,
        "steps": steps,
    }


def build_state(root: Path | None = None) -> dict:
    """Every wallet and every agent's last move. `root` exists for tests."""
    wallets = []
    for path in _ledgers(root):
        try:
            conn = _connect(path)
        except sqlite3.Error:
            # A book that cannot be read for a moment still shows up, flagged.
            # Silently dropping it is how an experiment quietly loses a row.
            wallets.append({
                "label": _label(path), "file": path.name, "unavailable": True,
                "equity": 0.0, "open": 0, "closed": 0, "reasons": {}, "positions": [],
            })
            continue
        try:
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            ).fetchone() is None:
                continue
            wallets.append({
                "label": _label(path),
                "file": path.name,
                "arm": "" if path.name == DEFAULT_DB else path.name.replace(".sqlite", ""),
                **_wallet(conn),
                **_agents(conn),
            })
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return {
        "now_ms": int(time.time() * 1000),
        "agent_order": AGENT_ORDER,
        "agent_jobs": AGENT_JOBS,
        "wallets": wallets,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_state(), ensure_ascii=False, indent=2))
