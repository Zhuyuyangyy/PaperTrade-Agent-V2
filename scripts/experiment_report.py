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
    """统一核算：账本里的每一笔已实现盈亏只算一次。

    口径：
      - 完整平仓：`positions.pnl`（已含该仓当时剩余的 entry_fee / exit_fee / funding）
      - 部分平仓：`position_fills.pnl`（按数量分摊 entry_fee + 本次 exit_fee）
      - 不再把 `positions.realized_net` 单独相加——它和 fills 是同一笔钱的两种记法，
        重复相加会把部分止盈收益算两遍。
      - 未实现：仅用最后一个权益快照估算，单独列示，不混进已实现。
    """
    p = root / f"{name}_decisions.jsonl"
    rows = []
    if p.exists():
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    entries, cooldown_blocks = 0, 0
    equity, data_stats, ai_stats = [], {}, {}
    ai_status: dict[str, int] = {}
    managed_actions: dict[str, int] = {}
    managed_closes = 0
    for r in rows:
        e = r.get("entry") or {}
        if e.get("fill"):
            entries += 1
        if e.get("blocked") == "COOLDOWN":
            cooldown_blocks += 1
        m = e.get("managed") or {}
        if m:
            a = str(m.get("action") or "?")
            managed_actions[a] = managed_actions.get(a, 0) + 1
            if a == "CLOSE_ALL" and m.get("executed"):
                managed_closes += 1
            st = str(m.get("ai_status") or "NOT_CALLED")
            ai_status[st] = ai_status.get(st, 0) + 1
        if r.get("equity") is not None:
            equity.append(_d(r["equity"]))
        if r.get("data_stats"):
            data_stats = r["data_stats"]
        if r.get("position_ai_stats"):
            ai_stats = r["position_ai_stats"]

    db = root / f"{name}.sqlite"
    closed, fills, opened, orders = [], [], [], []
    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            closed = [dict(r) for r in c.execute(
                "SELECT pnl, exit_reason, qty, entry_price FROM positions WHERE status='closed'")]
            fills = [dict(r) for r in c.execute("SELECT pnl, qty FROM position_fills")]
            opened = [dict(r) for r in c.execute(
                "SELECT qty, entry_price FROM positions WHERE status='open'")]
            orders = [dict(r) for r in c.execute(
                "SELECT notional FROM orders")] if True else []
        except sqlite3.Error:
            pass
        finally:
            c.close()

    realized_closed = sum((_d(r.get("pnl")) for r in closed), D("0"))
    realized_partial = sum((_d(r.get("pnl")) for r in fills), D("0"))
    realized = realized_closed + realized_partial
    notional = sum((_d(r.get("notional")) for r in orders), D("0"))
    last_equity = equity[-1] if equity else D("0")
    unrealized_est = last_equity - D("1000") - realized if equity else D("0")

    reasons: dict[str, int] = {}
    for r in closed:
        k = str(r.get("exit_reason") or "?")
        reasons[k] = reasons.get(k, 0) + 1
    reasons["MGR_PARTIAL"] = len(fills)

    peak, max_dd = None, D("0")
    for v in equity:
        peak = v if peak is None or v > peak else peak
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    avg_bps = (realized / notional * D("10000")) if notional > 0 else D("0")
    return {
        "book": name, "label": LABELS.get(name, name),
        "decisions": len(rows), "entries": entries,
        "closed_trades_db": len(closed), "partial_fills": len(fills),
        "managed_close_all": managed_closes, "cooldown_blocks": cooldown_blocks,
        "open_positions": len(opened),
        "realized_closed": str(realized_closed.quantize(D("0.00000001"))),
        "realized_partial": str(realized_partial.quantize(D("0.00000001"))),
        "realized_net": str(realized.quantize(D("0.00000001"))),
        "unrealized_est": str(unrealized_est.quantize(D("0.00000001"))),
        "notional_traded": str(notional.quantize(D("0.0001"))),
        "avg_bps_per_notional": str(avg_bps.quantize(D("0.0001"))),
        "max_drawdown_pct": str((max_dd * D("100")).quantize(D("0.0001"))),
        "managed_actions": managed_actions, "ai_status": ai_status,
        "exit_reasons": reasons, "data_stats": data_stats,
        "position_ai_stats": ai_stats,
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
