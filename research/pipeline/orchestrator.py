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
from .roundlock import acquire as acquire_round, release as release_round

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
    """Runs the six-agent desk once (or forever) against a paper account.

    `db_name` is what makes an experiment arm possible: every arm runs the
    same six agents over the same universe at the same moment, but writes to
    its own ledger, so a parameter can be varied one at a time without the
    arms contaminating each other. The name is recorded in the run row, so a
    report can always tell which arm a number came from.
    """

    def __init__(self, root: Path, config_path: Path | None = None, db_name: str = "memehunter.sqlite"):
        self.root = root
        self.db_name = db_name
        # The arm is the ledger's name, so a report can always tell which
        # experiment a run row belongs to. Empty for the live desk.
        self.arm_name = "" if db_name == "memehunter.sqlite" else db_name.replace(".sqlite", "")
        self.config_path = config_path or (root / "user_data" / "config.pipeline.json")
        self.cfg = load_config(self.config_path)
        if self.cfg.get("mode") != "dry_run":
            raise ValueError("只允许 dry_run：本流水线不存在实盘下单路径")
        # The entry hard stop lives in the desk config so EXIT enforces it.
        self.desk_cfg = dict(self.cfg)
        self.desk_cfg["stop_loss_pct"] = float(self.cfg["entry"]["stop_loss_pct"])
        self.desk = Desk(root, self.desk_cfg, db_name=db_name)
        self.counterpart_result: dict | None = None
        self._counterpart_error: str | None = None
        self._ensure_schema()
        self._ensure_arm_column()

    # ---------- ledger ----------
    def _ensure_schema(self) -> None:
        self.desk.conn.executescript(PIPELINE_SCHEMA)
        self.desk.conn.executescript(KV_SCHEMA)
        self.desk.conn.commit()

    def _ensure_arm_column(self) -> None:
        """pipeline_runs gained `arm` after the first ledger existed; an old
        table keeps working because CREATE TABLE IF NOT EXISTS never upgrades
        one (the same trap the candidate_marks primary key fell into)."""
        columns = {row[1] for row in self.desk.conn.execute("PRAGMA table_info(pipeline_runs)")}
        if "arm" not in columns:
            self.desk.conn.execute("ALTER TABLE pipeline_runs ADD COLUMN arm TEXT")
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
            "INSERT INTO pipeline_runs (started_ms, version, ok, arm) VALUES (?,?,1,?)",
            (started, PIPELINE_VERSION, self.arm_name),
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
            if agent.name == "EXIT":
                # Evaluators are slow and their inputs move slowly; every 20
                # rounds is plenty. They only file proposals, never edit config.
                if self.cfg.get("evaluators", {}).get("enabled", True) and run_id % 20 == 0:
                    try:
                        from .evaluators import run_evaluators
                        filed = run_evaluators(self.desk.conn)
                        if filed:
                            summary["proposals"] = [f["title"] for f in filed]
                    except Exception as exc:  # noqa: BLE001 - evaluators never stop the desk
                        self.evaluator_error = f"{type(exc).__name__}: {exc}"
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
                # Money-flow shadows: what D's tracked wallets are buying right
                # now, recorded without trading it. Every 20th round only --
                # it copies D's ledger to read it, and this is evidence
                # gathering, not a signal path.
                if run_id % 20 == 0:
                    try:
                        from ..replay.moneyflow import collect_signals, mark_signals
                        self.moneyflow_result = {
                            "collected": collect_signals(self.root),
                            "marked": mark_signals(self.root),
                        }
                    except Exception as exc:  # noqa: BLE001 - shadow recording must never stop the desk
                        self.moneyflow_error = f"{type(exc).__name__}: {exc}"
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


def exit_round(pipe: "Pipeline") -> dict:
    """One mark-and-exit pass: the only work that has to run every minute.

    An entry needs a whole round -- scan, veto, judge, fill. A stop-loss needs
    none of that, only a price. Running marks inside the entry round meant a
    -12% stop was only checked every 4-5 minutes, so a token that gapped from
    -8% to -21% between two checks filled at -21%: seven of those fills cost
    the book about $100, three times what all 62 timeouts paid in fees.

    It goes through ExitAgent like every other round -- the orchestrator still
    has no money powers of its own, it just decides how often the agent runs.
    """
    from .agents import ExitAgent

    ctx = Context(root=pipe.root, cfg=pipe.cfg, started_ms=utc_now_ms(), desk=pipe.desk)
    result = ExitAgent().run(ctx)
    if not result.ok:
        return {"marked": 0, "closed": [], "stale": [], "error": result.detail.get("error")}
    return result.detail.get("result") or {}


