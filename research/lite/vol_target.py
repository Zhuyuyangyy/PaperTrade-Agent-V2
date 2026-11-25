#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3：日线波动率目标 + 风险平价配置。

契约见 phase3_spec.json / phase3_spec.lock.json，启动校验，哈希失配拒跑。

两个基线，分别判定，不许合并择优：
  基线 1  BTC 单币：w = min(30% / σ20, 1)，|Δw| > 10% 才调
  基线 2  三币倒数波动率风险平价：base_i ∝ 1/σ_i，再整体缩放到 30% 目标；
          逐币 |Δw_i| > 5% 才调（防组合敞口互相抵消导致风险平价失灵）

执行口径：D-1 收盘出信号，D 开盘成交；隔夜用旧权重、日内用新权重。
成本 11bps/边（现货 taker 10 + 滑点 1），2x = 22bps。

两个只读对照（不改 Q1-Q6）：
  - 等平均仓位静态 B&H：验证动态调仓相对"静态少买一点"的增量
  - 闲置现金按 4% 年化计息：主口径仍为 0%，仅参考

用法：
    python research/lite/vol_target.py
    python research/lite/vol_target.py --save-daily
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alpha_lite as al  # noqa: E402

REPORT_DIR = HERE / "reports"
SPEC_PATH = HERE / "phase3_spec.json"
LOCK_PATH = HERE / "phase3_spec.lock.json"
DAY = 86_400_000


# ---------------------------------------------------------------- 契约

def load_contract():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    raw = SPEC_PATH.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != lock["contract_sha256"]:
        raise SystemExit(f"契约哈希失配：spec={digest[:16]}… lock={lock['contract_sha256'][:16]}…\n"
                         f"改了参数就必须重新锁。")
    return json.loads(SPEC_PATH.read_text(encoding="utf-8")), lock


# ---------------------------------------------------------------- 数据

