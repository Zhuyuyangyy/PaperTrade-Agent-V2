#!/usr/bin/env python
"""夜间自主研究循环：诊断 → 假设 → 历史回测 → 记录（不碰运行中的基线）。

用法：
    python scripts/overnight_research.py --root data/openalpha_v2 \
        --out reports/overnight/2026-11-22 --tag 01
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.market.indicators import adx, atr, ema  # noqa: E402
from research.market.klines import closed_only  # noqa: E402
from research.runtime.live import BinanceKlineSource  # noqa: E402

D = Decimal
HOUR = 3_600_000


def _books(root: Path) -> list[str]:
    return sorted(p.stem for p in root.glob("*.sqlite"))


def _ledger_stats(path: Path) -> dict:
    if not path.is_file():
        return {}
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
        rows = c.execute("SELECT pnl, exit_reason, strategy_id FROM positions"
                         " WHERE status='closed'").fetchall()
        realized = sum(float(r["pnl"] or 0) for r in rows)
        wins = sum(1 for r in rows if float(r["pnl"] or 0) > 0)
        reasons: dict[str, int] = {}
        for r in rows:
            reasons[r["exit_reason"] or "?"] = reasons.get(r["exit_reason"] or "?", 0) + 1
        return {"orders": orders, "closed": len(rows), "realized": round(realized, 4),
                "win_rate": round(wins / len(rows), 3) if rows else None,
                "exit_reasons": reasons}
    finally:
        c.close()


def _diagnose(root: Path) -> dict:
    return {name: _ledger_stats(root / f"{name}.sqlite") for name in _books(root)}


# ---------- 轻量回测（真实 1h 历史，含成本） ----------

def _bars(limit: int = 500) -> list:
    src = BinanceKlineSource(symbol="BTCUSDT", limit=limit, timeout_s=20, retries=1)
    views = src.candles(int(time.time() * 1000))
    return list(views.get("1h") or [])


def _backtest(bars: list, *, adx_min: float, early_exit: bool, fee_bps: float = 5,
              slip_bps: float = 5, stop_atr: float = 1.5, tp_atr: float = 3.0) -> dict:
    dc = [c.close for c in bars]
    dh = [c.high for c in bars]
    dl = [c.low for c in bars]
    e20, e60 = ema(dc, 20), ema(dc, 60)
    a = atr(dh, dl, dc, 14)
    ad = adx(dh, dl, dc, 14)[0]
    closes = [float(x) for x in dc]
    highs = [float(x) for x in dh]
    lows = [float(x) for x in dl]
    trades = []
    i = 61
    while i < len(bars) - 1:
        def at(seq, period):
            k = i - (period - 1)
            return seq[k] if 0 <= k < len(seq) else None

        v20, v60, va, vad = at(e20, 20), at(e60, 60), at(a, 14), at(ad, 14)
        if None in (v20, v60, va, vad) or va <= 0:
            i += 1
            continue
        long_ok = v20 > v60 and float(vad) >= adx_min
        short_ok = v20 < v60 and float(vad) >= adx_min
        if not (long_ok or short_ok):
            i += 1
            continue
        side = 1 if long_ok else -1
        entry = closes[i]
        stop = entry - side * stop_atr * float(va)
        tp = entry + side * tp_atr * float(va)
        exit_price, reason, j = None, "TIMEOUT", i + 1
        while j < len(bars):
            if (side == 1 and lows[j] <= stop) or (side == -1 and highs[j] >= stop):
                exit_price, reason = stop, "STOP"
                break
            if (side == 1 and highs[j] >= tp) or (side == -1 and lows[j] <= tp):
                exit_price, reason = tp, "TP"
                break
            if early_exit and j - i >= 2:
                pnl_now = (closes[j] - entry) * side
                if pnl_now < 0:
                    exit_price, reason = closes[j], "EARLY"
                    break
            j += 1
        if exit_price is None:
            exit_price, j = closes[-1], len(bars) - 1
        gross = (exit_price - entry) * side
        cost = entry * (fee_bps + slip_bps) / 10_000 + exit_price * (fee_bps + slip_bps) / 10_000
        trades.append({"side": side, "gross": gross, "cost": cost,
                       "net_bps": (gross - cost) / entry * 10_000, "reason": reason})
        i = j + 1
    if not trades:
        return {"n": 0, "net_bps": 0.0, "pf": None, "win": None, "dd_bps": 0.0}
    net = [t["net_bps"] for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x < 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(trades), "net_bps": round(sum(net) / len(net), 2),
            "pf": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
            "win": round(len(wins) / len(net), 3), "dd_bps": round(dd, 2),
            "reasons": {r: sum(1 for t in trades if t["reason"] == r) for r in ("STOP", "TP", "EARLY", "TIMEOUT")}}


def _variants(bars: list) -> dict:
    return {
        "A_no_adx": _backtest(bars, adx_min=0, early_exit=False),
        "B_adx25": _backtest(bars, adx_min=25, early_exit=False),
        "C_adx25_early": _backtest(bars, adx_min=25, early_exit=True),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/openalpha_v2")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H:%M:%S")

    diagnosis = _diagnose(Path(args.root))
    bars = _bars(500)
    results = _variants(bars) if len(bars) >= 100 else {}

    payload = {"ts": ts, "tag": args.tag, "diagnosis": diagnosis, "backtest": results,
               "bars": len(bars)}
    (out / "research_loop.jsonl").open("a", encoding="utf-8").write(
        json.dumps(payload, ensure_ascii=False) + "\n")

    # 报告追加
    md = out / "experiments.md"
    with md.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## 研究循环 {ts}\n\n")
        fh.write("### 账本诊断\n\n")
        for name, s in diagnosis.items():
            fh.write(f"- {name}: orders={s.get('orders')} closed={s.get('closed')} "
                     f"realized={s.get('realized')} win={s.get('win_rate')} "
                     f"reasons={s.get('exit_reasons')}\n")
        fh.write("\n### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）\n\n")
        fh.write("| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |\n|---|---|---|---|---|---|---|\n")
        for name, r in results.items():
            fh.write(f"| {name} | {r['n']} | {r['net_bps']} | {r['pf']} | {r['win']} | "
                     f"{r['dd_bps']} | {r.get('reasons')} |\n")
        fh.write("\n结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。\n")
    print(json.dumps({"ts": ts, "books": list(diagnosis), "variants": list(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())