#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1：现货-永续 Funding Carry 基线（事前锁规则，不调参）。

规则在写任何回测代码之前就定死（2026-11-25），依据是用户给的 Phase 1 草案：

  开仓：滚动 9 期（3 天）平均年化 funding > 15%
        且 永续相对现货基差 >= -5bps
  平仓：最近连续 3 期 funding <= 0
        或 滚动 9 期年化 < 3%
  成交：信号在 bar[i] 收盘产生，bar[i+1] 的 open 成交（现货腿与永续腿同一时刻）
  成本：4 条腿，基准 32bps 往返（明细见 COST_LEGS），另有 24bps 乐观 / 64bps 2x

防前视（沿用 alpha_lite 的口径）：
  1. 只用 bar[i] 收盘价算基差，成交在 bar[i+1] open
  2. funding 只用「结算时刻 <= bar[i] 收盘时刻」的，未结算的不进决策
  3. 持仓期内的 funding 收入只取 t_entry < f_t < t_exit，两端都不算（不虚增）
  4. 数据窗口取永续与现货的交集，交集内必须零缺口，否则拒跑

证伪门槛（事前写死，不事后放宽）：
  G1 全样本年化 > 5%
  G2 样本外（最后 1 年）年化 > 5%
  G3 2x 成本（64bps）下全样本总收益 >= 0
  G4 组合最大回撤 <= 15%
  G5 完整持仓 >= 10 次（否则样本不足 -> INCONCLUSIVE）
  全部满足才 NOT_FALSIFIED。

用法：
    python research/lite/carry.py
    python research/lite/carry.py --cost-bps 24 --save-trades
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alpha_lite as al  # noqa: E402

REPO = HERE.parent.parent
MARKET = REPO / "data" / "v3_market"
REPORT_DIR = HERE / "reports"

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOUR_MS = 3_600_000

# 4 条腿的成本明细（单边 bps，含滑点）
COST_LEGS = {
    "open_spot_buy": 10.0,   # taker 9 + slip 1
    "open_perp_sell": 6.0,   # taker 5 + slip 1
    "close_spot_sell": 10.0,
    "close_perp_buy": 6.0,
}
COST_BPS_BASE = sum(COST_LEGS.values())  # 32.0

# 事前锁死的阈值
ANN_OPEN = 15.0      # 滚动 9 期年化 funding 开仓门槛（%）
ANN_CLOSE = 3.0      # 滚动年化平仓门槛（%）
ROLL_N = 9           # 9 期 × 8h = 3 天
NEG_N = 3            # 连续 3 期 <= 0 平仓
BASIS_MIN = -5.0     # 基差下限 bps
FUNDING_PER_YEAR = 3 * 365  # 每 8h 一期

GATES = {
    "G1_full_ann_gt_5": "全样本年化 > 5%",
    "G2_oos_ann_gt_5": "样本外（最后 1 年）年化 > 5%",
    "G3_2x_not_negative": "2x 成本下全样本总收益 >= 0",
    "G4_mdd_le_15": "组合最大回撤 <= 15%",
    "G5_trades_ge_10": "完整持仓 >= 10 次",
}


# ---------------------------------------------------------------- 载入

def load_spot(sym: str) -> dict:
    store = al.load_store(al.store_path(sym, "1h", "spot"))
    return {t: (float(r["r"][1]), float(r["r"][2]), float(r["r"][3]), float(r["r"][4]))
            for t, r in store.items()}


def load_perp(sym: str):
    d = json.loads((MARKET / sym / "bars_1h.json").read_text(encoding="utf-8"))
    bars = {r["t"]: (float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]))
            for r in d["rows"]}
    return bars, d["meta"]


def load_funding(sym: str):
    d = json.loads((MARKET / sym / "funding.json").read_text(encoding="utf-8"))
    rows = sorted((r["t"], float(r["rate"]), float(r["mark_price"])) for r in d["rows"])
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], d["meta"]


def align(sym: str):
    spot = load_spot(sym)
    perp, pmeta = load_perp(sym)
    f_ts, f_rate, f_mark, fmeta = load_funding(sym)
    ts = sorted(set(perp) & set(spot))
    if not ts:
        raise SystemExit(f"{sym}: 现货与永续没有交集")
    gaps = al.find_gaps(ts, HOUR_MS)
    if gaps:
        raise SystemExit(f"{sym}: 交集内有 {len(gaps)} 处缺口，拒绝运行：{gaps[:3]}")
    return {
        "sym": sym, "ts": ts, "spot": spot, "perp": perp,
        "f_ts": f_ts, "f_rate": f_rate, "f_mark": f_mark,
        "pmeta": pmeta, "fmeta": fmeta,
    }


