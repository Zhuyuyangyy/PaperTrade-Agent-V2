#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1b：5m 平值市场的 **定价效率** 检验——跳出"毫秒延迟"的错误框架。

为什么必须换问题
----------------
已经实测确认（38 个已结算窗口）：`btc-updown-5m-*` 的结算统计量是

    「窗口内 TWAP 均值  >=  窗口开盘价」 → UP

TWAP 判定命中 35/38 = 92.1%，末价判定只有 33/38 = 86.8%。
（差额全部来自"末价回到开盘附近但中途大幅偏离"的窗口。）

这条事实推翻了毫秒延迟实验的前提：
现货一次 `5bps` 的瞬时跳动，对 5 分钟均值的贡献是
`5bps × (1s/300s) ≈ 0.017bps`，对平值市场公允概率的影响远小于 1 tick（100bps 名义）。
**盘口不动是数学上正确的，不是反应慢。**
所以 `flat5m_probe.py` 量到的"响应率低"是设计缺陷，不能当作证伪证据。

该问的问题
----------
剩 `k` 秒时，结算结果已经由「已实现部分 + 未实现部分的期望」大致决定：

    公允概率 P(UP) = P( TWAP_已实现 × w + TWAP_剩余 × (1-w)  >=  open )
                   ≈ Φ( (TWAP_已实现 - open) / (σ × sqrt((1-w)·T_remain)) )

其中 `w = 已过时间 / 300`。这个模型的**收敛性**是关键：
窗口越靠后，`T_remain` 越小，`P` 越迅速趋近 0 或 1。

于是两个可测的 edge 候选：
1. **盘口是否跟随这个收敛**——若窗口最后 60s 盘口还停在 0.55，而模型给 0.95，
   那就是可吃的偏差（且不需要低延迟，只需要算得准）。
2. **盘口是否过度反应瞬时脉冲**——若瞬时 5bps 让盘口跳 5 ticks 而 TWAP 只该动 0.02bps，
   那是反向利用"过度反应"的机会（做市逻辑，但我们要的是 taker 可验证的偏差）。

本脚本做第 1 项：逐分钟采集「模型公允概率 vs 实际盘口 vs 最终结算」，
用 **Brier 分数**与**分桶校准**判断盘口是偏保守还是偏激进，
并直接算「按盘口买入、模型说该买」的净收益（扣 crypto_btc_5m_15m_fees 的 0.05）。

用法
----
    python research/polymarket/twap_efficiency.py --windows 40
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arb_probe as ap  # noqa: E402

WINDOW_S = 300
FEE_RATE = 0.05        # crypto_btc_5m_15m_fees 的 taker 档
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = {"User-Agent": "PaperTrade-TwapEff/0.1 (research; read-only)"}


def http_json(url: str, timeout: float = 25.0, retries: int = 4):
    last = None
    for i in range(retries):
        try:
            with _OPENER.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as exc:                        # noqa: BLE001
            last = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last                                          # type: ignore[misc]


# ---------------------------------------------------------------- 数据

def klines_1m(start_ts: int, end_ts: int):
    url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
           f"&startTime={start_ts * 1000}&endTime={end_ts * 1000}&limit=20")
    return http_json(url)


def window_bars(ts: int):
    """窗口内的分钟线，只保留起点落在 [ts, ts+300) 的。"""
    kl = klines_1m(ts, ts + WINDOW_S)
    out = []
    for k in kl:
        t = int(k[0]) // 1000
        if ts <= t < ts + WINDOW_S:
            out.append({"t": t, "o": float(k[1]), "h": float(k[2]),
                        "l": float(k[3]), "c": float(k[4])})
    return out


def price_history(token: str, start_ts: int, end_ts: int):
    """CLOB 价格历史。返回 [(ts_s, price)]，按时间升序。

    注意：本接口的 timestamp 单位是**秒**（踩过坑：当成毫秒会得到 0 条且不报错）。
    """
    url = (f"https://clob.polymarket.com/prices-history?market={token}"
           f"&startTs={start_ts}&endTs={end_ts}&fidelity=1")
    try:
        d = http_json(url, timeout=20)
    except Exception:
        return []
    hist = d.get("history") or []
    out = []
    for h in hist:
        t, p = h.get("t"), h.get("p")
        if t is None or p is None:
            continue
        t = int(t)
        if t > 10_000_000_000:          # 防御：若某天改成毫秒
            t //= 1000
        out.append((t, float(p)))
    out.sort()
    return out


