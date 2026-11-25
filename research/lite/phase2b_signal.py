#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2B：离散度 FLAT 门控 + 纯 Funding 截面（收官实验，限跑 1 次）。

与 Phase 2 的唯一区别在信号层：
  - 排序依据：过去 14 日（42 次结算）平均 funding 年化，去掉动量
  - 空仓机制：截面离散度低于「样本内 30% 分位」时整周 FLAT

防前视：
  1. 每个调仓日 D 的信号只用 D-1 及之前的日线 / 结算
  2. 成交在 D 的 open
  3. 阈值只在样本内（前 2 年）拟合，样本外使用冻结常数

复用的部分（成本、funding 计提、撮合、统计）全部来自 cross_section.py，
不重写，避免两套口径产生偏差。

用法：
    python research/lite/phase2b_signal.py run
"""

from __future__ import annotations

import argparse
import bisect
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
import cross_section as cs  # noqa: E402

SPEC_PATH = HERE / "phase2b_spec.json"
LOCK_PATH = HERE / "phase2b_spec.lock.json"
REPORT_DIR = HERE / "reports"
DAY_MS = cs.DAY_MS


# ---------------------------------------------------------------- 契约

def load_contract():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    raw = SPEC_PATH.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != lock["contract_sha256"]:
        raise SystemExit(
            f"契约哈希失配：spec={digest[:16]}… lock={lock['contract_sha256'][:16]}…\n"
            f"改了参数就必须重新锁，不能在旧锁下跑新规则。"
        )
    return json.loads(SPEC_PATH.read_text(encoding="utf-8")), lock


def percentile_linear(values, q: float):
    """线性插值分位数：idx = q * (n - 1)，与 numpy 默认 'linear' 一致。"""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    idx = q * (n - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


# ---------------------------------------------------------------- 信号

def build_plans(spec, bars_all, fund_all):
    """每个调仓日：动态 Top12 池、纯 funding 排序、池内 14 日截面离散度。"""
    n_settle = spec["signal"]["window_settlements"]
    plans = []
    for ts in cs.rebalance_dates(spec):
        adv, ret14, fscore = {}, {}, {}
        for sym, bars in bars_all.items():
            closes, qvs = [], []
            ok = True
            for k in range(1, 31):
                b = bars.get(ts - k * DAY_MS)
                if b is None:
                    ok = False
                    break
                closes.append(b["c"])
                qvs.append(b["qv"])
            if not ok:
                continue
            adv[sym] = sum(qvs) / 30.0
            ret14[sym] = closes[0] / closes[14] - 1.0
            fts, frate = fund_all[sym]
            i = bisect.bisect_right(fts, ts - 1)
            if i >= n_settle:
                fscore[sym] = statistics.mean(frate[i - n_settle:i]) * 1095 * 100

        pool = sorted(adv, key=lambda x: adv[x], reverse=True)[:spec["dynamic_pool"]["size"]]
        if len(pool) < 6:
            continue
        # 离散度：成交额池内 14 日收益的截面标准差（全部成员都有 30 天日线，必有 ret14）
        disp = statistics.stdev([ret14[s] for s in pool]) * 100

        rankable = [s for s in pool if s in fscore]
        if len(rankable) < 6:
            continue
        ranked = sorted(rankable, key=lambda s: fscore[s], reverse=True)
        shorts, longs = ranked[:3], ranked[-3:]
        w = {}
        for s in longs:
            w[s] = 1.0 / 6.0
        for s in shorts:
            w[s] = -1.0 / 6.0
        plans.append({
            "ts": ts, "pool": pool, "rankable": rankable, "w": w,
            "longs": longs, "shorts": shorts, "disp": round(disp, 4),
            "funding": {s: round(fscore[s], 3) for s in rankable},
        })
    return plans


def apply_gate(plans, threshold):
    """低于阈值 -> 空权重（FLAT）。阈值是整个回测期共用的冻结常数。"""
    return [p["w"] if p["disp"] >= threshold else {} for p in plans]


# ---------------------------------------------------------------- 统计

def leg_pnl(plans, weights, cache):
    legs = {"long": 0.0, "short": 0.0}
    for i, w in enumerate(weights):
        for k in range(7):
            for s, wt in w.items():
                legs["long" if wt > 0 else "short"] += wt * cache[i][k].get(s, 0.0)
    return legs


def flat_ratio(plans, threshold, start=None, end=None):
    sel = [p for p in plans
           if (start is None or p["ts"] >= start) and (end is None or p["ts"] < end)]
    if not sel:
        return None, 0
    flat = sum(1 for p in sel if p["disp"] < threshold)
    return round(flat / len(sel) * 100, 2), len(sel)


def split_metrics(plans, weights, cache, cost, oos0, years_all, years_oos):
    daily, segs, _, turnover = cs.run_weights(weights, cache, cost)
    oos_idx = next((i for i, p in enumerate(plans) if p["ts"] >= oos0), len(plans))
    cut = oos_idx * 7
    allsum = cs.summarize(daily, segs, leg_pnl(plans, weights, cache), turnover, years_all)
    oos_sum = cs.summarize(daily[cut:], segs[oos_idx:],
                           leg_pnl(plans[oos_idx:], weights[oos_idx:],
                                   cache[oos_idx:]), 0.0, years_oos)
    return allsum, oos_sum, daily, segs


# ---------------------------------------------------------------- 主流程

def run(args) -> int:
    spec, lock = load_contract()
    print(f"契约 {lock['contract_sha256'][:16]}…  锁于 {lock['locked_at_utc']}")
    bars_all, fund_all = cs.load_all(spec)
    btc_days = sorted(bars_all["BTCUSDT"])
    print(f"池子 {len(bars_all)} 币，数据 {al.utc(btc_days[0])[:10]} → "
          f"{al.utc(btc_days[-1])[:10]}（{len(btc_days)} 天）")

    W0 = int(datetime(2023, 11, 27, tzinfo=timezone.utc).timestamp() * 1000)
    W1 = int(datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp() * 1000)
    OOS0 = int(datetime(2025, 11, 25, tzinfo=timezone.utc).timestamp() * 1000)
    years_all = (W1 - W0) / (365.25 * DAY_MS)
    years_oos = (W1 - OOS0) / (365.25 * DAY_MS)

    plans = build_plans(spec, bars_all, fund_all)
    cache = cs.build_cache(plans, bars_all, fund_all, spec)
    print(f"{len(plans)} 次调仓  {al.utc(plans[0]['ts'])[:10]} → {al.utc(plans[-1]['ts'])[:10]}")

    # 阈值只在样本内拟合，之后作为常数冻结
    is_disp = [p["disp"] for p in plans if p["ts"] < OOS0]
    oos_disp = [p["disp"] for p in plans if p["ts"] >= OOS0]
    thr = percentile_linear(is_disp, spec["dispersion_gate"]["percentile"])
    print(f"\n离散度：IS n={len(is_disp)} 中位 {statistics.median(is_disp):.2f}% "
          f"| OOS n={len(oos_disp)} 中位 {statistics.median(oos_disp):.2f}%")
    print(f"阈值 = IS 30% 分位 = {thr:.4f}%（样本外冻结为该常数）")

    gated_w = apply_gate(plans, thr)
    ungated_w = [p["w"] for p in plans]

    results = {}
    for mult in (1.0, 2.0):
        cost = spec["cost"]["roundtrip_bps"] * mult
        tag = "1x" if mult == 1 else "2x"
        a, o, daily, segs = split_metrics(plans, gated_w, cache, cost, OOS0, years_all, years_oos)
        results[f"gated_{tag}"] = {"all": a, "oos": o}
        ua, uo, _, _ = split_metrics(plans, ungated_w, cache, cost, OOS0, years_all, years_oos)
        results[f"ungated_{tag}"] = {"all": ua, "oos": uo}
        print(f"\n[{tag}] 门控版 全样本 收益 {a['total_pct']:+.2f}% 年化 {a['ann_pct']:+.2f}% "
              f"夏普 {a['sharpe']} MDD {a['mdd_pct']:.2f}%")
        print(f"       门控版 样本外 收益 {o['total_pct']:+.2f}% 年化 {o['ann_pct']:+.2f}% "
              f"夏普 {o['sharpe']} MDD {o['mdd_pct']:.2f}%")
        print(f"       诊断 无门控 全样本 {ua['total_pct']:+.2f}% / 夏普 {ua['sharpe']}  "
              f"样本外 {uo['total_pct']:+.2f}% / 夏普 {uo['sharpe']}")

    is_ratio, is_n = flat_ratio(plans, thr, None, OOS0)
    oos_ratio, oos_n = flat_ratio(plans, thr, OOS0, None)
    traded_all = sum(1 for w in gated_w if w)
    traded_oos = sum(1 for i, p in enumerate(plans) if p["ts"] >= OOS0 and gated_w[i])
    print(f"\nFLAT 占比：IS {is_ratio}%（{is_n} 次）| OOS {oos_ratio}%（{oos_n} 次）")
    print(f"实际持仓周：全样本 {traded_all} | 样本外 {traded_oos}")

    # 随机对照：同池同日、同样 FLAT 排程、随机 3 多 3 空
    rng = __import__("random").Random(20261125)
    real_sh = results["gated_1x"]["all"]["sharpe"]
    sims = []
    for _ in range(args.mc):
        ws = []
        for i, p in enumerate(plans):
            if not gated_w[i]:
                ws.append({})
                continue
            picks = rng.sample(p["rankable"], 6)
            w = {}
            for s in picks[:3]:
                w[s] = 1 / 6
            for s in picks[3:]:
                w[s] = -1 / 6
            ws.append(w)
        d, _, _, _ = cs.run_weights(ws, cache, spec["cost"]["roundtrip_bps"])
        sims.append(cs.sharpe(d))
    sims = [x for x in sims if x is not None]
    sims.sort()
    better = sum(1 for x in sims if x >= (real_sh or 0))
    p95 = sims[int(len(sims) * 0.95)]
    rand = {
        "iterations": len(sims), "real_sharpe": real_sh,
        "random_mean": round(statistics.mean(sims), 3),
        "random_p95": round(p95, 3),
        "p_value": round(better / len(sims), 4),
        "real_percentile": round((1 - better / len(sims)) * 100, 2),
    }
    print(f"\n随机对照 {len(sims)} 次（同池同日、同 FLAT 排程）：真实 {real_sh}  "
          f"随机均值 {rand['random_mean']}  p95 {p95}  p={rand['p_value']}  "
          f"分位 {rand['real_percentile']}%")

    # 门控的机会成本：被 FLAT 的那些周，无门控版实际赚/亏了多少
    _, _, ungated_daily, ungated_segs = split_metrics(
        plans, ungated_w, cache, spec["cost"]["roundtrip_bps"], OOS0, years_all, years_oos)
    skipped = sum(ungated_segs[i]["ret"] for i, w in enumerate(gated_w) if not w)
    skipped_cost = sum(ungated_segs[i]["turnover"] * 12.0 / 10_000.0
                       for i, w in enumerate(gated_w) if not w)
    print(f"被 FLAT 的 {len(plans) - traded_all} 周，无门控版合计 {skipped * 100:+.2f}%"
          f"（其中成本 {skipped_cost * 100:.2f}%）→ 空仓{'省下' if skipped < 0 else '错过'}")

    gates = {
        "Q1_oos_sharpe_gt_0.8_1x": (results["gated_1x"]["oos"]["sharpe"] or -9) > 0.8,
        "Q2_oos_sharpe_gt_0.8_2x": (results["gated_2x"]["oos"]["sharpe"] or -9) > 0.8,
        "Q3_oos_return_gt_0_both": (results["gated_1x"]["oos"]["total_pct"] > 0
                                    and results["gated_2x"]["oos"]["total_pct"] > 0),
        "Q4_beats_random_p95_full": (real_sh is not None and real_sh > p95),
        "Q5_traded_rebalances_ge_20": traded_all >= 20,
        "Q6_both_legs_positive": (results["gated_1x"]["all"]["long_pnl_pct"] > 0
                                  and results["gated_1x"]["all"]["short_pnl_pct"] > 0),
    }
    print("\n门槛")
    for k, v in gates.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

    if traded_oos < 12:
        verdict = "INCONCLUSIVE"
        why = f"样本外实际持仓仅 {traded_oos} 周 < 12，OOS 指标不可评估"
    elif all(gates.values()):
        verdict = "NOT_FALSIFIED"
        why = "六条门槛全过"
    else:
        verdict = "FALSIFIED"
        why = "门槛未全过：" + ", ".join(k for k, v in gates.items() if not v)
    print(f"\n判定：{verdict}（{why}）")

    # 逐年离散度分布
    by_year = {}
    for p in plans:
        y = datetime.fromtimestamp(p["ts"] / 1000, tz=timezone.utc).year
        by_year.setdefault(y, []).append(p["disp"])
    disp_by_year = {str(y): {"n": len(v), "median": round(statistics.median(v), 3),
                            "p30": round(percentile_linear(v, 0.30), 3)}
                    for y, v in sorted(by_year.items())}

    payload = {
        "phase": "Phase 2B dispersion-gated pure funding",
        "generated_at_utc": al.utc(int(time.time() * 1000)),
        "contract_sha256": lock["contract_sha256"],
        "spec": spec,
        "window": {"start": al.utc(W0), "end": al.utc(W1), "oos_start": al.utc(OOS0)},
        "threshold": {"value_pct": round(thr, 6), "fit_n": len(is_disp),
                      "fit_median_pct": round(statistics.median(is_disp), 4),
                      "oos_median_pct": round(statistics.median(oos_disp), 4)},
        "dispersion_by_year": disp_by_year,
        "flat": {"is_ratio_pct": is_ratio, "oos_ratio_pct": oos_ratio,
                 "traded_all": traded_all, "traded_oos": traded_oos,
                 "total_rebalances": len(plans)},
        "skipped_weeks_ungated_pnl_pct": round(skipped * 100, 4),
        "skipped_weeks_ungated_cost_pct": round(skipped_cost * 100, 4),
        "results": results,
        "random_control": rand,
        "gates": gates,
        "verdict": verdict,
        "verdict_reason": why,
    }
    out = REPORT_DIR / f"phase2b_{int(time.time())}.json"
    al.atomic_write(out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"结果已写入 {out}")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="Phase 2B: 离散度门控 + 纯 funding 截面")
    sub = p.add_subparsers(dest="cmd", required=True)
    sr = sub.add_parser("run")
    sr.add_argument("--mc", type=int, default=2000)
    sr.set_defaults(func=run)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