# ---------------------------------------------------------------- 模拟

def simulate(series, cost_bps: float):
    """返回交易列表。每笔的 ret 都是「对现货名义」的收益率。"""
    ts, spot, perp = series["ts"], series["spot"], series["perp"]
    f_ts, f_rate = series["f_ts"], series["f_rate"]
    sym = series["sym"]
    n = len(ts)
    trades = []
    warmup = ROLL_N * 8 + 1  # 至少 72 根才有 9 期 funding
    pos = None
    i = warmup

    while i < n - 1:
        t = ts[i]
        close_t = t + HOUR_MS - 1
        j = bisect.bisect_right(f_ts, close_t)  # 已结算的期数

        if pos is None:
            if j >= ROLL_N:
                ann = statistics.mean(f_rate[j - ROLL_N:j]) * FUNDING_PER_YEAR * 100
                basis = (perp[t][3] / spot[t][3] - 1.0) * 10_000
                if ann > ANN_OPEN and basis >= BASIS_MIN:
                    pos = {"sig": i, "e": i + 1, "ann_in": ann, "basis_in": basis,
                           "spot_e": spot[ts[i + 1]][0], "perp_e": perp[ts[i + 1]][0]}
                    i += 1
                    continue
        else:
            neg3 = j >= NEG_N and all(r <= 0 for r in f_rate[j - NEG_N:j])
            ann_now = (statistics.mean(f_rate[j - ROLL_N:j]) * FUNDING_PER_YEAR * 100
                       if j >= ROLL_N else None)
            must_close = neg3 or (ann_now is not None and ann_now < ANN_CLOSE)
            if must_close:
                e, x = pos["e"], i + 1
                te, tx = ts[e], ts[x]
                spot_e, spot_x = spot[te][0], spot[tx][0]
                perp_e, perp_x = perp[te][0], perp[tx][0]

                spot_ret = spot_x / spot_e - 1.0
                perp_ret = -(perp_x / perp_e - 1.0)
                lo = bisect.bisect_right(f_ts, te)   # 严格大于入场时刻
                hi = bisect.bisect_left(f_ts, tx)    # 严格小于出场时刻
                funding_ret = sum(f_rate[k] for k in range(lo, hi)) * (perp_e / spot_e)
                gross = spot_ret + perp_ret + funding_ret
                net = gross - cost_bps / 10_000.0
                trades.append({
                    "symbol": sym,
                    "signal_time": ts[pos["sig"]],
                    "entry_time": te,
                    "exit_time": tx,
                    "hold_hours": (tx - te) / HOUR_MS,
                    "ann_in": round(pos["ann_in"], 2),
                    "basis_in_bps": round(pos["basis_in"], 2),
                    "basis_out_bps": round((perp[tx][3] / spot[tx][3] - 1.0) * 10_000, 2),
                    "spot_ret_bps": round(spot_ret * 10_000, 2),
                    "perp_ret_bps": round(perp_ret * 10_000, 2),
                    "funding_ret_bps": round(funding_ret * 10_000, 2),
                    "gross_bps": round(gross * 10_000, 2),
                    "net_bps": round(net * 10_000, 2),
                    "n_funding": hi - lo,
                })
                pos = None
                i = x
                continue
        i += 1

    return trades


# ---------------------------------------------------------------- 统计

def summarize(trades, years: float, label: str):
    if not trades:
        return {"label": label, "trades": 0, "total_pct": 0.0, "ann_pct": 0.0,
                "win_rate": None, "avg_hold_h": None, "mdd_pct": 0.0,
                "funding_bps": 0.0, "spread_bps": 0.0, "gross_bps": 0.0, "cost_bps": 0.0}
    rets = [t["net_bps"] / 10_000 for t in trades]
    total = sum(rets)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq += r          # 固定名义累加，不复利（carry 笔数少，复利会放大）
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    ann = (eq ** (1 / years) - 1) * 100 if years > 0 and eq > 0 else total / years * 100
    return {
        "label": label,
        "trades": len(trades),
        "total_pct": round(total * 100, 4),
        "ann_pct": round(ann, 4),
        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 2),
        "avg_hold_h": round(statistics.mean(t["hold_hours"] for t in trades), 1),
        "mdd_pct": round(mdd * 100, 4),
        "funding_bps": round(statistics.mean(t["funding_ret_bps"] for t in trades), 2),
        "spread_bps": round(statistics.mean(t["spot_ret_bps"] + t["perp_ret_bps"]
                                           for t in trades), 2),
        "gross_bps": round(statistics.mean(t["gross_bps"] for t in trades), 2),
        "cost_bps": round(statistics.mean(t["net_bps"] - t["gross_bps"] for t in trades) * -1, 2),
    }