def resolution(ts: int):
    d = ap.http_json(f"{ap.GAMMA}/events?slug=btc-updown-5m-{ts}", timeout=20)
    if not d:
        return None, None
    m = (d[0].get("markets") or [{}])[0]
    p = m.get("outcomePrices")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except json.JSONDecodeError:
            return None, None
    if not m.get("closed") or not p:
        return None, None
    toks = ap._tokens(m)
    return ("up" if float(p[0]) >= 0.5 else "down"), toks


# ---------------------------------------------------------------- 模型

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigma_1m(ts: int) -> float:
    """用该窗口 **之前** 一小时的分钟收益估 σ（bps/分钟）。绝不用窗口内的数据。"""
    try:
        kl = klines_1m(ts - 3600, ts - 1)
    except Exception:
        return 8.0                       # BTC 分钟波动经验值 ~8bps
    rets = []
    for a, b in zip(kl, kl[1:]):
        try:
            c0, c1 = float(a[4]), float(b[4])
            if c0 > 0:
                rets.append((c1 / c0 - 1) * 1e4)
        except (TypeError, ValueError, IndexError):
            continue
    if len(rets) < 10:
        return 8.0
    return statistics.pstdev(rets)


def fair_prob(twap_sofar: float, open_px: float, elapsed_s: float,
              sigma_1m_bps: float, window_s: int = WINDOW_S):
    """在 **只看得到 t=elapsed 时刻信息** 的前提下，估算结算为 UP 的概率。

    结算条件：`(TWAP_sofar·elapsed + TWAP_rest·remain) / window >= open`

    剩余段的期望独立于已实现段（随机游走近似），于是
    `P(UP) = P( TWAP_rest >= (open·window − TWAP_sofar·elapsed) / remain )`

    剩余均值的标准差用 `σ·sqrt(remain/60)/sqrt(3)`：除以 sqrt(3) 是均值型统计量
    对独立增量的方差折扣。这是**保守**估计——真实剩余均值的时间相关性会
    让方差更大，扣掉折扣后模型会更靠近 0.5（更少假 edge），不会更激进。
    """
    remain = max(window_s - elapsed_s, 0.0)
    if remain <= 1.0:
        return 1.0 if twap_sofar >= open_px else 0.0
    need = (open_px * window_s - twap_sofar * elapsed_s) / remain
    sd = sigma_1m_bps / 1e4 * open_px * math.sqrt(remain / 60.0) / math.sqrt(3.0)
    if sd <= 1e-12:
        return 1.0 if need <= open_px else 0.0
    return norm_cdf((open_px - need) / sd)


# ---------------------------------------------------------------- 主流程