def load_daily(sym: str, win_start: str, win_end: str):
    """现货 1h 聚合日线。返回 [(day_ms, o, h, l, c), ...]，已丢弃未收盘日。"""
    store = al.load_store(al.store_path(sym, "1h", "spot"))
    by_day = {}
    for t in sorted(store):
        d = t - (t % DAY)
        r = store[t]["r"]
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        b = by_day.get(d)
        if b is None:
            by_day[d] = [o, h, l, c, 1]
        else:
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
            b[4] += 1

    today = int(time.time() * 1000) // DAY * DAY
    w0 = int(datetime.fromisoformat(win_start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    w1 = int(datetime.fromisoformat(win_end).replace(tzinfo=timezone.utc).timestamp() * 1000) + DAY

    days = []
    for d in sorted(by_day):
        if d < w0 or d >= w1 or d >= today:
            continue
        o, h, l, c, hours = by_day[d]
        if hours < 20:  # 当天不足 20 小时视为不完整，丢弃
            continue
        days.append((d, o, h, l, c))
    if not days:
        raise SystemExit(f"{sym} 聚合后无数据")
    return days


def align(all_days):
    """取所有币共同的日期，返回 [(day, {sym: (o,l,c)})]。"""
    common = set(d[0] for d in all_days[0])
    for ds in all_days[1:]:
        common &= set(d[0] for d in ds)
    dates = sorted(common)
    idx = [{d[0]: (d[1], d[3]) for d in ds} for ds in all_days]  # (o, c)
    syms = [None] * len(idx)
    return dates, idx


# ---------------------------------------------------------------- 指标

def metrics(rets, years):
    if not rets:
        return {"days": 0}
    nav, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        nav *= (1 + r)
        peak = max(peak, nav)
        mdd = max(mdd, (peak - nav) / peak)
    ann = (nav ** (1 / years) - 1) if nav > 0 and years > 0 else -1.0
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    vol = sd * math.sqrt(365)
    sharpe = (statistics.mean(rets) / sd * math.sqrt(365)) if sd > 0 else None
    calmar = (ann / mdd) if mdd > 0 else None
    return {
        "days": len(rets),
        "final_nav": round(nav, 4),
        "total_pct": round((nav - 1) * 100, 4),
        "ann_pct": round(ann * 100, 4),
        "ann_vol_pct": round(vol * 100, 4),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "mdd_pct": round(mdd * 100, 4),
        "calmar": round(calmar, 3) if calmar is not None else None,
    }


# ---------------------------------------------------------------- 基线 1

def run_baseline1(dates, btc, spec, cost_mult=1.0, cash_yield=0.0):
    target = spec["volatility"]["target_annual"]
    win = spec["volatility"]["window_days"]
    buf = spec["baseline1"]["buffer_abs"]
    cost_bps = spec["cost"]["per_side_bps"] * cost_mult
    cash_daily = (1 + cash_yield) ** (1 / 365) - 1

    rets = [btc[i][3] / btc[i - 1][3] - 1 for i in range(1, len(btc))]
    rets = [0.0] + rets  # 与 btc 索引对齐

    out, w, n_rebal, gross_sum = [], 0.0, 0, 0.0
    for i in range(win + 1, len(btc)):
        hist = rets[i - win:i]
        sig = statistics.stdev(hist) * math.sqrt(365)
        tgt = min(target / sig, 1.0) if sig > 0 else 0.0
        d = abs(tgt - w)
        cost = d * cost_bps / 10_000.0
        if d > buf:
            w_new = tgt
            n_rebal += 1
        else:
            w_new = w
            cost = 0.0
        overnight = w * (btc[i][1] / btc[i - 1][3] - 1)
        intraday = w_new * (btc[i][3] / btc[i][1] - 1)
        cash = max(0.0, 1.0 - w_new) * cash_daily
        out.append((dates[i], overnight + intraday + cash - cost))
        gross_sum += w_new
        w = w_new
    return out, n_rebal, gross_sum / max(1, len(out))


# ---------------------------------------------------------------- 基线 2

def run_baseline2(dates, series, spec, cost_mult=1.0, cash_yield=0.0):
    """series: {sym: [(day, o, h, l, c), ...]} 已按 dates 对齐"""
    syms = spec["baseline2"]["symbols"]
    target = spec["volatility"]["target_annual"]
    win = spec["volatility"]["window_days"]
    buf = spec["baseline2"]["buffer_per_coin_abs"]
    cost_bps = spec["cost"]["per_side_bps"] * cost_mult
    cash_daily = (1 + cash_yield) ** (1 / 365) - 1

    rets = {}
    for s in syms:
        r = [series[s][i][3] / series[s][i - 1][3] - 1 for i in range(1, len(dates))]
        rets[s] = [0.0] + r

    w = {s: 0.0 for s in syms}
    out, n_rebal, gross_sum = [], 0, 0.0
    for i in range(win + 1, len(dates)):
        sig = {}
        for s in syms:
            sig[s] = statistics.stdev(rets[s][i - win:i]) * math.sqrt(365)
        inv = {s: (1.0 / sig[s] if sig[s] > 0 else 0.0) for s in syms}
        tot = sum(inv.values())
        if tot <= 0:
            continue
        base = {s: inv[s] / tot for s in syms}
        port_hist = [sum(base[s] * rets[s][k] for s in syms) for k in range(i - win, i)]
        sig_p = statistics.stdev(port_hist) * math.sqrt(365)
        k = min(target / sig_p, 1.0) if sig_p > 0 else 0.0
        tgt = {s: k * base[s] for s in syms}

        w_new, cost = {}, 0.0
        for s in syms:
            if abs(tgt[s] - w[s]) > buf:
                w_new[s] = tgt[s]
                cost += abs(tgt[s] - w[s])
            else:
                w_new[s] = w[s]
        cost = cost * cost_bps / 10_000.0
        if cost > 0:
            n_rebal += 1

        overnight = sum(w[s] * (series[s][i][1] / series[s][i - 1][3] - 1) for s in syms)
        intraday = sum(w_new[s] * (series[s][i][3] / series[s][i][1] - 1) for s in syms)
        cash = max(0.0, 1.0 - sum(w_new.values())) * cash_daily
        out.append((dates[i], overnight + intraday + cash - cost))
        gross_sum += sum(w_new.values())
        w = w_new
    return out, n_rebal, gross_sum / max(1, len(out))


# ---------------------------------------------------------------- 基准

def run_bh(dates, series, weights, spec, start_i, cost_mult=1.0):
    cost_bps = spec["cost"]["per_side_bps"] * cost_mult
    syms = list(weights)
    init_cost = sum(abs(weights[s]) for s in syms) * cost_bps / 10_000.0
    out = []
    for i in range(start_i, len(dates)):
        if i == start_i:
            r = sum(weights[s] * (series[s][i][3] / series[s][i][1] - 1) for s in syms) - init_cost
        else:
            r = sum(weights[s] * (series[s][i][3] / series[s][i - 1][3] - 1) for s in syms)
        out.append((dates[i], r))
    return out


def run_static(dates, series, weights, avg_gross, spec, start_i, cost_mult=1.0):
    """等平均仓位静态 B&H：期初按 avg_gross 建仓，之后不动。"""
    scaled = {s: w * avg_gross for s, w in weights.items()}
    return run_bh(dates, series, scaled, spec, start_i, cost_mult)


# ---------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="Phase 3: 波动率目标 + 风险平价")
    p.add_argument("--save-daily", action="store_true")
    args = p.parse_args(argv)

    spec, lock = load_contract()
    print(f"契约 {lock['contract_sha256'][:16]}…  锁于 {lock['locked_at_utc']}")
    print(f"目标波动 {spec['volatility']['target_annual']*100:.0f}%  "
          f"窗口 {spec['volatility']['window_days']}d  "
          f"成本 {spec['cost']['per_side_bps']}bps/边")

    syms = spec["data"]["symbols"]
    raw = {s: load_daily(s, spec["data"]["window"][0], spec["data"]["window"][1]) for s in syms}
    for s in syms:
        print(f"{s}: {len(raw[s])} 天  {al.utc(raw[s][0][0])[:10]} → {al.utc(raw[s][-1][0])[:10]}")

    dates = sorted(set(d[0] for d in raw[syms[0]])
                   & set(d[0] for d in raw[syms[1]])
                   & set(d[0] for d in raw[syms[2]]))
    idx = {s: {d[0]: d for d in raw[s]} for s in syms}
    series = {s: [idx[s][t] for t in dates] for s in syms}
    print(f"对齐 {len(dates)} 天\n")

    bt_start = int(datetime.fromisoformat(spec["data"]["backtest_start"])
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    oos_start = int(datetime.fromisoformat(spec["split"]["oos"][0])
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
    start_idx = next(i for i, t in enumerate(dates) if t >= bt_start)
    years_full = (dates[-1] - dates[start_idx]) / (365.25 * DAY)
    years_oos = (dates[-1] - oos_start) / (365.25 * DAY)

    def split_and_score(pairs, cost_mult):
        full = [r for _, r in pairs]
        oos = [r for t, r in pairs if t >= oos_start]
        return metrics(full, len(full) / 365.25), metrics(oos, len(oos) / 365.25)

    results = {}
    # 基线 1
    d1, n1, avg1 = run_baseline1([t for t in dates[start_idx:]], series["BTCUSDT"][start_idx:], spec)
    m1_full, m1_oos = split_and_score(d1, 1.0)
    results["baseline1"] = {"full": m1_full, "oos": m1_oos, "rebalances": n1,
                            "avg_gross": round(avg1, 4), "daily": d1}

    d1x, n1x, _ = run_baseline1([t for t in dates[start_idx:]], series["BTCUSDT"][start_idx:],
                                spec, cost_mult=2.0)
    m1x_full, m1x_oos = split_and_score(d1x, 2.0)
    results["baseline1_2x"] = {"full": m1x_full, "oos": m1x_oos, "rebalances": n1x}

    # 基线 2
    sub = {s: series[s][start_idx:] for s in syms}
    d2, n2, avg2 = run_baseline2([t for t in dates[start_idx:]], sub, spec)
    m2_full, m2_oos = split_and_score(d2, 1.0)
    results["baseline2"] = {"full": m2_full, "oos": m2_oos, "rebalances": n2,
                            "avg_gross": round(avg2, 4), "daily": d2}

    d2x, n2x, _ = run_baseline2([t for t in dates[start_idx:]], sub, spec, cost_mult=2.0)
    m2x_full, m2x_oos = split_and_score(d2x, 2.0)
    results["baseline2_2x"] = {"full": m2x_full, "oos": m2x_oos, "rebalances": n2x}

    # 基准（与策略同起点：跳过 warmup）
    START_I = spec["volatility"]["window_days"] + 1
    bh_btc = run_bh([t for t in dates[start_idx:]], sub, {"BTCUSDT": 1.0}, spec, START_I)
    bh_eq = run_bh([t for t in dates[start_idx:]], sub,
                   {"BTCUSDT": 1 / 3, "ETHUSDT": 1 / 3, "SOLUSDT": 1 / 3}, spec, START_I)
    b1_full, b1_oos = split_and_score(bh_btc, 1.0)
    b2_full, b2_oos = split_and_score(bh_eq, 1.0)
    results["btc_bh"] = {"full": b1_full, "oos": b1_oos}
    results["equal_bh"] = {"full": b2_full, "oos": b2_oos}

    # 只读对照 1：等平均仓位静态 B&H
    st1 = run_static([t for t in dates[start_idx:]], sub, {"BTCUSDT": 1.0}, avg1, spec, START_I)
    st2 = run_static([t for t in dates[start_idx:]], sub,
                     {"BTCUSDT": 1 / 3, "ETHUSDT": 1 / 3, "SOLUSDT": 1 / 3}, avg2, spec, START_I)
    s1_full, s1_oos = split_and_score(st1, 1.0)
    s2_full, s2_oos = split_and_score(st2, 1.0)
    results["static_btc_at_avg1"] = {"full": s1_full, "oos": s1_oos, "avg_gross": round(avg1, 4)}
    results["static_eq_at_avg2"] = {"full": s2_full, "oos": s2_oos, "avg_gross": round(avg2, 4)}

    # 只读对照 2：现金计息 4%（仅基线 1/2）
    cy1, _, _ = run_baseline1([t for t in dates[start_idx:]], series["BTCUSDT"][start_idx:],
                              spec, cash_yield=0.04)
    cy2, _, _ = run_baseline2([t for t in dates[start_idx:]], sub, spec, cash_yield=0.04)
    c1_full, c1_oos = split_and_score(cy1, 1.0)
    c2_full, c2_oos = split_and_score(cy2, 1.0)
    results["baseline1_cash4"] = {"full": c1_full, "oos": c1_oos}
    results["baseline2_cash4"] = {"full": c2_full, "oos": c2_oos}

    # 打印
    def line(label, r):
        f, o = r["full"], r["oos"]
        print(f"{label:22s} 全 {f['ann_pct']:+7.2f}% 夏 {f['sharpe']} "
              f"回撤 {f['mdd_pct']:6.2f}% 卡玛 {f['calmar']}  |  "
              f"OOS {o['ann_pct']:+7.2f}% 夏 {o['sharpe']} 回撤 {o['mdd_pct']:5.2f}%")

    print("=== 基线 ===")
    line("BTC B&H", results["btc_bh"])
    line("三币等权 B&H", results["equal_bh"])
    print("=== 策略 ===")
    line("基线1 BTC vol-target", results["baseline1"])
    line("基线2 三币 RP+VT", results["baseline2"])
    print("=== 2x 成本 ===")
    line("基线1 2x", results["baseline1_2x"])
    line("基线2 2x", results["baseline2_2x"])
    print("=== 只读对照 ===")
    line(f"静态 BTC @{avg1*100:.1f}%", results["static_btc_at_avg1"])
    line(f"静态等权 @{avg2*100:.1f}%", results["static_eq_at_avg2"])
    line("基线1 + 现金4%", results["baseline1_cash4"])
    line("基线2 + 现金4%", results["baseline2_cash4"])

    print(f"\n调仓次数：基线1 {n1}（{n1/years_full:.1f}/年）  基线2 {n2}（{n2/years_full:.1f}/年）")
    print(f"平均仓位：基线1 {avg1*100:.1f}%  基线2 {avg2*100:.1f}%")

    # 判定
    def judge(tag, base_key, x2_key, n_rebal):
        f = results[base_key]["full"]
        o = results[base_key]["oos"]
        f2 = results[x2_key]["full"]
        o2 = results[x2_key]["oos"]
        bh_f, bh_o = results["btc_bh"]["full"], results["btc_bh"]["oos"]
        g = {
            "Q1": (f["sharpe"] or -9) > (bh_f["sharpe"] or 9),
            "Q2": f["mdd_pct"] <= bh_f["mdd_pct"] * 0.70,
            "Q3": (f["calmar"] or -9) > (bh_f["calmar"] or 9),
            "Q4": (o["sharpe"] or -9) >= (bh_o["sharpe"] or 9) and o["mdd_pct"] <= bh_o["mdd_pct"] * 0.70,
            "Q5": ((f2["sharpe"] or -9) > (bh_f["sharpe"] or 9)
                   and f2["mdd_pct"] <= bh_f["mdd_pct"] * 0.70
                   and (f2["calmar"] or -9) > (bh_f["calmar"] or 9)
                   and (o2["sharpe"] or -9) >= (bh_o["sharpe"] or 9)
                   and o2["mdd_pct"] <= bh_o["mdd_pct"] * 0.70),
            "Q6": (n_rebal / years_full) <= 10,
        }
        if all(g.values()):
            v = "NOT_FALSIFIED"
        elif g["Q2"] and g["Q3"] and g["Q4"] and not g["Q1"]:
            v = "PARTIAL"
        else:
            v = "FALSIFIED"
        return g, v

    g1, v1 = judge("baseline1", "baseline1", "baseline1_2x", n1)
    g2, v2 = judge("baseline2", "baseline2", "baseline2_2x", n2)
    for tag, g, v in (("基线1", g1, v1), ("基线2", g2, v2)):
        print(f"\n{tag} 门槛")
        for k, ok in g.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {k}")
        print(f"  判定：{v}")

    payload = {
        "phase": "Phase 3 vol target + risk parity",
        "generated_at_utc": al.utc(int(time.time() * 1000)),
        "contract_sha256": lock["contract_sha256"],
        "spec": spec,
        "window": {"start": al.utc(dates[start_idx]), "end": al.utc(dates[-1]),
                   "oos_start": al.utc(oos_start)},
        "results": {k: {kk: vv for kk, vv in v.items() if kk != "daily"}
                    for k, v in results.items()},
        "gates": {"baseline1": g1, "baseline2": g2},
        "verdict": {"baseline1": v1, "baseline2": v2},
    }
    if args.save_daily:
        for k in ("baseline1", "baseline2"):
            rows = "\n".join(f"{al.utc(t)},{r:.8f}" for t, r in results[k]["daily"])
            al.atomic_write(REPORT_DIR / f"phase3_{k}_daily.csv",
                            "date_utc,daily_ret\n" + rows + "\n")
    out = REPORT_DIR / f"phase3_{int(time.time())}.json"
    al.atomic_write(out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"\n结果已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