def portfolio(trades, n_symbols: int):
    """等权：每笔只占 1/n 资金，按时间顺序累加。"""
    if not trades:
        return []
    w = 1.0 / n_symbols
    return [{"net_bps": t["net_bps"] * w, "hold_hours": t["hold_hours"],
             "funding_ret_bps": t["funding_ret_bps"] * w,
             "spot_ret_bps": t["spot_ret_bps"] * w,
             "perp_ret_bps": t["perp_ret_bps"] * w,
             "gross_bps": t["gross_bps"] * w,
             "entry_time": t["entry_time"], "exit_time": t["exit_time"]}
            for t in sorted(trades, key=lambda x: x["entry_time"])]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="Phase 1: 现货-永续 Funding Carry 基线")
    p.add_argument("--cost-bps", type=float, default=COST_BPS_BASE)
    p.add_argument("--save-trades", action="store_true")
    args = p.parse_args(argv)

    print("Phase 1 · 现货-永续 Funding Carry（Delta 中性）")
    print(f"规则：开仓 滚动{ROLL_N}期年化>{ANN_OPEN}% 且 基差>={BASIS_MIN}bps；"
          f"平仓 连续{NEG_N}期<=0 或 年化<{ANN_CLOSE}%")
    print(f"成本：{args.cost_bps:g}bps 往返（4 腿："
          + " + ".join(f"{k}={v:g}" for k, v in COST_LEGS.items()) + "）\n")

    all_series = []
    for sym in SYMBOLS:
        s = align(sym)
        all_series.append(s)
        print(f"{sym}: 交集 {len(s['ts'])} 根  "
              f"{al.utc(s['ts'][0])} → {al.utc(s['ts'][-1])}  零缺口  "
              f"funding {len(s['f_ts'])} 条")

    ys = []
    for s in all_series:
        span = s["ts"][-1] - s["ts"][0]
        ys.append(span / (365.25 * 86_400_000))
    years = min(ys)
    print(f"\n公共窗口 {years:.3f} 年\n")

    all_trades = []
    per_sym = {}
    for s in all_series:
        tr = simulate(s, args.cost_bps)
        per_sym[s["sym"]] = tr
        all_trades.extend(tr)

    print(f"{'币种':8s} {'笔数':>4s} {'总收益':>9s} {'年化':>7s} {'胜率':>6s} "
          f"{'均持(h)':>8s} {'funding':>9s} {'基差':>8s} {'毛/笔':>8s}")
    for sym in SYMBOLS:
        st = summarize(per_sym[sym], years, sym)
        print(f"{sym:8s} {st['trades']:>4d} {st['total_pct']:>8.2f}% {st['ann_pct']:>6.2f}% "
              f"{(st['win_rate'] or 0):>5.1f}% {(st['avg_hold_h'] or 0):>8.1f} "
              f"{st['funding_bps']:>8.2f}b {st['spread_bps']:>7.2f}b {st['gross_bps']:>7.2f}b")

    port = portfolio(all_trades, len(SYMBOLS))
    st_all = summarize(port, years, "组合(等权1/3)")
    print(f"{'组合':8s} {st_all['trades']:>4d} {st_all['total_pct']:>8.2f}% "
          f"{st_all['ann_pct']:>6.2f}% {(st_all['win_rate'] or 0):>5.1f}% "
          f"{(st_all['avg_hold_h'] or 0):>8.1f} "
          f"{st_all['funding_bps']:>8.2f}b {st_all['spread_bps']:>7.2f}b {st_all['gross_bps']:>7.2f}b")

    # 逐年
    print("\n逐自然年（组合，累加口径）")
    by_year = {}
    for t in port:
        y = time.strftime("%Y", time.gmtime(t["entry_time"] / 1000))
        by_year.setdefault(y, []).append(t["net_bps"])
    for y in sorted(by_year):
        v = by_year[y]
        print(f"  {y}: 笔数 {len(v):>2d}  收益 {sum(v)/100:+.2f}%  "
              f"胜率 {sum(1 for x in v if x > 0)/len(v)*100:.1f}%")

    # 样本外：最后 1 年
    cutoff = all_series[0]["ts"][-1] - int(365.25 * 86_400_000)
    oos = [t for t in all_trades if t["entry_time"] >= cutoff]
    oos_port = portfolio(oos, len(SYMBOLS))
    st_oos = summarize(oos_port, 1.0, "OOS-1y")

    # 2x 成本
    tr_2x = []
    for s in all_series:
        tr_2x.extend(simulate(s, args.cost_bps * 2))
    st_2x = summarize(portfolio(tr_2x, len(SYMBOLS)), years, "2x")

    print(f"\n样本外（最后 1 年）：{st_oos['trades']} 笔  "
          f"总 {st_oos['total_pct']:+.2f}%  年化 {st_oos['ann_pct']:+.2f}%")
    print(f"2x 成本 {args.cost_bps*2:g}bps：{st_2x['trades']} 笔  "
          f"总 {st_2x['total_pct']:+.2f}%  年化 {st_2x['ann_pct']:+.2f}%")

    gates = {
        "G1_full_ann_gt_5": st_all["ann_pct"] > 5.0,
        "G2_oos_ann_gt_5": st_oos["ann_pct"] > 5.0,
        "G3_2x_not_negative": st_2x["total_pct"] >= 0.0,
        "G4_mdd_le_15": st_all["mdd_pct"] <= 15.0,
        "G5_trades_ge_10": st_all["trades"] >= 10,
    }
    print("\n证伪门槛")
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL'}  {GATES[k]}")

    if st_all["trades"] < 10:
        verdict = "INCONCLUSIVE"
    elif all(gates.values()):
        verdict = "NOT_FALSIFIED"
    else:
        verdict = "FALSIFIED"
    print(f"\n判定：{verdict}")

    payload = {
        "phase": "Phase 1 spot-perp funding carry",
        "generated_at_utc": al.utc(int(time.time() * 1000)),
        "rules": {"roll_n": ROLL_N, "ann_open": ANN_OPEN, "ann_close": ANN_CLOSE,
                  "neg_n": NEG_N, "basis_min_bps": BASIS_MIN,
                  "entry": "bar[i] close signal -> bar[i+1] open",
                  "funding_window": "t_entry < f_t < t_exit (both ends excluded)"},
        "cost": {"base_bps": args.cost_bps, "legs": COST_LEGS,
                 "stress_2x_bps": args.cost_bps * 2},
        "window_years": round(years, 4),
        "data": {s["sym"]: {"bars": len(s["ts"]), "first_utc": al.utc(s["ts"][0]),
                            "last_utc": al.utc(s["ts"][-1]),
                            "funding_rows": len(s["f_ts"])} for s in all_series},
        "per_symbol": {s: summarize(per_sym[s], years, s) for s in SYMBOLS},
        "portfolio": st_all,
        "out_of_sample_1y": st_oos,
        "stress_2x": st_2x,
        "by_year": {y: {"trades": len(v), "ret_pct": round(sum(v) / 100, 4)}
                    for y, v in sorted(by_year.items())},
        "gates": gates,
        "verdict": verdict,
    }
    if args.save_trades:
        payload["trades"] = all_trades
    out = REPORT_DIR / f"carry_phase1_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    al.atomic_write(out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"结果已写入 {out}")

    if all_trades:
        print("\n前 3 笔明细")
        for t in all_trades[:3]:
            print(f"  {t['symbol']} {al.utc(t['entry_time'])} → {al.utc(t['exit_time'])} "
                  f"{t['hold_hours']:.0f}h  ann_in={t['ann_in']}%  "
                  f"basis {t['basis_in_bps']}→{t['basis_out_bps']}bps  "
                  f"funding {t['funding_ret_bps']}b  spread "
                  f"{t['spot_ret_bps']+t['perp_ret_bps']:+.2f}b  net {t['net_bps']}b")
    return 0


if __name__ == "__main__":
    sys.exit(main())