def run(windows: int, out_path: Path):
    now = int(time.time())
    base = now - now % WINDOW_S
    # 观测点记录（严格因果对齐）
    obs = []          # 每行：一个 (窗口, 采样时刻) 的观测
    meta = []
    skipped_no_quote = 0
    skipped_mismatch = 0

    print(f"扫描最近 {windows} 个已结算窗口…", flush=True)
    for k in range(1, windows + 1):
        ts = base - k * WINDOW_S
        res, toks = resolution(ts)
        if not res or not toks:
            continue
        bars = window_bars(ts)
        if len(bars) < 4:
            continue
        twap_all = statistics.mean(b["c"] for b in bars)
        open_px = bars[0]["o"]
        sig = sigma_1m(ts)
        meta.append({"ts": ts, "res": res, "open": open_px,
                     "twap": twap_all, "last": bars[-1]["c"],
                     "sigma_bps": round(sig, 2), "n_bars": len(bars)})
        tok = toks[0]                        # YES = UP
        hist = price_history(tok, ts, ts + WINDOW_S)
        if not hist:
            continue

        # 只用一个"已收完的分钟线"状态做判断：第 i 根线收完 = ts+(i+1)*60。
        # 这样才能保证「模型看到的」与「盘口给价的」是同一时刻。
        cum, n = 0.0, 0
        for i, b in enumerate(bars):
            cum += b["c"]
            n += 1
            t_state = ts + n * 60            # 该分钟线收盘时刻
            elapsed = n * 60
            if elapsed > WINDOW_S:
                break
            twap_sofar = cum / n
            p_fair = fair_prob(twap_sofar, open_px, elapsed, sig)
            # 盘口价：必须 **<= t_state** 且尽量新。
            # 向后取最近一条（严格因果），并限制最大陈旧度，避免拿 45s 前的价
            # 和刚收完的 K 线比——那会凭空造出 edge（第一版就栽在这里）。
            px, px_t = None, None
            for (ht, hp) in hist:
                if ht <= t_state:
                    px, px_t = hp, ht
                else:
                    break
            if px is None:
                skipped_no_quote += 1
                continue
            staleness = t_state - px_t
            if staleness > 90:               # 太旧就不算，宁可少样本
                skipped_mismatch += 1
                continue
            if px <= 0.002 or px >= 0.998:    # 已定价，无信息
                continue
            obs.append({"ts": ts, "res": res, "elapsed": elapsed,
                        "remain": WINDOW_S - elapsed, "px": px,
                        "px_staleness_s": staleness,
                        "p_fair": p_fair, "twap_sofar": twap_sofar,
                        "open": open_px, "sigma_bps": sig})
        print(f"  {ts} {res:4s} TWAP {twap_all:.1f} open {open_px:.1f} "
              f"σ {sig:.1f}bps 盘口点 {len(hist)}", flush=True)
        time.sleep(0.3)

    if not obs:
        print("\n没有可用观测——prices-history 对 5m token 可能不返回数据")
        return 1
    print(f"\n因果对齐：可用 {len(obs)} 个观测；"
          f"无更早报价 {skipped_no_quote}；陈旧 >90s {skipped_mismatch}")
    print(f"  盘口价陈旧度中位 {statistics.median(o['px_staleness_s'] for o in obs):.0f}s"
          f"（这是模型与盘口信息量的真实差距，必须报出来）")

    # ---- 报告 1：结算统计量确认 ----
    print(f"\n{'='*76}\n结算统计量确认（n={len(meta)} 窗口）")
    tw = sum(1 for m in meta if ("up" if m["twap"] >= m["open"] else "down") == m["res"])
    lt = sum(1 for m in meta if ("up" if m["last"] >= m["open"] else "down") == m["res"])
    print(f"  TWAP 均值判定命中 {tw}/{len(meta)} = {tw/len(meta)*100:.1f}%")
    print(f"  末价判定命中     {lt}/{len(meta)} = {lt/len(meta)*100:.1f}%")

    # ---- 报告 2：按剩余时间分桶，比较盘口 vs 模型 ----
    print(f"\n{'='*76}\n盘口 vs TWAP 模型（按剩余时间分桶，n={len(obs)} 观测）")
    print(f"  {'剩余':<10}{'n':>5}{'盘口均值':>9}{'模型均值':>9}"
          f"{'实际UP%':>9}{'盘口Brier':>10}{'模型Brier':>10}  谁更准")
    buckets = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 300)]
    for lo, hi in buckets:
        sub = [o for o in obs if lo <= o["remain"] < hi]
        if len(sub) < 5:
            continue
        up = sum(1 for o in sub if o["res"] == "up") / len(sub)
        px = statistics.mean(o["px"] for o in sub)
        pf = statistics.mean(o["p_fair"] for o in sub)
        bs_px = statistics.mean((o["px"] - (1 if o["res"] == "up" else 0)) ** 2 for o in sub)
        bs_pf = statistics.mean((o["p_fair"] - (1 if o["res"] == "up" else 0)) ** 2 for o in sub)
        who = "模型" if bs_pf < bs_px - 1e-6 else ("盘口" if bs_px < bs_pf - 1e-6 else "持平")
        print(f"  {lo}-{hi}s{'':<4}{len(sub):>5}{px:>9.3f}{pf:>9.3f}{up*100:>8.0f}%"
              f"{bs_px:>10.4f}{bs_pf:>10.4f}  {who}")

    # ---- 报告 3：可交易偏差 ----
    print(f"\n{'='*76}\n可交易偏差（按模型买 YES 或 NO，扣 fee 0.05×p×(1-p)）")
    trades = []
    for o in obs:
        side, price, p = ("up", o["px"], o["p_fair"]) if o["p_fair"] > o["px"] else \
                         ("down", 1 - o["px"], 1 - o["p_fair"])
        edge = p - price
        if edge <= 0.02:                        # 2pp 以下视作噪声
            continue
        fee = FEE_RATE * price * (1 - price)
        won = (o["res"] == side)
        pnl = (1.0 if won else 0.0) - price - fee
        trades.append({**o, "side": side, "price": price, "model_p": p,
                       "edge": edge, "fee": fee, "won": won, "pnl": pnl})
    if not trades:
        print("  无（模型与盘口的差距都在 2pp 以内）")
    else:
        n = len(trades)
        wr = sum(1 for t in trades if t["won"]) / n
        cost = sum(t["price"] for t in trades)
        pnl = sum(t["pnl"] for t in trades)
        print(f"  候选交易 {n} 笔  胜率 {wr*100:.1f}%  总投入 ${cost:.2f}  "
              f"总净 ${pnl:+.2f}  净收益率 {pnl/cost*1e4:+.0f}bps")
        print(f"  平均 edge {statistics.mean(t['edge'] for t in trades)*100:.1f}pp  "
              f"平均费 {statistics.mean(t['fee'] for t in trades)*1e4:.0f}bps  "
              f"平均盘口陈旧 {statistics.mean(t['px_staleness_s'] for t in trades):.0f}s")
        # 按窗口聚簇 bootstrap：同一窗口的多笔高度相关，逐笔独立会窄得离谱
        by_win = {}
        for t in trades:
            by_win.setdefault(t["ts"], []).append(t)
        rets_by_win = [(sum(x["pnl"] for x in v) / sum(x["price"] for x in v) * 1e4)
                       for v in by_win.values()]
        if len(rets_by_win) >= 5:
            rng = __import__("random").Random(20261125)
            m = len(rets_by_win)
            means = sorted(
                sum(rets_by_win[rng.randrange(m)] for _ in range(m)) / m
                for _ in range(4000))
            print(f"  按窗口聚簇 bootstrap（{m} 个窗口）净收益率 95%CI "
                  f"[{means[100]:+.0f}, {means[3900]:+.0f}]bps")
        # 分剩余时间看
        for lo, hi in buckets:
            sub = [t for t in trades if lo <= t["remain"] < hi]
            if not sub:
                continue
            w2 = sum(1 for t in sub if t["won"]) / len(sub)
            c2 = sum(t["price"] for t in sub)
            p2 = sum(t["pnl"] for t in sub)
            print(f"    剩 {lo}-{hi}s: n={len(sub):2d} 胜率 {w2*100:5.1f}%  "
                  f"净 {p2/c2*1e4:+7.0f}bps")

    # ---- 报告 4：陈旧度敏感性（最容易被误当 edge 的地方）----
    print(f"\n{'='*76}\n盘口陈旧度敏感性（edge 若只存在于旧报价上，就是假的）")
    print(f"  {'陈旧上限':<10}{'n':>5}{'胜率':>8}{'净bps':>9}")
    for cap in (15, 30, 60, 90):
        sub = [t for t in trades if t["px_staleness_s"] <= cap]
        if len(sub) < 3:
            continue
        w2 = sum(1 for t in sub if t["won"]) / len(sub)
        c2 = sum(t["price"] for t in sub)
        p2 = sum(t["pnl"] for t in sub)
        print(f"  <={cap}s{'':<6}{len(sub):>5}{w2*100:>7.1f}%{p2/c2*1e4:>+9.0f}")

    out_path.write_text(json.dumps({
        "generated_utc": int(time.time() * 1000),
        "windows": meta, "obs": obs,
        "trades": [{k: v for k, v in t.items() if k != "res"} for t in trades],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n写入 {out_path}")
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="5m 平值市场 TWAP 定价效率检验")
    p.add_argument("--windows", type=int, default=40)
    args = p.parse_args(argv)
    out = ap.DATA_DIR / f"twap_eff_{int(time.time())}.json"
    return run(args.windows, out)


if __name__ == "__main__":
    sys.exit(main())