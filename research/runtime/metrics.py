"""账本统计（Sprint 4.2）：不只看现金，还要看回撤、胜率、策略分布。

所有指标都只从 sqlite 账本推导；没有数据就返回 0/None，不猜。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.paper.broker import PaperBroker

D = Decimal


def max_drawdown(equities: list[Decimal]) -> Decimal:
    """最大回撤（比例 0..1）：峰到谷的最大跌幅 / 峰值。"""
    if not equities:
        return D("0")
    peak = equities[0]
    worst = D("0")
    for e in equities:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak
            if dd > worst:
                worst = dd
    return worst


def _equity_series(broker: PaperBroker) -> list[Decimal]:
    rows = broker._db.execute("SELECT equity FROM equity ORDER BY ts_ms").fetchall()
    return [Decimal(r["equity"]) for r in rows]


def summarize(broker: PaperBroker, label: str = "book") -> dict:
    orders = int(broker._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
    closed_rows = broker._db.execute(
        "SELECT pnl, strategy_id, funding_paid FROM positions WHERE status='closed'"
    ).fetchall()
    closed = len(closed_rows)
    realized = sum((Decimal(r["pnl"] or "0") for r in closed_rows), D("0"))
    funding = sum((Decimal(r["funding_paid"] or "0") for r in closed_rows), D("0"))
    wins = sum(1 for r in closed_rows if Decimal(r["pnl"] or "0") > 0)
    by_strategy: dict[str, dict] = {}
    for r in closed_rows:
        key = r["strategy_id"] or "unknown"
        s = by_strategy.setdefault(key, {"closed": 0, "realized": D("0"), "wins": 0})
        s["closed"] += 1
        s["realized"] += Decimal(r["pnl"] or "0")
        if Decimal(r["pnl"] or "0") > 0:
            s["wins"] += 1
    for s in by_strategy.values():
        s["realized"] = str(s["realized"])
        s["win_rate"] = f"{s['wins'] / s['closed']:.3f}" if s["closed"] else None
    equity = _equity_series(broker)
    open_positions = broker.open_positions()
    unrealized = D("0")
    for p in open_positions:
        qty, entry = Decimal(p["qty"]), Decimal(p["entry_price"])
        mark = Decimal(p["mark_price"]) if p.get("mark_price") else entry
        direction = D("1") if p["side"] == "LONG" else D("-1")
        unrealized += (mark - entry) * qty * direction
    return {
        "label": label,
        "cash": str(broker.cash()),
        "orders": orders,
        "closed": closed,
        "open_positions": len(open_positions),
        "realized_pnl": str(realized),
        "unrealized_pnl": str(unrealized),
        "funding_paid": str(funding),
        "win_rate": f"{wins / closed:.3f}" if closed else None,
        "max_drawdown": str(max_drawdown(equity).quantize(D("0.0001"))),
        "equity_points": len(equity),
        "by_strategy": by_strategy,
    }


def summarize_books(root: Path | str, *, shadow_db: str = "shadow.sqlite",
                    verified_db: str = "verified.sqlite") -> dict:
    root = Path(root)
    out: dict = {"root": str(root)}
    for label, name in (("shadow", shadow_db), ("verified", verified_db)):
        path = root / name
        if path.is_file():
            b = PaperBroker(path, starting_cash=D("1000"))
            out[label] = summarize(b, label)
            b.close()
        else:
            out[label] = None
    return out