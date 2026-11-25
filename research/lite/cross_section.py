#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2：Top-12 动态池截面动量（1d 周频）。

契约见 phase2_spec.json / phase2_spec.lock.json，启动时校验哈希，不一致拒跑。

三条防前视：
  1. 调仓日 D 的信号只用 D-1 及之前的日线（动量 14 日、成交额 30 日）
  2. 成交在 D 的 open，不在 D-1 收盘
  3. 动态池用 trailing 30 日 quote_volume，不含当日；数据不足 14 日的币不进池

用法：
    python research/lite/cross_section.py pull
    python research/lite/cross_section.py verify
    python research/lite/cross_section.py run
    python research/lite/cross_section.py run --mc 2000 --save-trades
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alpha_lite as al  # noqa: E402

DATA_DIR = HERE / "data" / "perp1d"
REPORT_DIR = HERE / "reports"
SPEC_PATH = HERE / "phase2_spec.json"
LOCK_PATH = HERE / "phase2_spec.lock.json"

DAY_MS = 86_400_000
FAPI = "https://fapi.binance.com"
START_MS = int(datetime(2023, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_MS = int(datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp() * 1000)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，绕开不稳的本地代理


# ---------------------------------------------------------------- 契约

def load_contract():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    # 换行规范化后再算哈希：避免 Windows checkout 出 CRLF 导致跨平台失配
    raw = SPEC_PATH.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != lock["contract_sha256"]:
        raise SystemExit(
            f"契约哈希失配：spec={digest[:16]}… lock={lock['contract_sha256'][:16]}…\n"
            f"改了参数就必须重新锁，不能在旧锁下跑新规则。"
        )
    return json.loads(SPEC_PATH.read_text(encoding="utf-8")), lock


# ---------------------------------------------------------------- 取数

def fapi_get(path: str, params: dict, retries: int = 3):
    url = FAPI + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "alpha-lite/1.0"})
            with _OPENER.open(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                raise SystemExit(f"限频 HTTP {exc.code}，Retry-After="
                                 f"{exc.headers.get('Retry-After','?')}，立即停止。") from exc
            if exc.code == 400:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(1.0 + attempt)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(1.0 + attempt)


def server_time() -> int:
    return int(fapi_get("/fapi/v1/time", {})["serverTime"])


def pull(args) -> int:
    spec, _ = load_contract()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    st = server_time()
    print(f"fapi serverTime={al.utc(st)}")
    print(f"池子 {len(spec['universe'])} 个，窗口 {al.utc(START_MS)} → {al.utc(END_MS)}")

    total_req = 0
    for sym in spec["universe"]:
        # 1d K 线：一次最多 1500 根，窗口内约 1150 根，一页够
        rows = fapi_get("/fapi/v1/klines", {
            "symbol": sym, "interval": "1d",
            "startTime": START_MS, "endTime": END_MS, "limit": 1500,
        })
        total_req += 1
        closed = [r for r in rows if int(r[6]) < st]
        dropped = len(rows) - len(closed)
        recs = [{"t": int(r[0]), "r": r, "h": al.sha16(r)} for r in closed]
        al.atomic_write(DATA_DIR / f"{sym}_1d.jsonl",
                        "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in recs))

        # funding：1000 条/页，3 年约 3300 条，4 页
        fund, cursor, pages = [], START_MS, 0
        while True:
            fr = fapi_get("/fapi/v1/fundingRate", {
                "symbol": sym, "startTime": cursor, "endTime": END_MS, "limit": 1000,
            })
            total_req += 1
            pages += 1
            if not fr:
                break
            for x in fr:
                t = int(x["fundingTime"])
                if t <= START_MS or t > END_MS:
                    continue
                fund.append({"t": t, "r": x["fundingRate"], "m": x["markPrice"]})
            if len(fr) < 1000:
                break
            cursor = int(fr[-1]["fundingTime"]) + 1
            time.sleep(0.15)

        # 去重 + 排序 + 哈希
        uniq = {}
        for x in fund:
            uniq[x["t"]] = {"t": x["t"], "r": x["r"], "h": al.sha16([x["t"], x["r"], x["m"]])}
        frecs = [uniq[t] for t in sorted(uniq)]
        al.atomic_write(DATA_DIR / f"{sym}_funding.jsonl",
                        "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in frecs))

        man = {
            "symbol": sym, "interval": "1d",
            "source": f"{FAPI}/fapi/v1/klines + /fapi/v1/fundingRate",
            "window": [al.utc(START_MS), al.utc(END_MS)],
            "bars": len(recs),
            "first_utc": al.utc(recs[0]["t"]) if recs else None,
            "last_utc": al.utc(recs[-1]["t"]) if recs else None,
            "dropped_unclosed": dropped,
            "funding_rows": len(frecs),
            "bars_fingerprint": al.fingerprint(recs),
            "funding_fingerprint": al.fingerprint(frecs),
        }
        al.atomic_write(DATA_DIR / f"{sym}_1d.manifest.json",
                        json.dumps(man, indent=2, ensure_ascii=False) + "\n")
        print(f"  {sym:16s} bars {len(recs):>4d}  {man['first_utc'][:10]} → {man['last_utc'][:10]}  "
              f"funding {len(frecs):>4d}  请求 {pages + 1}")
        time.sleep(args.sleep)

    print(f"\n完成，共 {total_req} 次请求，零密钥。")
    return 0


# ---------------------------------------------------------------- 载入与校验

def load_symbol(sym: str):
    bp = DATA_DIR / f"{sym}_1d.jsonl"
    fp = DATA_DIR / f"{sym}_funding.jsonl"
    if not bp.exists() or not fp.exists():
        raise SystemExit(f"{sym} 数据缺失，先跑 pull")
    bars = {}
    for line in bp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if al.sha16(rec["r"]) != rec["h"]:
            raise SystemExit(f"{sym} 1d 哈希不匹配 t={rec['t']}")
        r = rec["r"]
        bars[int(r[0])] = {"o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                           "c": float(r[4]), "v": float(r[5]), "qv": float(r[7]),
                           "ct": int(r[6])}
    fund = {}
    for line in fp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        fund[int(rec["t"])] = float(rec["r"])
    return bars, fund


def verify(args) -> int:
    spec, _ = load_contract()
    ok = True
    for sym in spec["universe"]:
        bars, fund = load_symbol(sym)
        ts = sorted(bars)
        gaps = [t for a, t in zip(ts, ts[1:]) if t - a != DAY_MS]
        ft = sorted(fund)
        fgaps = [t for a, t in zip(ft, ft[1:]) if not (7 * 3600_000 <= t - a <= 9 * 3600_000)]
        zero_qv = sum(1 for t in ts if bars[t]["qv"] <= 0)
        flag = "" if (not gaps and not fgaps and zero_qv == 0) else "  <-- CHECK"
        if flag:
            ok = False
        print(f"{sym:16s} 1d {len(ts):>4d} 缺口 {len(gaps):>3d}  "
              f"funding {len(ft):>4d} 异常间隔 {len(fgaps):>2d}  零成交额 {zero_qv:>3d}{flag}")
    # 签名校验
    for sym in spec["universe"]:
        m = json.loads((DATA_DIR / f"{sym}_1d.manifest.json").read_text(encoding="utf-8"))
        recs = []
        for line in (DATA_DIR / f"{sym}_1d.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                recs.append(json.loads(line))
        if al.fingerprint(recs) != m["bars_fingerprint"]:
            print(f"{sym} fingerprint 不一致")
            ok = False
    print("\n全部通过" if ok else "\n存在问题，需处理")
    return 0 if ok else 1


# ---------------------------------------------------------------- 回测

def load_all(spec):
    bars_all, fund_all = {}, {}
    for sym in spec["universe"]:
        bars, fund = load_symbol(sym)
        bars_all[sym] = bars
        ts = sorted(fund)
        fund_all[sym] = (ts, [fund[t] for t in ts])
    return bars_all, fund_all


def rebalance_dates(spec):
    start = datetime.fromisoformat(spec["rebalance"]["first_rebalance"]).replace(tzinfo=timezone.utc)
    end = datetime(2026, 11, 25, tzinfo=timezone.utc)
    out, d = [], start
    while d < end:
        out.append(int(d.timestamp() * 1000))
        d += timedelta(days=spec["rebalance"]["freq_days"])
    return out


def zscore(xs):
    if len(xs) < 2:
        return [0.0] * len(xs)
    m = statistics.mean(xs)
    sd = statistics.stdev(xs)
    return [(x - m) / sd if sd > 0 else 0.0 for x in xs]


def build_plans(spec, bars_all, fund_all, variant: str):
    """variant='A' 纯动量；'B' 动量 + funding 复合。"""
    plans = []
    for ts in rebalance_dates(spec):
        adv, mom, ann = {}, {}, {}
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
            ret14 = closes[0] / closes[14] - 1.0
            rets = [closes[i] / closes[i + 1] - 1.0 for i in range(14)]
            vol = statistics.stdev(rets)
            if vol <= 0:
                continue
            mom[sym] = ret14 / vol
            fts, frate = fund_all[sym]
            i = bisect.bisect_right(fts, ts - 1)
            ann[sym] = (statistics.mean(frate[i - 9:i]) * 1095 * 100) if i >= 9 else 0.0

        pool = [s for s in sorted(adv, key=lambda x: adv[x], reverse=True)[:12] if s in mom]
        if len(pool) < 6:
            continue
        if variant == "A":
            score = {s: mom[s] for s in pool}
        else:
            mz = dict(zip(pool, zscore([mom[s] for s in pool])))
            az = dict(zip(pool, zscore([ann[s] for s in pool])))
            score = {s: mz[s] - az[s] for s in pool}
        ranked = sorted(pool, key=lambda x: score[x], reverse=True)
        longs, shorts = ranked[:3], ranked[-3:]
        w = {}
        for s in longs:
            w[s] = 1.0 / 6.0
        for s in shorts:
            w[s] = -1.0 / 6.0
        plans.append({"ts": ts, "w": w, "pool": pool, "longs": longs, "shorts": shorts,
                      "scores": {s: round(score[s], 4) for s in pool}})
    return plans


def build_cache(plans, bars_all, fund_all, spec):
    """每段、每天、每币的 (价格收益 - funding)，权重之外的部分全部预计算。"""
    cache = []
    for p in plans:
        D = p["ts"]
        days = []
        for k in range(7):
            t = D + k * DAY_MS
            row = {}
            for s in p["pool"]:
                b = bars_all[s]
                if k < 6:
                    bt = b.get(t)
                    if bt is None:
                        row[s] = 0.0
                        continue
                    if k == 0:
                        pr = bt["c"] / bt["o"] - 1.0
                    else:
                        bp = b.get(t - DAY_MS)
                        pr = (bt["c"] / bp["c"] - 1.0) if bp else 0.0
                else:
                    # 最后一天：D+6 收盘 → D+7 开盘（换仓价）
                    bt = b.get(t - DAY_MS)
                    bx = b.get(t + DAY_MS)
                    pr = (bx["o"] / bt["c"] - 1.0) if (bt and bx) else 0.0
                fts, frate = fund_all[s]
                lo = bisect.bisect_left(fts, t)
                hi = bisect.bisect_left(fts, t + DAY_MS)
                fsum = sum(frate[lo:hi]) if k < 6 else 0.0
                row[s] = pr - fsum
            days.append(row)
        cache.append(days)
    return cache


def run_weights(weights, cache, cost_bps):
    """weight 序列 -> (每日收益列表, 每段统计, 腿分解, 总换手)"""
    daily, segs, prev = [], [], {}
    legs = {"long": 0.0, "short": 0.0}
    turnover_total = 0.0
    for i, w in enumerate(weights):
        keys = set(w) | set(prev)
        turnover = sum(abs(w.get(s, 0.0) - prev.get(s, 0.0)) for s in keys)
        cost = turnover * (cost_bps / 2.0) / 10_000.0
        turnover_total += turnover
        seg_ret = 0.0
        for k in range(7):
            r = 0.0
            for s, wt in w.items():
                c = wt * cache[i][k].get(s, 0.0)
                r += c
                legs["long" if wt > 0 else "short"] += c
            if k == 0:
                r -= cost
            daily.append(r)
            seg_ret += r
        segs.append({"ret": seg_ret, "turnover": turnover})
        prev = w
    return daily, segs, legs, turnover_total


def sharpe(daily, periods=365):
    if len(daily) < 10:
        return None
    sd = statistics.stdev(daily)
    if sd <= 0:
        return None
    return statistics.mean(daily) / sd * math.sqrt(periods)


def nav_stats(daily):
    nav, peak, mdd = 1.0, 1.0, 0.0
    for r in daily:
        nav *= (1 + r)
        peak = max(peak, nav)
        mdd = max(mdd, (peak - nav) / peak)
    return nav, mdd


def summarize(daily, segs, legs, turnover, years):
    nav, mdd = nav_stats(daily)
    ann = (nav ** (1 / years) - 1) * 100 if nav > 0 and years > 0 else -100.0
    wins = sum(1 for s in segs if s["ret"] > 0)
    sh = sharpe(daily)
    return {
        "days": len(daily),
        "final_nav": round(nav, 4),
        "total_pct": round((nav - 1) * 100, 4),
        "ann_pct": round(ann, 4),
        "sharpe": round(sh, 3) if sh is not None else None,
        "mdd_pct": round(mdd * 100, 4),
        "seg_win_rate": round(wins / len(segs) * 100, 2) if segs else None,
        "segments": len(segs),
        "turnover_sum": round(turnover, 2),
        "long_pnl_pct": round(legs["long"] * 100, 4),
        "short_pnl_pct": round(legs["short"] * 100, 4),
    }


def run(args) -> int:
    spec, lock = load_contract()
    print(f"契约 {lock['contract_sha256'][:16]}…  锁于 {lock['locked_at_utc']}")
    bars_all, fund_all = load_all(spec)
    btc_days = sorted(bars_all["BTCUSDT"])
    print(f"池子 {len(bars_all)} 币，BTC 日历 {al.utc(btc_days[0])[:10]} → "
          f"{al.utc(btc_days[-1])[:10]}（{len(btc_days)} 天）")

    # 固定 3 年窗口，避免运行时刻影响年化
    W0 = int(datetime(2023, 11, 27, tzinfo=timezone.utc).timestamp() * 1000)
    W1 = int(datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp() * 1000)
    OOS0 = int(datetime(2025, 11, 25, tzinfo=timezone.utc).timestamp() * 1000)
    years_all = (W1 - W0) / (365.25 * DAY_MS)
    years_oos = (W1 - OOS0) / (365.25 * DAY_MS)

    results = {}
    for variant in ("A", "B"):
        plans = build_plans(spec, bars_all, fund_all, variant)
        cache = build_cache(plans, bars_all, fund_all, spec)
        print(f"\n基线 {variant}: {len(plans)} 次调仓  "
              f"{al.utc(plans[0]['ts'])[:10]} → {al.utc(plans[-1]['ts'])[:10]}")
        for mult in (1.0, 2.0):
            cost = spec["cost"]["roundtrip_bps"] * mult
            weights = [p["w"] for p in plans]
            daily, segs, legs, turnover = run_weights(weights, cache, cost)
            # 切分：OOS 从 2025-11-25 起（按调仓日对齐）
            oos_idx = next((i for i, p in enumerate(plans) if p["ts"] >= OOS0), len(plans))
            cut = oos_idx * 7
            d_oos = daily[cut:]
            s_is = segs[:oos_idx]
            s_oos = segs[oos_idx:]
            allsum = summarize(daily, segs, legs, turnover, years_all)
            oos_legs = {"long": 0.0, "short": 0.0}
            for i, p in enumerate(plans):
                if p["ts"] < OOS0:
                    continue
                for k in range(7):
                    for s, wt in p["w"].items():
                        oos_legs["long" if wt > 0 else "short"] += wt * cache[i][k].get(s, 0.0)
            oos_sum = summarize(d_oos, s_oos, oos_legs, 0.0, years_oos)
            tag = f"{variant}_{'1x' if mult == 1 else '2x'}"
            results[tag] = {"all": allsum, "oos": oos_sum}
            print(f"  {tag:6s} 全样本 净值 {allsum['final_nav']:.3f} "
                  f"收益 {allsum['total_pct']:+.1f}% 年化 {allsum['ann_pct']:+.2f}% "
                  f"夏普 {allsum['sharpe']} MDD {allsum['mdd_pct']:.1f}%  "
                  f"换手 {allsum['turnover_sum']:.0f}")
            print(f"         样本外 收益 {oos_sum['total_pct']:+.2f}% "
                  f"年化 {oos_sum['ann_pct']:+.2f}% 夏普 {oos_sum['sharpe']}  "
                  f"多头 {allsum['long_pnl_pct']:+.1f}% 空头 {allsum['short_pnl_pct']:+.1f}%")

    # 随机对照（用 1x 成本）
    rng = __import__("random").Random(20261125)
    rand = {}
    if args.mc > 0:
        print(f"\n随机对照 {args.mc} 次（同池同日、随机 3 多 3 空，1x 成本）")
    for variant in ("A", "B"):
        if args.mc <= 0:
            rand[variant] = {"iterations": 0, "real_sharpe": results[f"{variant}_1x"]["all"]["sharpe"],
                             "random_mean": None, "random_p95": None, "p_value": None,
                             "real_percentile": None}
            continue
        plans = build_plans(spec, bars_all, fund_all, variant)
        cache = build_cache(plans, bars_all, fund_all, spec)
        real_sh = results[f"{variant}_1x"]["all"]["sharpe"]
        sims = []
        for _ in range(args.mc):
            ws = []
            for p in plans:
                picks = rng.sample(p["pool"], 6)
                w = {}
                for s in picks[:3]:
                    w[s] = 1 / 6
                for s in picks[3:]:
                    w[s] = -1 / 6
                ws.append(w)
            d, _, _, _ = run_weights(ws, cache, spec["cost"]["roundtrip_bps"])
            sims.append(sharpe(d))
        sims = [x for x in sims if x is not None]
        sims.sort()
        better = sum(1 for x in sims if x >= (real_sh or 0))
        p95 = sims[int(len(sims) * 0.95)]
        rand[variant] = {
            "iterations": len(sims), "real_sharpe": real_sh,
            "random_mean": round(statistics.mean(sims), 3),
            "random_p95": round(p95, 3),
            "p_value": round(better / len(sims), 4),
            "real_percentile": round((1 - better / len(sims)) * 100, 2),
        }
        print(f"  {variant}: 真实夏普 {real_sh}  随机均值 {rand[variant]['random_mean']} "
              f"p95 {p95}  p={rand[variant]['p_value']}  分位 {rand[variant]['real_percentile']}%")

    # 门槛
    gates = {}
    for variant in ("A", "B"):
        o1 = results[f"{variant}_1x"]["oos"]
        o2 = results[f"{variant}_2x"]["oos"]
        a1 = results[f"{variant}_1x"]["all"]
        gates[variant] = {
            "P1_oos_sharpe_gt_0.8_1x": (o1["sharpe"] or -9) > 0.8,
            "P2_oos_sharpe_gt_0.8_2x": (o2["sharpe"] or -9) > 0.8,
            "P3_oos_return_gt_0_both": o1["total_pct"] > 0 and o2["total_pct"] > 0,
            "P4_beats_random_p95": (
                a1["sharpe"] is not None
                and rand[variant]["random_p95"] is not None
                and a1["sharpe"] > rand[variant]["random_p95"]
            ),
            "P5_rebalances_ge_30": a1["segments"] >= 30,
            "P6_both_legs_positive": a1["long_pnl_pct"] > 0 and a1["short_pnl_pct"] > 0,
        }
        print(f"\n基线 {variant} 门槛")
        for k, v in gates[variant].items():
            print(f"  {'PASS' if v else 'FAIL'}  {k}")

    verdict = {}
    for variant in ("A", "B"):
        if results[f"{variant}_1x"]["all"]["segments"] < 30:
            verdict[variant] = "INCONCLUSIVE"
        elif all(gates[variant].values()):
            verdict[variant] = "NOT_FALSIFIED"
        else:
            verdict[variant] = "FALSIFIED"
    print(f"\n判定：A={verdict['A']}  B={verdict['B']}")

    payload = {
        "phase": "Phase 2 cross-section momentum",
        "generated_at_utc": al.utc(int(time.time() * 1000)),
        "contract_sha256": lock["contract_sha256"],
        "spec": spec,
        "window": {"start": al.utc(W0), "end": al.utc(W1), "oos_start": al.utc(OOS0)},
        "results": results,
        "random_control": rand,
        "gates": gates,
        "verdict": verdict,
    }
    out = REPORT_DIR / f"phase2_{int(time.time())}.json"
    al.atomic_write(out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"结果已写入 {out}")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="Phase 2: Top-12 动态池截面动量")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("pull")
    sp.add_argument("--sleep", type=float, default=0.2)
    sp.set_defaults(func=pull)
    sv = sub.add_parser("verify")
    sv.set_defaults(func=verify)
    sr = sub.add_parser("run")
    sr.add_argument("--mc", type=int, default=2000)
    sr.set_defaults(func=run)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
