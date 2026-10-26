"""Evaluator agents: they propose, they never apply.

Three read-only agents watch the desk's own evidence and file proposals. The
distinction matters more than the feature: an auto-tuner would test two
thousand parameter sets overnight and keep the best-looking one, which is how
a system memorises noise and calls it an edge. A proposer can only say "here
is what the numbers show, here is what I would change, here is my evidence" --
a human applies it.

Proposals live in the same ledger as everything else, carry their evidence,
and are cheap to reject. Rejecting is the expected outcome most of the time;
that is the point.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

PROPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence TEXT NOT NULL,
    target TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_ms INTEGER NOT NULL,
    decided_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON pipeline_proposals(status);
"""


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PROPOSAL_SCHEMA)
    conn.commit()


def file_proposal(conn: sqlite3.Connection, *, agent: str, kind: str, title: str,
                  detail: str, evidence: str, target: str = "") -> int | None:
    """File one proposal. Returns None when an identical pending one exists."""
    dup = conn.execute(
        "SELECT id FROM pipeline_proposals WHERE agent=? AND kind=? AND title=?"
        " AND status='pending'",
        (agent, kind, title),
    ).fetchone()
    if dup:
        return None
    cur = conn.execute(
        "INSERT INTO pipeline_proposals (agent, kind, title, detail, evidence, target,"
        " status, created_ms) VALUES (?,?,?,?,?,?,'pending',?)",
        (agent, kind, title, detail, evidence, target, _now_ms()),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------- evaluators


def evaluate_calibration(conn: sqlite3.Connection) -> list[dict]:
    """CALIBRATOR: does each JUDGE score band still earn its place?

    A band whose hit rate is not better than the band below it is decoration;
    a band that is consistently worse is a liability. Both are proposals, not
    edits -- the numbers argue, a human decides.
    """
    from ..replay.calibration import judge_calibration

    try:
        report = judge_calibration(conn)
    except Exception:  # noqa: BLE001 - an evaluator must never break the desk
        return []
    bands = [b for b in report.get("bands", []) if b.get("enough")]
    if len(bands) < 2:
        return []
    out = []
    best = max(bands, key=lambda b: b["hit_rate_pct"])
    worst = min(bands, key=lambda b: b["hit_rate_pct"])
    if best["band"] == worst["band"]:
        return []
    spread = best["hit_rate_pct"] - worst["hit_rate_pct"]
    if spread < 15:
        out.append({
            "agent": "CALIBRATOR", "kind": "calibration_flat",
            "title": f"JUDGE 分档区分度不足（最好/最差命中率差 {spread:.0f}pp）",
            "detail": f"最高命中率来自 {best['band']} 段（{best['hit_rate_pct']:.0f}%），"
                      f"最低来自 {worst['band']} 段（{worst['hit_rate_pct']:.0f}%）。"
                      "差距在噪声范围内，建议暂停按分数分级加仓，回归统一仓位。",
            "evidence": json_dumps(report, 1500),
            "target": "sizing.tiers",
        })
    if worst["hit_rate_pct"] < 35 and worst["n"] >= 5:
        out.append({
            "agent": "CALIBRATOR", "kind": "calibration_band_bad",
            "title": f"JUDGE {worst['band']} 段命中率仅 {worst['hit_rate_pct']:.0f}%",
            "detail": f"该分数段 {worst['n']} 个样本的后续收益胜率明显低于抛硬币，"
                      "建议把该段从 sizing 分级中移除或降到最低档。",
            "evidence": json_dumps(report, 1500),
            "target": "sizing.tiers",
        })
    return out


def evaluate_drift(conn: sqlite3.Connection) -> list[dict]:
    """DRIFT: did the upstream data change shape?

    A jump in one skip reason, or a collapse in fill rate, usually means an
    API changed -- not that the market changed. Saying so early saves a week
    of debugging the wrong thing.
    """
    import shutil
    import tempfile
    from pathlib import Path

    # decisions lives in the CopyWatch ledger, not this one.
    d_db = Path(__file__).resolve().parents[2] / "runtime" / "copywatch" / "copywatch.sqlite"
    if not d_db.is_file():
        return []
    tmp_dir = tempfile.mkdtemp()
    try:
        copy = Path(tmp_dir) / d_db.name
        shutil.copy(d_db, copy)
        ro = sqlite3.connect(str(copy))
        try:
            rows = ro.execute(
                "SELECT primary_code, COUNT(*) n FROM decisions WHERE decided_at_ms > ?"
                " GROUP BY primary_code ORDER BY n DESC",
                (int(datetime.now(timezone.utc).timestamp() * 1000) - 6 * 3_600_000,),
            ).fetchall()
        finally:
            ro.close()
    except (OSError, sqlite3.Error):
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if not rows:
        return []
    total = sum(r[1] for r in rows)
    top_code, top_n = rows[0][0], rows[0][1]
    share = top_n / total * 100.0
    if share > 60 and total >= 30:
        return [{
            "agent": "DRIFT", "kind": "skip_concentration",
            "title": f"跳过原因高度集中：{top_code} 占 {share:.0f}%",
            "detail": f"近 6 小时 {total} 次研判中 {top_n} 次因 {top_code} 被跳过。"
                      "若这是新出现的集中点，优先怀疑上游字段或费率变化，而不是策略失效。",
            "evidence": json_dumps({"total": total, "top": [list(r) for r in rows[:5]]}, 800),
            "target": "copywatch.risk",
        }]
    return []


def evaluate_arbiter(conn: sqlite3.Connection) -> list[dict]:
    """ARBITER: is one signal source beating the other, and by enough?"""
    from pathlib import Path

    from ..replay.signals import compare

    try:
        report = compare(Path(__file__).resolve().parents[2])
    except Exception:  # noqa: BLE001
        return []
    scored = report.get("scored") or []
    if len(scored) < 40:
        return []
    import statistics as stats

    d_vals = [x["ret"] for x in scored if x["side"] == "D"]
    e_vals = [x["ret"] for x in scored if x["side"] == "E"]
    if len(d_vals) < 15 or len(e_vals) < 15:
        return []
    d_mean = stats.mean(d_vals)
    e_mean = stats.mean(e_vals)
    gap = d_mean - e_mean
    if abs(gap) < 1.0:
        return [{
            "agent": "ARBITER", "kind": "signals_flat",
            "title": f"两个信号源前向收益接近（D {d_mean:+.2f}% vs E {e_mean:+.2f}%）",
            "detail": "差距在 1 个百分点内，样本内无法区分优劣。保持现有规则，"
                      "不要因为短期偏好调整选币逻辑。",
            "evidence": json_dumps({"d_n": len(d_vals), "d_mean": d_mean,
                                    "e_n": len(e_vals), "e_mean": e_mean}, 600),
            "target": "pipeline.risk",
        }]
    winner = "D（链上资金流）" if gap > 0 else "E（organic 分）"
    return [{
        "agent": "ARBITER", "kind": "signals_gap",
        "title": f"{winner} 前向收益领先 {abs(gap):.2f}pp",
        "detail": f"D 均值 {d_mean:+.2f}%（n={len(d_vals)}），E 均值 {e_mean:+.2f}%（n={len(e_vals)}）。"
                  "若差距持续扩大，可考虑把领先信号源的特征权重上调——先小步，再观察。",
        "evidence": json_dumps({"d_n": len(d_vals), "d_mean": d_mean,
                                "e_n": len(e_vals), "e_mean": e_mean}, 600),
        "target": "pipeline.smart_money",
    }]


EVALUATORS = [evaluate_calibration, evaluate_drift, evaluate_arbiter]


def run_evaluators(conn: sqlite3.Connection) -> list[dict]:
    """Run every evaluator, file what they propose, return the filed proposals."""
    ensure_schema(conn)
    filed = []
    for fn in EVALUATORS:
        try:
            proposals = fn(conn) or []
        except Exception:  # noqa: BLE001 - one broken evaluator never stops the rest
            continue
        for p in proposals:
            pid = file_proposal(conn, **p)
            if pid:
                filed.append({"id": pid, **p})
    return filed


def json_dumps(obj, limit: int = 1200) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return text[:limit]
