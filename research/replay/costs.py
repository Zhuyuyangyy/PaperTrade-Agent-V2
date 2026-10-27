"""What does a round trip actually cost? The desk answers "120 bps" from
config, and every conclusion downstream inherits that number.

This models the toll from the two things that decide it:

  * pool depth  -- a constant-product pool moves price by notional / (depth/2)
                   per side, so a $100 order into a $50k pool pays 40 bps and
                   the same order into a $2M pool pays 1 bp;
  * venue fee   -- the LP fee, which is a constant per venue, not per trade.

Everything else (MEV, token taxes, routing splits, priority fees above the
fixed estimate) is NOT modelled, and the report says so. The point is not to
prove the desk charges too much -- it is to find out whether 120 bps is the
right order of magnitude, and what the book looks like at the other end.
"""
from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path

from .selectivity import _config_cost, _load

LP_FEE_ONE_WAY = 0.0025     # Raydium CPMM / most Solana AMMs, 25 bps per side
TX_FIXED_USD = 0.02         # signature + priority, per transaction, both sides
SCALE_NOTIONALS = (100.0, 500.0, 1000.0, 5000.0)
LIQUIDITY_WINDOW_MS = 30 * 60_000


def _liquidity_at(conn: sqlite3.Connection, mint: str, opened_ms: int) -> float | None:
    """Pool depth from the snapshot closest before the fill.

    The candidate table is the only place the desk records depth, and it is
    written at scan time -- so this is the depth the desk *believed* when it
    decided, not the depth at the moment of the swap.
    """
    row = conn.execute(
        "SELECT liquidity_usd FROM candidates WHERE mint = ?"
        " AND scanned_at_ms BETWEEN ? AND ? ORDER BY abs(scanned_at_ms - ?) LIMIT 1",
        (mint, opened_ms - LIQUIDITY_WINDOW_MS, opened_ms + LIQUIDITY_WINDOW_MS, opened_ms),
    ).fetchone()
    if not row or not row[0]:
        return None
    depth = float(row[0])
    return depth if depth > 0 else None


def impact_one_way(notional: float, liquidity_usd: float) -> float:
    """Fraction of price paid to the curve, one side.

    A CPMM holds both sides, so only half of `liquidity_usd` is the quote
    reserve that absorbs a buy: impact ~= notional / (depth / 2).
    """
    if liquidity_usd <= 0 or notional <= 0:
        return 0.0
    return notional / (liquidity_usd / 2.0)


def modelled_round_trip(notional: float, liquidity_usd: float,
                        lp_fee: float = LP_FEE_ONE_WAY,
                        tx_usd: float = TX_FIXED_USD) -> float:
    """Total round-trip cost as a fraction of notional."""
    if notional <= 0:
        return 0.0
    per_side = lp_fee + impact_one_way(notional, liquidity_usd) + tx_usd / notional
    return per_side * 2.0