def run_exit_loop(root: Path, interval: int = 60, config_path: Path | None = None,
                  db_name: str = "memehunter.sqlite") -> int:
    """Mark and exit on their own clock, independent of the entry pipeline."""
    import time

    from ..guards import guard_round

    log_path = root / "runtime" / "memehunter" / "loop.log"
    pipe = Pipeline(root, config_path, db_name=db_name)
    tag = f"[{pipe.arm_name}] " if pipe.arm_name else ""
    print(f"{tag}[exit-loop] every {interval}s · Ctrl-C or a STOP file to stop", flush=True)
    try:
        while True:
            may_run, note = guard_round(root, log_path)
            if not may_run:
                print(f"{tag}[exit-loop] halted: {note}", flush=True)
                return 0
            try:
                result = exit_round(pipe)
            except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
                print(f"{tag}[exit-loop] pass failed: {type(exc).__name__}: {exc}", flush=True)
                try:
                    pipe.desk.conn.rollback()
                except Exception:  # noqa: BLE001 - rollback is best effort
                    pass
                time.sleep(interval)
                continue
            for closed in result.get("closed") or []:
                print(
                    f"{tag}[exit] {closed['symbol']} {closed['reason']} {closed['pnl_usd']:+.2f}",
                    flush=True,
                )
            stale = result.get("stale") or []
            if stale:
                print(f"{tag}[exit] 无报价：{', '.join(stale[:3])}", flush=True)
            time.sleep(interval)
    finally:
        pipe.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Six-agent paper desk pipeline")
    parser.add_argument("command", choices=["once", "loop", "status", "proposals", "exit-loop"], nargs="?", default="once")
    parser.add_argument("--sleep", type=int, default=100, help="loop interval seconds")
    parser.add_argument("--interval", type=int, default=60, help="exit-loop seconds between mark passes")
    parser.add_argument("--config", default="", help="pipeline config file (default user_data/config.pipeline.json)")
    parser.add_argument("--db-name", default="memehunter.sqlite", help="ledger file under runtime/memehunter; the arm's identity")
    args = parser.parse_args(argv)

    root = _default_root()
    pipe = Pipeline(
        root,
        Path(args.config) if args.config else None,
        db_name=args.db_name,
    )
    try:
        if args.command == "exit-loop":
            return run_exit_loop(
                root,
                interval=args.interval,
                config_path=Path(args.config) if args.config else None,
                db_name=args.db_name,
            )
        if args.command == "loop":
            import time
            from ..guards import guard_round
            log_path = root / "runtime" / "memehunter" / "loop.log"
            tag = f"[{pipe.arm_name}] " if pipe.arm_name else ""
            print(f"{tag}[pipeline] loop every {args.sleep}s · Ctrl-C or a STOP file to stop", flush=True)
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
                    # A failed statement leaves the connection inside a
                    # transaction, and an open transaction holds the WAL write
                    # lock -- so one bad round would lock out the exit loop for
                    # as long as it took to notice. Roll back before retrying.
                    try:
                        pipe.desk.conn.rollback()
                    except Exception:  # noqa: BLE001 - rollback is best effort
                        pass
                time.sleep(args.sleep)
        if args.command == "proposals":
            from .evaluators import ensure_schema
            ensure_schema(pipe.desk.conn)
            rows = pipe.desk.conn.execute(
                "SELECT id, agent, kind, title, status, created_ms FROM pipeline_proposals"
                " WHERE status='pending' ORDER BY id DESC"
            ).fetchall()
            if not rows:
                print("没有待审提案")
            for row in rows:
                stamp = datetime.fromtimestamp(row[5] / 1000, timezone.utc).strftime("%m-%d %H:%M")
                print(f"#{row[0]} [{row[1]}] {row[3]}")
                print(f"     {stamp} · {row[2]}")
            return 0

        if args.command == "once":
            # A manual round must not pile onto a running one. The watchdog's
            # liveness probes have been seen running two at a time, and two
            # rounds on one ledger is how "database is locked" reaches JUDGE.
            ok, why = acquire_round(root)
            if not ok:
                print(f"[once] 跳过：{why}", flush=True)
                return 0
            try:
                summary = pipe.run_once()
                for step in summary["steps"]:
                    flag = "ok " if step["ok"] else "ERR"
                    print(f"[{flag}] {step['agent']:<9} {step['summary']}", flush=True)
                print(f"[run {summary['run_id']}] equity={summary['equity_usdt']}", flush=True)
            finally:
                release_round(root)
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
