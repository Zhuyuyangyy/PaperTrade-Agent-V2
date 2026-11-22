"""把单进程 A/B/C/D 实验的账本汇总成可比指标。

用法：
    python -m scripts.experiment_report --root data/v3_experiment

只读 SQLite 与 decisions jsonl，不发任何网络请求。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

BOOKS = ("rules_5m", "deepseek_5m", "dual_5m", "deepseek_5m_manager")
LABELS = {
    "rules_5m": "A 固定ATR+旧认错",
    "deepseek_5m": "B 成本感知+冷却",
    "dual_5m": "D AI 持仓管理",
    "deepseek_5m_manager": "C 确定性移动止损/分批",
}


def _d(v) -> D:
    try:
        return D(str(v or 0))
    except Exception:  # noqa: BLE001
        return D("0")


def book_stats(root: Path, name: str) -> dict:
    p = root / f"{name}_decisions.jsonl"
    rows = []
    if p.exists():
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    entries, closes, managed, blocked = 0, 0, {}, 0
    equity, notional_total, pnl_gross = [], D("0"), D("0")
    for r in rows:
        e = r.get("entry") or {}
        if e.get("fill"):
            entries += 1
            f = e["fill"]
            notional_total += _d(f.get("notional")) or _d(f.get("qty")) * _d(f.get("price"))
        if e.get("blocked") == "COOLDOWN":
            blocked += 1
        for c in e.get("closed") or []:
            closes += 1
            pnl_gross += _d(c.get("pnl"))
        m = e.get("managed") or {}
        if m.get("action"):
            managed[m["action"]] = managed.get(m["action"], 0) + 1
        if r.get("equity") is not None:
            equity.append(_d(r["equity"]))

    db = root / f"{name}.sqlite"
    db_rows = []
    if db.exists():
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        try:
            db_rows = [dict(r) for r in c.execute(
                "SELECT pnl, exit_reason, qty, entry_price FROM positions WHERE status='closed'")]
        except sqlite3.Error:
            db_rows = []
        finally:
            c.close()

    realized = sum((_d(r.get("pnl")) for r in db_rows), D("0"))
    reasons: dict[str, int] = {}
    for r in db_rows:
        k = str(r.get("exit_reason") or "?")
        reasons[k] = reasons.get(k, 0) + 1

    # 最大回撤（按决策日志里的 equity 序列）
    peak, max_dd = None, D("0")
    for v in equity:
        peak = v if peak is None or v > peak else peak
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    avg_bps = (realized / notional_total * D("10000")) if notional_total > 0 else D("0")
    return {
        "book": name, "label": LABELS.get(name, name),
        "decisions": len(rows), "entries": entries, "closes": closes,
        "cooldown_blocks": blocked,
        "realized_pnl": str(realized.quantize(D("0.00000001"))),
        "notional_traded": str(notional_total.quantize(D("0.0001"))),
        "avg_bps_per_notional": str(avg_bps.quantize(D("0.0001"))),
        "max_drawdown_pct": str((max_dd * D("100")).quantize(D("0.0001"))),
        "managed_actions": managed, "exit_reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    root = Path(a.root)
    out = {"root": str(root), "books": [book_stats(root, n) for n in BOOKS]}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"written -> {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