def calibrate(root: Path, db_name: str = "memehunter.sqlite") -> dict:
    conn, tmp_dir = _load(root, db_name)
    try:
        assumed = _config_cost(root)  # fraction, e.g. 0.012
        rows = []
        for row in conn.execute(
            "SELECT id, mint, symbol, opened_at_ms, notional_usd, qty,"
            " entry_fill_price, exit_fill_price, pnl_usd, exit_reason"
            " FROM positions WHERE status='closed' ORDER BY opened_at_ms"
        ):
            d = dict(row)
            depth = _liquidity_at(conn, str(d["mint"]), int(d["opened_at_ms"]))
            d["liquidity_usd"] = depth
            d["gross"] = float(d["qty"] or 0.0) * (
                float(d["exit_fill_price"] or 0.0) - float(d["entry_fill_price"] or 0.0)
            )
            rows.append(d)
        depths = [r["liquidity_usd"] for r in rows if r["liquidity_usd"]]
    finally:
        conn.close()
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)

    priced = []
    missing = 0
    for d in rows:
        depth = d["liquidity_usd"]
        notional = float(d["notional_usd"] or 0.0)
        if not depth or notional <= 0:
            missing += 1
            continue
        rt = modelled_round_trip(notional, depth)
        d["model_rt"] = rt
        d["model_toll"] = rt * notional
        d["assumed_toll"] = assumed * notional
        d["net_model"] = d["gross"] - d["model_toll"]
        d["net_assumed"] = float(d["pnl_usd"] or 0.0)
        priced.append(d)

    def _pct(vals: list[float], q: float) -> float:
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * q))]

    out = {
        "assumed_bps": assumed * 10_000,
        "n_trades": len(rows),
        "n_priced": len(priced),
        "n_missing_depth": missing,
        "liquidity": {
            "p10": _pct(depths, 0.10) if depths else None,
            "median": statistics.median(depths) if depths else None,
            "p90": _pct(depths, 0.90) if depths else None,
        },
        "model_bps": (
            {
                "p10": _pct([d["model_rt"] for d in priced], 0.10) * 10_000,
                "median": statistics.median([d["model_rt"] for d in priced]) * 10_000,
                "p90": _pct([d["model_rt"] for d in priced], 0.90) * 10_000,
                "max": max(d["model_rt"] for d in priced) * 10_000,
            }
            if priced
            else None
        ),
        "book": {
            "gross": sum(d["gross"] for d in priced),
            "net_assumed": sum(d["net_assumed"] for d in priced),
            "net_model": sum(d["net_model"] for d in priced),
            "toll_assumed": sum(d["assumed_toll"] for d in priced),
            "toll_model": sum(d["model_toll"] for d in priced),
        },
        "flips_to_profit": sum(1 for d in priced if d["net_assumed"] <= 0 < d["net_model"]),
        "gross_bps": (
            sum(d["gross"] for d in priced) / sum(float(d["notional_usd"]) for d in priced) * 10_000
            if priced
            else None
        ),
        # The part of the toll no amount of depth can remove: the LP takes its
        # cut on both sides no matter how small the order is.
        "floor_bps": LP_FEE_ONE_WAY * 2 * 10_000 if priced else None,
        "worst": sorted(priced, key=lambda d: -d["model_rt"])[:5],
        "scale": [],
        "note": (
            "模型只含 AMM 曲线冲击 + LP 费率 + 固定链上费；不含 MEV、代币税、路由拆分、"
            "Jupiter 平台费，也未区分场所（默认 25bps）。池深度取成交前最近的扫描快照，"
            "不是成交瞬间的真实深度。结论只用于判断量级。"
        ),
    }
    if depths:
        median_depth = statistics.median(depths)
        for notional in SCALE_NOTIONALS:
            out["scale"].append(
                {
                    "notional": notional,
                    "bps": modelled_round_trip(notional, median_depth) * 10_000,
                    "usd": modelled_round_trip(notional, median_depth) * notional,
                }
            )
    return out


def format_calibration(rep: dict) -> str:
    lines = []
    if not rep["n_priced"]:
        return "成本校准：没有可定价的成交（缺池深度）"
    mb = rep["model_bps"]
    liq = rep["liquidity"]
    book = rep["book"]
    lines.append(
        f"成本校准 · {rep['n_priced']}/{rep['n_trades']} 笔可定价"
        + (f"（{rep['n_missing_depth']} 笔缺池深度）" if rep["n_missing_depth"] else "")
    )
    lines.append(
        f"  池深度 p10 ${liq['p10']:,.0f} · 中位 ${liq['median']:,.0f} · p90 ${liq['p90']:,.0f}"
    )
    lines.append(
        f"  往返成本（模型）p10 {mb['p10']:.0f}bps · 中位 {mb['median']:.0f}bps · "
        f"p90 {mb['p90']:.0f}bps · 最大 {mb['max']:.0f}bps   —— 账面假设 {rep['assumed_bps']:.0f}bps"
    )
    lines.append(
        f"  全账重算：毛利 {book['gross']:+.2f} · 假设成本净 {book['net_assumed']:+.2f} · "
        f"模型成本净 {book['net_model']:+.2f}"
    )
    lines.append(
        f"  成本总额：假设 -{book['toll_assumed']:.2f} → 模型 -{book['toll_model']:.2f}"
        f"（差 {book['toll_assumed'] - book['toll_model']:+.2f}）· "
        f"由亏转盈 {rep['flips_to_profit']} 笔"
    )
    if rep.get("gross_bps") is not None:
        verdict = "毛利够付" if rep["gross_bps"] >= mb["median"] else "毛利不够付"
        lines.append(
            f"  每笔毛利 {rep['gross_bps']:+.0f}bps vs 模型成本 {mb['median']:.0f}bps"
            f"（结构性下限 {rep['floor_bps']:.0f}bps = 双边 LP 费）→ {verdict}"
        )
    if rep["worst"]:
        lines.append("  模型成本最高的几笔：")
        for d in rep["worst"]:
            lines.append(
                f"    {d['symbol'][:10]:<11} ${d['notional_usd']:>7.0f} 进 ${d['liquidity_usd']:>10,.0f} 池"
                f"  {d['model_rt']*10_000:>5.0f}bps  毛 {d['gross']:>+7.2f}  净 {d['net_model']:>+7.2f}"
            )
    if rep["scale"]:
        lines.append("  按中位池深度放大单笔：")
        for s in rep["scale"]:
            lines.append(f"    ${s['notional']:>7,.0f} → {s['bps']:>5.0f}bps（${s['usd']:.2f}）")
    lines.append("  " + rep["note"])
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    print(format_calibration(calibrate(root)))
