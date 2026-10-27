"""One JSON state for the office wall: every wallet and every agent, right now.

The dashboard used to be a page that rebuilt itself on a 15-second meta
refresh. That is fine for a report and useless for watching a desk: you cannot
see a step happen, only its aftermath. This is the feed the office page polls
instead -- five books side by side, and for each book the six agents with
whatever they last did.

Read-only, loopback, and it never touches a config or an order path.
"""
from __future__ import annotations

import json
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


# What each book is, in plain words. The arms carry their own file; these two
# are the baseline and the control, and they need saying too -- a row of
# unnamed ledgers is how an experiment stops being readable.
BOOK_NOTES = {
    DEFAULT_DB: "六 agent 全开：扫盘 → 否决打分 → 聪明钱 → LLM 研判 → 开仓 → 盯盘。"
                "所有实验臂的基线，改任何一个参数之前先看它。",
    CONTROL_DB: "同一个候选池、同一时刻，唯一规则：按 organic_score 取前 3 买入——"
                "没有否决、没有分数线、没有聪明钱加成、没有冷却。"
                "E 与 C 的差就是六 agent 管线带来的价值，或者证明它没带来。",
}


def _arm_info(root: Path | None, arm: str) -> dict | None:
    """An arm's hypothesis and falsify rule, read from its config file."""
    if not arm:
        return None
    path = (root or ROOT) / "user_data" / f"config.{arm}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    info = data.get("_arm")
    return info if isinstance(info, dict) else None


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


def _label(path: Path, root: Path | None = None) -> str:
    """What the operator sees. An arm names itself in its own config, so the
    page never has to explain a bare file name."""
    if path.name == DEFAULT_DB:
        return "E 现役"
    if path.name == CONTROL_DB:
        return "C 对照组"
    info = _arm_info(root, path.name.replace(".sqlite", ""))
    if info and info.get("label"):
        return str(info["label"])
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


def _history(conn: sqlite3.Connection, points: int = 60) -> list[list[float]]:
    """Equity over time, thinned to `points`, oldest first.

    A wallet card with only a current number cannot show whether an arm has
    been leading all night or caught up in the last hour, which is the entire
    question the comparison exists to answer.
    """
    rows = list(conn.execute(
        "SELECT at_ms, equity_usd FROM equity ORDER BY at_ms DESC LIMIT 3000"
    ))
    if not rows:
        return []
    rows.reverse()
    # Even thinning across two days leaves five points in the last hour, which
    # is useless for the one-hour window the chart opens on. Keep the newest
    # stretch at full resolution and thin only the older part.
    recent_n = min(len(rows), 150)
    recent = rows[-recent_n:]
    older = rows[: len(rows) - recent_n]
    out: list[list[float]] = []
    if older:
        step = max(1, len(older) // max(1, points - recent_n))
        out += [[int(r[0]), round(float(r[1] or 0), 2)] for r in older[::step]]
    out += [[int(r[0]), round(float(r[1] or 0), 2)] for r in recent]
    return out


def _feed(conn: sqlite3.Connection, label: str, limit: int = 6) -> list[dict]:
    """The last few things this book did: agent steps and closed trades."""
    events: list[dict] = []
    try:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_steps'"
        ).fetchone()
        if has:
            for r in conn.execute(
                "SELECT agent, ok, summary, decided_at_ms FROM pipeline_steps"
                " ORDER BY id DESC LIMIT ?", (limit,),
            ):
                events.append({
                    "wallet": label, "kind": "step", "who": str(r[0]),
                    "text": str(r[1] and r[2] or (r[2] or "")), "ok": bool(r[1]),
                    "ms": int(r[3] or 0),
                })
    except sqlite3.Error:
        pass
    try:
        for r in conn.execute(
            "SELECT symbol, exit_reason, pnl_usd, exit_at_ms FROM positions"
            " WHERE status='closed' AND exit_at_ms IS NOT NULL ORDER BY exit_at_ms DESC LIMIT ?",
            (limit,),
        ):
            events.append({
                "wallet": label, "kind": "trade", "who": str(r[0]),
                "text": f"{r[1]} · 净 {float(r[2] or 0):+.2f}",
                "ok": float(r[2] or 0) > 0, "ms": int(r[3] or 0),
            })
    except sqlite3.Error:
        pass
    events.sort(key=lambda e: e["ms"], reverse=True)
    return events[:limit]


def _trades(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """The closed-trade ledger for the detail panel: what was bought, how long
    it was held, why it ended, and what it paid."""
    out = []
    for r in conn.execute(
        "SELECT symbol, notional_usd, opened_at_ms, exit_at_ms, exit_reason, pnl_usd, pnl_pct"
        " FROM positions WHERE status='closed' AND exit_at_ms IS NOT NULL"
        " ORDER BY exit_at_ms DESC LIMIT ?", (limit,),
    ):
        opened = int(r[2] or 0)
        exit_at = int(r[3] or 0)
        out.append({
            "symbol": str(r[0]),
            "notional": round(float(r[1] or 0), 0),
            "opened_ms": opened,
            "exit_ms": exit_at,
            "hold_min": round((exit_at - opened) / 60000) if opened and exit_at else 0,
            "reason": str(r[4]),
            "pnl": round(float(r[5] or 0), 2),
            "pct": round(float(r[6] or 0), 1),
        })
    return out


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
    feed: list[dict] = []
    for path in _ledgers(root):
        try:
            conn = _connect(path)
        except sqlite3.Error:
            # A book that cannot be read for a moment still shows up, flagged.
            # Silently dropping it is how an experiment quietly loses a row.
            wallets.append({
                "label": _label(path, root), "file": path.name, "unavailable": True,
                "equity": 0.0, "open": 0, "closed": 0, "reasons": {}, "positions": [],
            })
            continue
        try:
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            ).fetchone() is None:
                continue
            wallet = _wallet(conn)
            label = _label(path, root)
            wallets.append({
                "label": label,
                "file": path.name,
                "arm": "" if path.name == DEFAULT_DB else path.name.replace(".sqlite", ""),
                **wallet,
                **_agents(conn),
                "history": _history(conn),
                "trades": _trades(conn),
                "note": BOOK_NOTES.get(path.name, ""),
                "arm_info": _arm_info(root, path.name.replace(".sqlite", "") if path.name != DEFAULT_DB else ""),
            })
            feed.extend(_feed(conn, label))
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    # Baseline and control first, then the arms -- a reader should hit the
    # thing being measured before the things measuring it.
    order = {DEFAULT_DB: 0, CONTROL_DB: 1}
    wallets.sort(key=lambda w: (order.get(w["file"], 2), w["label"]))
    feed.sort(key=lambda e: e["ms"], reverse=True)
    return {
        "now_ms": int(time.time() * 1000),
        "agent_order": AGENT_ORDER,
        "agent_jobs": AGENT_JOBS,
        "wallets": wallets,
        "feed": feed[:14],
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_state(), ensure_ascii=False, indent=2))
