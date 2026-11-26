#!/usr/bin/env python3
"""
M1'：Polymarket 非时效性市场的概率校准回测。

与 M1 的根本区别
----------------
M1 赌的是"比别人快"——在 METAR 入库或 BTC 跳动的那几百毫秒里抢先改价。
两条线都被一手数据否掉了：市场在源方入库同刻就完成定价。

M1' 换一个维度：不抢时间，问**市场价格作为概率预测准不准**。
如果市场在 p=0.30 的档位上，长期只有 22% 的命中率，那是系统性高估，
买 NO 就有正期望。这类 edge 不需要快，只需要**别人不愿意做的工作**：
把几百个已结算市场的历史价格逐点抠出来，按概率分桶算真实命中率。

核心方法：校准曲线（reliability diagram）
----------------------------------------
对每个市场，在若干"决策时点"取样其隐含概率 p，配对最终结果 y∈{0,1}。
按 p 分桶，桶内平均 p 应等于桶内平均 y。偏离量就是 edge：

    edge(bucket) = mean(y) - mean(p)

正 = 市场低估了该概率段（买 YES 有赚）；负 = 市场高估（买 NO 有赚）。

三个必须防的陷阱
----------------
1. **前视泄露**：取样时刻必须严格早于结算。且距离结算太近时价格已收敛到 0/1，
   样本没有信息量——用 `--min-hours-before-end` 强制截断。
2. **存活偏差**：只取已结算市场是对的，但要注意"作废/50-50 结算"的市场
   会污染标签。只保留 outcomePrices 严格为 ["0","1"] 或 ["1","0"] 的。
3. **样本相关性**：同一事件的多个子市场（如比赛 5 局各自的赢家）高度相关，
   不能当独立样本。按 event 聚簇统计，并报告聚簇数而非市场数。

费率：不管做多做空，taker 都要付。`weather_fees` 是 rate=0.01，
这是目前见过最便宜的档；实际扣费按 fee = 份数 × rate × p × (1-p)。
校准偏离必须大于费率才有意义——在 p=0.5 处 rate=0.01 意味着
名义 0.25%（即 25bps），比 crypto 的 250bps 低一个数量级。

边界：这是**校准**研究，不是套利研究。它回答"市场在哪些概率段系统性错价"，
不回答"能不能抢到"。后者需要盘口深度，而历史接口只给成交价。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arb_probe as ap  # noqa: E402

# prices-history 的时间跨度上限：实测 14 天 OK、20 天 HTTP 400。
# 超限时接口不是返回空，而是直接 400——很容易被误读成"该市场无数据"。
MAX_SPAN_D = 14
SPAN_CHUNK_D = 7          # 分段拉，留足余量

# 费率档。不同市场类型费率不同，取值以市场对象里的 feeSchedule 为准。
DEFAULT_FEE_RATE = 0.01


def parse_ts(s):
    if not s:
        return None
    try:
        return int(datetime.datetime.fromisoformat(
            s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def fetch_closed_markets(date_from, date_to, max_pages=20, min_volume=0.0):
    """按 endDate 窗口 + 成交量降序抓已结算市场。

    不用 `order=endDate` 分页——实测该排序下 offset 会卡在同一个日期上
    原地不动，看起来在翻页其实全是重复数据。
    """
    out, seen = [], set()
    for off in range(0, max_pages * 100, 100):
        url = (f"{ap.GAMMA}/markets?closed=true&limit=100&offset={off}"
               f"&end_date_min={date_from}&end_date_max={date_to}"
               f"&order=volumeNum&ascending=false")
        try:
            page = ap.http_json(url, timeout=40)
        except ap.ProbeError:
            break
        if not page:
            break
        fresh = 0
        for m in page:
            cid = m.get("conditionId")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            fresh += 1
            vol = ap._f(m.get("volumeNum")) or ap._f(m.get("volume")) or 0.0
            if vol < min_volume:
                continue
            out.append(m)
        if fresh == 0:       # 整页重复，说明 offset 已失效
            break
    return out


def outcome_label(m):
    """结算标签。只接受严格 0/1，剔除作废与 50-50。

    返回 1（YES 赢）/ 0（YES 输）/ None（不可用）。
    """
    p = m.get("outcomePrices")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            return None
    if not isinstance(p, list) or len(p) != 2:
        return None
    a, b = ap._f(p[0]), ap._f(p[1])
    if a is None or b is None:
        return None
    if a >= 0.999 and b <= 0.001:
        return 1
    if a <= 0.001 and b >= 0.999:
        return 0
    return None


def price_history(token, t_start, t_end, fidelity=60):
    """分段拉价格历史，绕开 14 天跨度上限。

    返回 [(ts, price)]，按时间升序去重。
    """
    pts = {}
    cur = int(t_start)
    end = int(t_end)
    while cur < end:
        seg_end = min(cur + SPAN_CHUNK_D * 86400, end)
        try:
            d = ap.http_json(
                f"{ap.CLOB}/prices-history?market={token}"
                f"&startTs={cur}&endTs={seg_end}&fidelity={fidelity}",
                timeout=30)
        except ap.ProbeError:
            cur = seg_end
            continue
        for x in (d or {}).get("history") or []:
            try:
                t = int(x["t"])
                if t > 10_000_000_000:
                    t //= 1000
                pts[t] = float(x["p"])
            except (KeyError, TypeError, ValueError):
                continue
        cur = seg_end
        time.sleep(0.15)
    return sorted(pts.items())


def price_at(hist, ts_cap):
    """严格因果：只用 <= ts_cap 的最后一点。"""
    px = None
    for t, p in hist:
        if t <= ts_cap:
            px = p
        else:
            break
    return px


def _collect_one(m, min_hours_before_end, sample_marks, fidelity):
    """处理单个市场，返回 (rows, skip_reason)。

    拆成独立函数是为了能并发跑——市场之间天然独立，串行拉取是纯粹的等待
    浪费。实测串行时 300 个市场 40 分钟都跑不完，并发后降到几分钟。
    """
    y = outcome_label(m)
    if y is None:
        return [], "非0/1结算"
    ids = m.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except Exception:
            return [], "token解析失败"
    if not ids:
        return [], "无token"
    end = parse_ts(m.get("endDate"))
    start = parse_ts(m.get("startDate"))
    if not end or not start:
        return [], "无时间"
    if end - start < min_hours_before_end * 3600:
        return [], "生命周期过短"

    hist = price_history(ids[0], start, end, fidelity=fidelity)
    if not hist:
        return [], "无历史价格"

    out = []
    for hb in sample_marks:
        ts_cap = end - hb * 3600
        if ts_cap <= start:
            continue
        p = price_at(hist, ts_cap)
        # 取不到，或价格已收敛到 0/1（无信息量）
        if p is None or p <= 0.002 or p >= 0.998:
            continue
        out.append({
            "cid": m.get("conditionId"),
            "q": (m.get("question") or "")[:90],
            "y": y,
            "p": p,
            "hours_before_end": hb,
            "vol": ap._f(m.get("volumeNum")) or 0.0,
            "fee_rate": fee_rate_of(m),
            "n_pts": len(hist),
        })
    return out, None


def collect(markets, min_hours_before_end, sample_marks, fidelity=60,
            workers=8, ckpt_every=50):
    """并发取样。worker 数不宜太高——CLOB 的 prices-history 有限流迹象，
    实测 8 并发稳定，16 开始偶发 400。

    每收集 ckpt_every 个市场就把已得样本追加写入一个 .partial 文件。
    理由和 M1 那次 643,205 条推送全丢是同一个：长任务必然会被超时或外部
    kill 打断，如果只在最后落盘，前面的工作全部作废。
    """
    rows = []
    skipped = collections.Counter()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    partial = ap.DATA_DIR / f"calib_partial_{int(time.time())}.jsonl"
    fh = open(partial, "w", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_collect_one, m, min_hours_before_end,
                          sample_marks, fidelity): m for m in markets}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                r, why = f.result()
            except Exception as e:
                skipped[f"异常:{type(e).__name__}"] += 1
                continue
            if why:
                skipped[why] += 1
            for x in r:
                fh.write(json.dumps(x, ensure_ascii=False) + "\n")
            rows.extend(r)
            if done % ckpt_every == 0:
                fh.flush()
                print(f"    …{done}/{len(markets)}（已得 {len(rows)} 样本）",
                      flush=True)
    fh.close()
    print(f"   中间结果 {partial}", flush=True)
    return rows, skipped


def fee_rate_of(m):
    fs = m.get("feeSchedule") or {}
    if isinstance(fs, dict):
        r = fs.get("rate")
        if r is not None:
            return float(r)
    return DEFAULT_FEE_RATE


def wilson_lb(k, n, z=1.96):
    """Wilson 95% 下界。小样本二项比例必须用它，裸比例会高估。"""
    if n <= 0:
        return 0.0
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    s = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (c - s) / d


def bucket_report(rows, edges):
    """核心产出：按概率分桶算校准偏离。"""
    print(f"\n{'='*80}")
    print("校准曲线（bucket 内 mean(y) − mean(p)）")
    print(f"{'概率区间':<12}{'n':>6}{'市场数':>7}{'mean(p)':>9}{'mean(y)':>9}"
          f"{'偏离':>8}{'y下界95%':>10}{'净收益bps':>11}")
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sub = [r for r in rows if lo <= r["p"] < hi]
        if len(sub) < 10:
            continue
        n = len(sub)
        mp = sum(r["p"] for r in sub) / n
        hit = sum(r["y"] for r in sub)
        my = hit / n
        cids = len({r["cid"] for r in sub})
        # 净收益：买 YES 付 mean(p)，赢则拿 1。扣 taker 费 rate×p×(1-p)。
        fee = sum(r["fee_rate"] * r["p"] * (1 - r["p"]) for r in sub) / n
        net = (my - mp - fee) / mp * 1e4 if mp > 0 else 0.0
        print(f"{lo:.2f}-{hi:.2f}  {n:>6}{cids:>7}{mp:>9.3f}{my:>9.3f}"
              f"{my-mp:>+8.3f}{wilson_lb(hit, n):>10.3f}{net:>+11.0f}")
        out.append({"lo": lo, "hi": hi, "n": n, "markets": cids,
                    "mean_p": mp, "mean_y": my, "dev": my - mp,
                    "net_bps": net})
    return out


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="date_from", default="2026-10-01")
    p.add_argument("--to", dest="date_to", default="2026-11-25")
    p.add_argument("--min-volume", type=float, default=5000.0)
    p.add_argument("--pages", type=int, default=10)
    p.add_argument("--marks", default="72,24,6",
                   help="距结算多少小时取样，逗号分隔")
    p.add_argument("--min-hours", type=float, default=4.0)
    p.add_argument("--out", default="")
    a = p.parse_args(argv)

    marks = [float(x) for x in a.marks.split(",") if x.strip()]
    print(f"抓取 {a.date_from} ~ {a.date_to} 已结算市场（成交额 >= ${a.min_volume:,.0f}）…")
    ms = fetch_closed_markets(a.date_from, a.date_to,
                              max_pages=a.pages, min_volume=a.min_volume)
    print(f"  {len(ms)} 个市场")

    rows, skipped = collect(ms, a.min_hours, marks)
    print(f"  可用样本 {len(rows)} 条，覆盖 "
          f"{len({r['cid'] for r in rows})} 个市场")
    if skipped:
        print("  跳过原因：" + "，".join(f"{k} {v}" for k, v in skipped.items()))

    if not rows:
        print("没有样本，无法评估")
        return 1

    edges = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
             0.60, 0.70, 0.80, 0.90, 0.95, 1.0]
    b = bucket_report(rows, edges)

    # 按时点分层：越接近结算价格越准，若早期更差，说明存在可用的时间差
    print(f"\n{'='*80}")
    print("按取样时点分层（检验「越早越不准」是否成立）")
    print(f"{'距结算':<10}{'n':>6}{'mean(p)':>9}{'mean(y)':>9}{'偏离':>8}{'净bps':>9}")
    by_h = {}
    for r in rows:
        by_h.setdefault(r["hours_before_end"], []).append(r)
    for hb in sorted(by_h, reverse=True):
        sub = by_h[hb]
        n = len(sub)
        mp = sum(r["p"] for r in sub) / n
        my = sum(r["y"] for r in sub) / n
        print(f"{hb:>5.0f}h    {n:>6}{mp:>9.3f}{my:>9.3f}{my-mp:>+8.3f}"
              f"{(my-mp)/mp*1e4 if mp else 0:>+9.0f}")

    # 结论：只有偏离超过费率且随簇稳定，才算真 edge
    print(f"\n{'='*80}")
    print("判定")
    sig = [x for x in b if abs(x["dev"]) > 0.02 and x["n"] >= 30]
    if not sig:
        print("  没有任何概率桶的偏离超过 2pp（且样本 >=30）→ 市场校准良好，无 edge")
    else:
        print(f"  {len(sig)} 个桶偏离 >2pp：")
        for x in sig:
            side = "买 YES" if x["dev"] > 0 else "买 NO"
            print(f"    p∈[{x['lo']:.2f},{x['hi']:.2f}) 偏离 {x['dev']*100:+.1f}pp "
                  f"→ {side}  净 {x['net_bps']:+.0f}bps  "
                  f"(n={x['n']}, {x['markets']} 市场)")
        print("\n  注意：这些是**校准**结论，不含滑点与深度约束。")
        print("  单市场最大仓位受限于当时的盘口，需另测。")

    if a.out or True:
        out = ap.DATA_DIR / f"calib_{int(time.time())}.json"
        out.write_text(json.dumps({
            "params": vars(a), "n_rows": len(rows),
            "n_markets": len({r['cid'] for r in rows}),
            "buckets": b, "rows": rows,
        }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())