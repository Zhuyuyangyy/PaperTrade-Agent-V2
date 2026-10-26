"""GROK: the orchestrator. It routes targets and records every hand-off.

GROK deliberately has no money powers. It cannot open or close a position, it
cannot change a risk verdict, and it cannot edit a candidate. All it does is run
the five worker agents in order, keep the artifacts they pass to each other, and
write an auditable record of what each agent decided.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from ..counterpart import CounterpartDesk
from ..memehunter import Desk
from .agents import AGENT_ORDER, AgentResult, Context, PIPELINE_VERSION, load_config, utc_now_ms

KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


PIPELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms INTEGER NOT NULL,
    finished_ms INTEGER,
    version INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pipeline_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    agent TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL,
    produced INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    decided_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_steps_run ON pipeline_steps(run_id);
CREATE TABLE IF NOT EXISTS pipeline_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    mint TEXT NOT NULL,
    score REAL,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    decided_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_targets_run ON pipeline_targets(run_id);
"""


class Pipeline:
    """Runs the six-agent desk once (or forever) against the E paper account."""

    def __init__(self, root: Path, config_path: Path | None = None):
        self.root = root
        self.config_path = config_path or (root / "user_data" / "config.pipeline.json")
        self.cfg = load_config(self.config_path)
        if self.cfg.get("mode") != "dry_run":
            raise ValueError("只允许 dry_run：本流水线不存在实盘下单路径")
        # The entry hard stop lives in the desk config so EXIT enforces it.
        self.desk_cfg = dict(self.cfg)
        self.desk_cfg["stop_loss_pct"] = float(self.cfg["entry"]["stop_loss_pct"])
        self.desk = Desk(root, self.desk_cfg)
        self.counterpart_result: dict | None = None
        self._counterpart_error: str | None = None
        self._ensure_schema()

    # ---------- ledger ----------
    def _ensure_schema(self) -> None:
        self.desk.conn.executescript(PIPELINE_SCHEMA)
        self.desk.conn.executescript(KV_SCHEMA)
        self.desk.conn.commit()

    def _kv_get(self, key: str, default: str = "0") -> str:
        row = self.desk.conn.execute("SELECT v FROM pipeline_kv WHERE k = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def _kv_set(self, key: str, value: str) -> None:
        self.desk.conn.execute(
            "INSERT INTO pipeline_kv(k, v) VALUES(?, ?)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
        self.desk.conn.commit()

    def _record(self, run_id: int, seq: int, result: AgentResult) -> None:
        self.desk.conn.execute(
            "INSERT INTO pipeline_steps (run_id, seq, agent, ok, summary, produced,"
            " rejected, elapsed_ms, detail, decided_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, seq, result.agent, 1 if result.ok else 0, result.summary,
                result.produced, result.rejected, result.elapsed_ms,
                json.dumps(result.detail, ensure_ascii=False, default=str)[:4000],
                utc_now_ms(),
            ),
        )

    def _record_targets(self, run_id: int, ctx: Context) -> None:
        rejects = ctx.get("rejects", {}) or {}
        for value, token in ctx.get("survivors", []):
            self.desk.conn.execute(
                "INSERT INTO pipeline_targets (run_id, symbol, mint, score, stage, outcome, decided_at_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, token.symbol, token.mint, round(value, 1), "RISK", "SURVIVOR", utc_now_ms()),
            )
        for entry in ctx.get("entries", []):
            self.desk.conn.execute(
                "INSERT INTO pipeline_targets (run_id, symbol, mint, score, stage, outcome, decided_at_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, entry.get("symbol", "?"), "", entry.get("score"), "FOMO", "OPENED", utc_now_ms()),
            )
        for reason, count in rejects.items():
            self.desk.conn.execute(
                "INSERT INTO pipeline_targets (run_id, symbol, mint, score, stage, outcome, decided_at_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, f"{reason} x{count}", "", None, "RISK", "VETOED", utc_now_ms()),
            )

    # ---------- run ----------
    def run_once(self) -> dict:
        started = utc_now_ms()
        ctx = Context(root=self.root, cfg=self.cfg, started_ms=started, desk=self.desk)
        # How many rounds in a row produced no survivor at all. RISK uses it to
        # decay the (soft) score floor; the veto list never moves.
        try:
            dry_streak = int(self._kv_get("dry_streak", "0"))
        except ValueError:
            dry_streak = 0
        ctx.put("dry_streak", dry_streak)
        cur = self.desk.conn.execute(
            "INSERT INTO pipeline_runs (started_ms, version, ok) VALUES (?,?,1)", (started, PIPELINE_VERSION)
        )
        run_id = int(cur.lastrowid)
        self.desk.conn.commit()

        results: list[AgentResult] = []
        for seq, agent_cls in enumerate(AGENT_ORDER, start=1):
            agent = agent_cls()
            try:
                result = agent.run(ctx)
            except Exception as exc:  # noqa: BLE001 - one broken agent must not stop the desk
                result = AgentResult(agent=agent.name, ok=False, summary=f"{type(exc).__name__}: {exc}")
            self._record(run_id, seq, result)
            results.append(result)
            # The control group sees exactly the candidates SEARCH just returned,
            # in the same round, so any C-vs-E gap is the pipeline's contribution.
            if agent.name == "RISK":
                produced = int(getattr(result, "produced", 0) or 0)
                self._kv_set("dry_streak", "0" if produced > 0 else str(dry_streak + 1))
            if agent.name == "SEARCH":
                # Shadow labels ride along with the scan: SEARCH already pulled
                # every price, so marking pending candidates costs no API call.
                try:
                    from ..replay.labels import mark_from_tokens
                    self.label_result = mark_from_tokens(self.desk.conn, ctx.get("tokens", []), ctx.started_ms)
                except Exception as exc:  # noqa: BLE001 - labelling must never stop the desk
                    self.label_error = f"{type(exc).__name__}: {exc}"
                if self.cfg.get("counterpart", {}).get("enabled"):
                    self._run_counterpart(ctx)

        self._record_targets(run_id, ctx)
        self.desk.conn.execute(
            "UPDATE pipeline_runs SET finished_ms=?, ok=? WHERE id=?",
            (utc_now_ms(), 1 if all(r.ok for r in results) else 0, run_id),
        )
        self.desk.conn.commit()

        equity = None
        try:
            equity = round(self.desk.cash() + sum(
                float(p["mark_price"] or 0.0) * float(p["qty"]) for p in self.desk.open_positions()
            ), 4)
        except sqlite3.Error:
            pass
        return {
            "run_id": run_id,
            "started_ms": started,
            "ok": all(r.ok for r in results),
            "steps": [r.__dict__ for r in results],
            "equity_usdt": equity,
        }

    def _run_counterpart(self, ctx: Context) -> None:
        """Run the C control group on the same candidate pool as E.

        Failures are logged to the counterpart ledger's heartbeat file rather
        than raised: a broken control group must never stop the main desk.
        """
        cfg = dict(self.cfg.get("counterpart") or {})
        cfg.pop("enabled", None)
        try:
            desk = CounterpartDesk(self.root, cfg)
        except Exception as exc:  # noqa: BLE001
            self._counterpart_error = f"{type(exc).__name__}: {exc}"
            return
        try:
            self.counterpart_result = desk.run_round(list(ctx.get("tokens", [])))
        except Exception as exc:  # noqa: BLE001
            self._counterpart_error = f"{type(exc).__name__}: {exc}"
        finally:
            desk.close()

    def close(self) -> None:
        try:
            self.desk.conn.close()
        except sqlite3.Error:
            pass


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Six-agent paper desk pipeline")
    parser.add_argument("command", choices=["once", "loop", "status"], nargs="?", default="once")
    parser.add_argument("--sleep", type=int, default=100, help="loop interval seconds")
    args = parser.parse_args(argv)

    root = _default_root()
    pipe = Pipeline(root)
    try:
        if args.command == "loop":
            import time
            from ..guards import guard_round
            log_path = root / "runtime" / "memehunter" / "loop.log"
            print(f"[pipeline] loop every {args.sleep}s · Ctrl-C or a STOP file to stop", flush=True)
            while True:
                may_run, note = guard_round(root, log_path)
                if not may_run:
                    print(f"[pipeline] halted: {note}", flush=True)
                    return 0
                if note:
                    print(f"[pipeline] {note}", flush=True)
                try:
                    summary = pipe.run_once()
                    for step in summary["steps"]:
                        flag = "ok " if step["ok"] else "ERR"
                        print(f"[{flag}] {step['agent']:<9} {step['summary']}", flush=True)
                    print(f"[run {summary['run_id']}] equity={summary['equity_usdt']}", flush=True)
                except Exception as exc:  # noqa: BLE001 - a bad round must not kill the loop
                    print(f"[loop] round failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(args.sleep)
        if args.command == "once":
            summary = pipe.run_once()
            for step in summary["steps"]:
                flag = "ok " if step["ok"] else "ERR"
                print(f"[{flag}] {step['agent']:<9} {step['summary']}")
            print(f"[run {summary['run_id']}] equity={summary['equity_usdt']}")
            return 0
        # status: latest run per agent
        rows = pipe.desk.conn.execute(
            "SELECT agent, ok, summary, produced, rejected, elapsed_ms, decided_at_ms"
            " FROM pipeline_steps WHERE run_id=(SELECT MAX(id) FROM pipeline_runs)"
            " ORDER BY seq"
        ).fetchall()
        if not rows:
            print("尚无流水线运行记录")
            return 0
        for row in rows:
            stamp = datetime.fromtimestamp(row[6] / 1000, timezone.utc).strftime("%H:%M:%S")
            print(f"{stamp} {row[0]:<9} {'ok ' if row[1] else 'ERR'} {row[2]} ({row[4]}ms)")
        return 0
    finally:
        pipe.close()


if __name__ == "__main__":
    sys.exit(main())
