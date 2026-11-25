#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Polymarket Phase 0：纯只读盘口套利探测。

零资金、零下单、零密钥。只做一件事：把真实订单簿拉下来，用逐档吃单模拟
回答"个人到底能不能吃到"，而不是"中间价看起来有没有"。

两类机会
--------
1. NegRisk 完备集：一个互斥事件的所有市场都买 YES，若 Σ ask < 1，
   理论上可合成 $1。**必须先过完备性校验**——只列了部分结果的"组冠军"
   市场 Σ ask 可能只有 0.12，那不是套利，是结果集缺了一半。
2. 二元 YES+NO：同一市场买 YES 和 NO 各 n 份，Σ ask < 1 即可合成 $1。

三条防自欺红线（写进代码结构，不靠自觉）
------------------------------------------
- 只用真实 asks 逐档吃单；mid / last 只用于**预筛和完备性校验**，绝不进套利计算
- 每个机会记录存活区间：首见 → 末见，按扫描轮次计算，区分 <1s / 1-3s / >=3s
- 费率按官方公式 fee = C × feeRate × p × (1-p) 逐档累加，
  feeRate 未知时同时给 0 / 0.01 / 0.05 三档，不猜单一值

用法
----
    python research/polymarket/arb_probe.py snapshot
    python research/polymarket/arb_probe.py probe --minutes 10
    python research/polymarket/arb_probe.py probe --minutes 10 --max-tokens 4000 --threads 8
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "PaperTrade-PolyProbe/0.1 (research; read-only)",
      "Accept": "application/json", "Accept-Encoding": "gzip"}
# 直连：本机代理会把 2s 拖成 9s（实测），且不需要代理也能通
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CAPITALS = (100.0, 500.0, 1000.0)
FEE_RATES = (0.0, 0.01, 0.05)     # 官方文档的 0 / 经济类 / 体育·政治选举类
COMPLETE_MID_LO, COMPLETE_MID_HI = 0.97, 1.03


class ProbeError(RuntimeError):
    pass


# ---------------------------------------------------------------- HTTP

def _read(resp) -> bytes:
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw


def http_json(url: str, *, timeout: float = 30.0, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            with _OPENER.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(_read(r))
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ProbeError(f"HTTP {exc.code} {url[:90]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < retries:
                time.sleep(1.0 + attempt)
                continue
            raise ProbeError(f"{type(exc).__name__} {url[:90]}") from exc
    raise ProbeError("重试后仍失败")


def http_post(url: str, payload, *, timeout: float = 40.0, retries: int = 2):
    body = json.dumps(payload).encode()
    headers = {**UA, "Content-Type": "application/json"}
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(_read(r))
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ProbeError(f"HTTP {exc.code} POST {url[:60]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < retries:
                time.sleep(1.0 + attempt)
                continue
            raise ProbeError(f"{type(exc).__name__} POST") from exc
    raise ProbeError("重试后仍失败")


# ---------------------------------------------------------------- 发现

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _tokens(m: dict) -> list[str]:
    raw = m.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            return [str(t) for t in json.loads(raw)]
        except json.JSONDecodeError:
            return []
    return [str(t) for t in (raw or [])]


def _tradable(m: dict) -> bool:
    return bool(m.get("enableOrderBook")) and bool(m.get("acceptingOrders")) and not m.get("closed")


def fetch_events(offset: int, limit: int = 100):
    url = (f"{GAMMA}/events?limit={limit}&offset={offset}&closed=false&active=true"
           f"&order=volume24hr&ascending=false")
    return http_json(url, timeout=40)


def discover(max_events: int = 1200, threads: int = 6) -> dict:
    """拉全量活跃事件，切成 negRisk 完备集候选 + 二元候选。"""
    t0 = time.time()
    offsets = list(range(0, max_events, 100))

    def page(off):
        try:
            return off, fetch_events(off)
        except ProbeError:
            return off, []

    rows = []
    with cf.ThreadPoolExecutor(max_workers=threads) as ex:
        for _off, data in ex.map(page, offsets):
            rows.extend(data or [])

    seen, events = set(), []
    for e in rows:
        if e["id"] not in seen:
            seen.add(e["id"])
            events.append(e)

    neg_events, binaries = [], []
    for e in events:
        markets = [m for m in (e.get("markets") or []) if _tradable(m)]
        if len(markets) < 2:
            continue
        if e.get("negRisk"):
            legs = []
            for m in markets:
                toks = _tokens(m)
                if len(toks) < 2:
                    break
                legs.append({"market_id": m.get("id"), "condition_id": m.get("conditionId"),
                             "question": (m.get("question") or "")[:120],
                             "yes_token": toks[0], "no_token": toks[1],
                             "gamma_best_ask": _f(m.get("bestAsk")),
                             "gamma_best_bid": _f(m.get("bestBid")),
                             "liquidity": _f(m.get("liquidityNum")) or 0.0})
            else:
                if len(legs) >= 2:
                    neg_events.append({"event_id": e["id"], "title": e.get("title") or "",
                                       "neg_risk_augmented": bool(e.get("negRiskAugmented")),
                                       "legs": legs, "kind": "negRisk"})
        else:
            for m in markets:
                toks = _tokens(m)
                if len(toks) != 2:
                    continue
                binaries.append({"condition_id": m.get("conditionId"), "market_id": m.get("id"),
                                 "event_id": e["id"], "question": (m.get("question") or "")[:120],
                                 "yes_token": toks[0], "no_token": toks[1],
                                 "gamma_best_ask": _f(m.get("bestAsk")),
                                 "gamma_best_bid": _f(m.get("bestBid")),
                                 "liquidity": _f(m.get("liquidityNum")) or 0.0,
                                 "end_date": m.get("endDate") or m.get("endDateIso"),
                                 "kind": "binary"})

    meta = {"events_total": len(events), "neg_events": len(neg_events),
            "neg_legs": sum(len(x["legs"]) for x in neg_events),
            "binaries": len(binaries), "discover_s": round(time.time() - t0, 2)}
    return {"neg": neg_events, "bin": binaries, "meta": meta}


def prefilter(universe: dict, neg_max: float = 1.02, bin_max: float = 1.005,
              min_liq: float = 5000.0) -> dict:
    """用 Gamma 的 bestAsk/bestBid 粗筛，只决定"先看谁"，不参与套利计算。"""
    neg_hot, neg_inc = [], []
    for ev in universe["neg"]:
        asks = [l["gamma_best_ask"] for l in ev["legs"]]
        mids = []
        for l in ev["legs"]:
            a, b = l["gamma_best_ask"], l["gamma_best_bid"]
            mids.append((a + b) / 2 if (a is not None and b is not None) else None)
        if any(a is None for a in asks) or any(m is None for m in mids):
            neg_inc.append(ev)
            continue
        s_ask, s_mid = sum(asks), sum(mids)
        if s_ask <= neg_max:
            neg_hot.append({**ev, "gamma_sum_ask": round(s_ask, 6),
                            "gamma_sum_mid": round(s_mid, 6),
                            "complete_hint": COMPLETE_MID_LO <= s_mid <= COMPLETE_MID_HI})
    neg_hot.sort(key=lambda x: x["gamma_sum_ask"])

    bin_hot = []
    for m in universe["bin"]:
        a, b = m["gamma_best_ask"], m["gamma_best_bid"]
        if a is None or b is None:
            continue
        if m["liquidity"] < min_liq:
            continue
        approx = a + (1.0 - b)
        if approx <= bin_max:
            bin_hot.append({**m, "gamma_approx_sum": round(approx, 6)})
    bin_hot.sort(key=lambda x: x["gamma_approx_sum"])
    return {"neg_hot": neg_hot, "neg_incomplete_hint": neg_inc, "bin_hot": bin_hot}


# ---------------------------------------------------------------- 盘口

def fetch_books(tokens: list[str], threads: int = 8, batch: int = 200) -> dict:
    """批量 POST /books（单本 5.6ms，比逐个 GET 快 300 倍）。"""
    chunks = [tokens[i:i + batch] for i in range(0, len(tokens), batch)]
    out: dict[str, dict] = {}

    def one(chunk):
        try:
            res = http_post(f"{CLOB}/books", [{"token_id": t} for t in chunk])
        except ProbeError:
            return []
        return res if isinstance(res, list) else []

    with cf.ThreadPoolExecutor(max_workers=threads) as ex:
        for books in ex.map(one, chunks):
            for bk in books:
                tid = str(bk.get("asset_id") or "")
                if tid:
                    out[tid] = bk
    return out


def sides(book: dict):
    """返回 (bids, asks)，均为 [(price, size)] 且已按最优在前排序。

    CLOB 的 bids 实测是**升序**排列的（bids[0] 是最差买价），asks 同理，
    绝不能直接取 levels[0]。
    """
    def conv(levels):
        rows = []
        for lv in levels or []:
            p, s = _f(lv.get("price")), _f(lv.get("size"))
            if p is None or s is None or s <= 0:
                continue
            rows.append((p, s))
        return rows

    bids = sorted(conv(book.get("bids")), key=lambda x: -x[0])
    asks = sorted(conv(book.get("asks")), key=lambda x: x[0])
    return bids, asks


# ---------------------------------------------------------------- 吃单模拟

def walk_buy(levels: list[tuple[float, float]], shares: float,
             fee_rate: float) -> tuple[float, float, float]:
    """逐档买入 shares 份。返回 (成本 USD, 手续费 USD, 实际成交份数)。

    手续费按官方公式逐档累加：fee = 份数 × feeRate × p × (1-p)
    """
    remaining, cost, fee = shares, 0.0, 0.0
    for p, s in levels:
        take = min(remaining, s)
        cost += take * p
        fee += take * fee_rate * p * (1.0 - p)
        remaining -= take
        if remaining <= 1e-12:
            break
    return cost, fee, shares - max(0.0, remaining)


def solve_shares(legs: list[list[tuple[float, float]]], capital: float,
                 fee_rate: float) -> dict:
    """同一份数买齐所有腿。求「花掉约 capital」的份数，并给出深度上限。"""
    depth = min(sum(s for _p, s in leg) for leg in legs)
    if depth <= 0:
        return {"shares": 0.0, "cost": 0.0, "fee": 0.0, "profit": 0.0,
                "max_deployable": 0.0, "depth_limited": True}

    def at(n: float):
        cost = fee = filled = 0.0
        filled = n
        for leg in legs:
            c, f, got = walk_buy(leg, n, fee_rate)
            cost += c
            fee += f
            filled = min(filled, got)
        return cost, fee, filled

    lo, hi = 0.0, depth
    for _ in range(36):
        mid = (lo + hi) / 2
        c, _f_, _fl = at(mid)
        if c <= capital:
            lo = mid
        else:
            hi = mid
    n = lo
    cost, fee, filled = at(n)
    n = filled
    return {"shares": round(n, 4), "cost": round(cost, 4), "fee": round(fee, 4),
            "profit": round(n - cost - fee, 4),
            "max_deployable": round(at(depth)[0], 2),
            "depth_limited": n >= depth - 1e-9}


def best_ask(levels) -> float | None:
    return levels[0][0] if levels else None


def best_bid(levels) -> float | None:
    return levels[0][0] if levels else None


# ---------------------------------------------------------------- 评估

def eval_neg(event: dict, books: dict, deep: bool = True) -> dict | None:
    """NegRisk 完备集：买齐所有腿的 YES。deep=False 时只算顶层指标，不做深度模拟。"""
    asks, mids, missing, legs_used = [], [], [], 0
    for leg in event["legs"]:
        bk = books.get(leg["yes_token"])
        if not bk:
            missing.append(leg["market_id"])
            continue
        bids, a = sides(bk)
        if not a:
            missing.append(leg["market_id"])
            continue
        asks.append(a)
        mids.append((bids[0][0] + a[0][0]) / 2 if bids else None)
        legs_used += 1

    if len(asks) < 2 or missing:
        return None
    if any(m is None for m in mids):
        return None

    sum_ask = sum(a[0][0] for a in asks)
    sum_mid = sum(mids)
    complete = COMPLETE_MID_LO <= sum_mid <= COMPLETE_MID_HI
    row = {"kind": "negRisk", "key": f"neg:{event['event_id']}",
           "title": event["title"][:110], "n_legs": len(asks),
           "sum_ask": round(sum_ask, 6), "sum_mid": round(sum_mid, 6),
           "complete": complete, "gross_edge": round(1.0 - sum_ask, 6),
           "missing_legs": len(missing), "max_deployable": 0.0}
    # 只有"完备且真有价差"才值得算深度；其余算力全部省下
    if deep or (complete and sum_ask < 1.0):
        row["max_deployable"] = round(solve_shares(asks, 1e9, 0.0)["max_deployable"], 2)
        if complete and sum_ask < 1.0:
            for fee_rate in FEE_RATES:
                for cap in CAPITALS:
                    r = solve_shares(asks, cap, fee_rate)
                    row[f"f{fee_rate:.2f}_c{int(cap)}"] = {
                        "net_usd": r["profit"], "net_bps": round(r["profit"] / cap * 1e4, 2),
                        "cost": r["cost"], "fee": r["fee"], "shares": r["shares"]}
    return row


def eval_binary(m: dict, books: dict, deep: bool = True) -> dict | None:
    by = books.get(m["yes_token"])
    bn = books.get(m["no_token"])
    if not by or not bn:
        return None
    bids_y, ay = sides(by)
    bids_n, an = sides(bn)
    if not ay or not an:
        return None
    sum_ask = ay[0][0] + an[0][0]
    sum_mid = None
    if bids_y and bids_n:
        sum_mid = (bids_y[0][0] + ay[0][0]) / 2 + (bids_n[0][0] + an[0][0]) / 2
    legs = [ay, an]
    row = {"kind": "binary", "key": f"bin:{m['condition_id']}",
           "title": m["question"][:110], "n_legs": 2,
           "sum_ask": round(sum_ask, 6),
           "sum_mid": round(sum_mid, 6) if sum_mid is not None else None,
           "complete": True, "gross_edge": round(1.0 - sum_ask, 6),
           "liquidity": m["liquidity"], "max_deployable": 0.0}
    if deep or sum_ask < 1.0:
        row["max_deployable"] = round(solve_shares(legs, 1e9, 0.0)["max_deployable"], 2)
        if sum_ask < 1.0:
            for fee_rate in FEE_RATES:
                for cap in CAPITALS:
                    r = solve_shares(legs, cap, fee_rate)
                    row[f"f{fee_rate:.2f}_c{int(cap)}"] = {
                        "net_usd": r["profit"], "net_bps": round(r["profit"] / cap * 1e4, 2),
                        "cost": r["cost"], "fee": r["fee"], "shares": r["shares"]}
    return row



# ---------------------------------------------------------------- 尾盘折价

def _parse_end(s):
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        txt = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def eval_tail(m: dict, books: dict, now_ms: int, max_hours: float = 48.0,
              lo: float = 0.90, hi: float = 0.995) -> dict | None:
    """临近结算的高概率折价：买 YES 持有到结算，折算年化。

    这不是套利。买 0.96 到期拿 1.00 的期望收益，正好等于市场给失败概率的定价；
    赚的是"它没失败"，亏的是"它失败了"。所以必须把年化和赔付结构一起报。
    """
    end = _parse_end(m.get("end_date"))
    if end is None:
        return None
    hours = (end - now_ms) / 3_600_000.0
    if hours <= 0 or hours > max_hours:
        return None
    bk = books.get(m["yes_token"])
    if not bk:
        return None
    _bids, asks = sides(bk)
    if not asks:
        return None
    ask = asks[0][0]
    if not (lo <= ask <= hi):
        return None

    depth_shares = sum(sz for _p, sz in asks)
    max_usd = sum(p * sz for p, sz in asks)
    # $100 规模的真实均价与手续费
    n = min(100.0 / ask, depth_shares)
    cost, fee, filled = walk_buy(asks, n, 0.05)
    if filled <= 0:
        return None
    avg = cost / filled
    fee_ps = fee / filled
    gross = (1.0 - avg) / avg
    net = (1.0 - avg - fee_ps) / avg
    days = hours / 24.0
    return {"kind": "tail", "key": f"tail:{m['condition_id']}", "title": m["question"][:110],
            "ask": round(ask, 4), "avg_fill": round(avg, 4), "hours_left": round(hours, 2),
            "depth_usd": round(max_usd, 2), "liquidity": m["liquidity"],
            "gross_ret_pct": round(gross * 100, 3), "net_ret_pct": round(net * 100, 3),
            "ann_pct": round(net * 365.0 / days * 100, 1) if days > 0 else None,
            "fee_per_share": round(fee_ps, 5),
            "breakeven_win_rate": round(avg + fee_ps, 4)}


def cmd_tail(args) -> int:
    u, pre = _universe(args)
    now = int(time.time() * 1000)
    cands = []
    for m in u["bin"]:
        end = _parse_end(m.get("end_date"))
        if end is None:
            continue
        hours = (end - now) / 3_600_000.0
        a = m["gamma_best_ask"]
        if 0 < hours <= args.max_hours and a is not None and args.lo <= a <= args.hi:
            cands.append(m)
    cands.sort(key=lambda m: -(1 - m["gamma_best_ask"]) / max(m["gamma_best_ask"], 1e-9))
    print(f"尾盘候选（<={args.max_hours}h，ask {args.lo}-{args.hi}）：{len(cands)}")
    toks = [t for m in cands for t in (m["yes_token"], m["no_token"])]
    books = fetch_books(toks, threads=args.threads)
    rows = [r for m in cands if (r := eval_tail(m, books, now, args.max_hours, args.lo, args.hi))]
    rows.sort(key=lambda r: -(r["ann_pct"] or -1e9))
    print(f"\n{'ask':>6} {'时剩余':>7} {'深度$':>9} {'净收益':>8} {'年化':>10} {'保本胜率':>9}  市场")
    for r in rows[:25]:
        print(f"{r['ask']:6.3f} {r['hours_left']:6.1f}h {r['depth_usd']:9.0f} "
              f"{r['net_ret_pct']:+7.2f}% {r['ann_pct']:+9.1f}% {r['breakeven_win_rate']:9.3f}  "
              f"{r['title'][:42]}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"tail_{int(time.time())}.json"
    out.write_text(json.dumps({"generated_utc": now, "candidates": len(cands),
                               "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0



# ---------------------------------------------------------------- 校准

CAL_WINDOWS = (6, 24, 48)            # 结算前小时数（以 closedTime 为锚）
MAX_SPAN_H = 336.0                   # prices-history 单次查询上限，实测 14 天（16 天返回 400）
BUCKETS_HIGH = ((0.50, 0.80), (0.80, 0.90), (0.90, 0.95),
                (0.95, 0.98), (0.98, 0.99), (0.99, 1.0001))
BUCKETS_LOW = ((0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50))

CATEGORY_RULES = (
    ("sports", ("sports", "esports", "games", "nfl", "nba", "mlb", "nhl", "soccer",
                "football", "tennis", "ufc", "boxing", "olympic", "cricket", "golf",
                "counter strike", "league of legends", "dota", "valorant", "cs2",
                "map handicap", "total rounds", "match winner", "vs.")),
    ("crypto", ("crypto", "crypto prices", "bitcoin", "btc", "ethereum", "eth",
                "solana", "sol", "xrp", "dogecoin", "doge", "binance", "defi",
                "up or down", "token", "altcoin")),
    ("politics", ("politics", "election", "elections", "president", "presidential",
                  "senate", "congress", "governor", "parliament", "trump", "biden",
                  "harris", "vote", "geopolitics", "prime minister", "supreme court")),
)


def fetch_closed_markets(want: int = 300, min_vol: float = 10_000.0,
                         max_pages: int | None = None) -> list[dict]:
    """按成交额降序取已结算的二元市场。

    按 volumeNum 而不是 closedTime 排序：后者会被 5 分钟加密涨跌市场淹没
    （实测最新 6 个全是 5M 市场），「尾盘折价」在那类市场上没有意义。
    代价是偏向高成交的老市场，这一偏差在报告里写明。
    """
    out, seen = [], set()
    pages = max_pages if max_pages is not None else want // 100 + 12
    for page in range(pages):
        try:
            rows = http_json(f"{GAMMA}/markets?limit=100&offset={page*100}"
                             f"&closed=true&order=volumeNum&ascending=false", timeout=40)
        except ProbeError:
            break
        if not rows:
            break
        for m in rows:
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            if len(_tokens(m)) != 2:
                continue
            if (_f(m.get("volumeNum")) or 0) < min_vol:
                continue
            out.append(m)
        if len(out) >= want or len(rows) < 100:
            break
    return out[:want]


def fetch_event_tags(event_ids, threads: int = 12) -> dict:
    """事件标签。market 对象上没有 tags，只有 /events/{id} 有；
    同一个 event 下的多个 market 共享一次请求。"""
    out = {}

    def one(eid):
        try:
            d = http_json(f"{GAMMA}/events/{eid}", timeout=30)
        except ProbeError:
            return eid, []
        return eid, [t.get("label") for t in (d.get("tags") or []) if t.get("label")]

    ids = list(event_ids)
    if not ids:
        return out
    with cf.ThreadPoolExecutor(max_workers=min(threads, len(ids))) as ex:
        for eid, tags in ex.map(one, ids):
            out[eid] = tags
    return out


def classify(tags, question: str = "") -> str:
    """按事件标签定品类，标签缺失时退回问题文本关键词。"""
    low = [str(t).lower() for t in (tags or [])]
    text = " ".join(low) + " " + (question or "").lower()
    best, best_hits = "other", 0
    for name, keys in CATEGORY_RULES:
        hits = sum(1 for k in keys if k in text)
        if name in low:
            hits += 3
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def _norm_ts(t) -> int:
    """prices-history 的 t 是**秒**，统一到毫秒。

    实际踩过：拿毫秒去和秒比较，所有时间窗都判成"无数据"，
    500 个市场返回 0 条观测，且不报错。
    """
    ts = int(t)
    return ts * 1000 if ts < 10 ** 11 else ts


def price_path(token: str, anchor_ms: int, span_h: float = MAX_SPAN_H):
    """[anchor - span_h, anchor] 的逐小时价格，一次请求覆盖三个时间窗。"""
    start = int(anchor_ms / 1000 - span_h * 3600)
    end = int(anchor_ms / 1000)
    try:
        d = http_json(f"{CLOB}/prices-history?market={token}"
                      f"&startTs={start}&endTs={end}&fidelity=60", timeout=30)
    except ProbeError:
        return []
    hist = d.get("history", []) if isinstance(d, dict) else d
    out = []
    for pt in hist:
        p = _f(pt.get("p"))
        if p is not None:
            out.append((_norm_ts(pt["t"]), p))
    out.sort()
    return out


def sample_at(path, target_ms: int, tol_h: float = 3.0):
    """target 时刻之前最近的一个报价。滞后超过 tol_h 视为该窗口无数据，
    避免拿 40 小时前的价格冒充 6 小时前的价格。"""
    best = None
    for ts, p in path:
        if ts <= target_ms and (best is None or ts > best[0]):
            best = (ts, p)
    if best is None:
        return None
    lag_h = (target_ms - best[0]) / 3_600_000.0
    if lag_h > tol_h:
        return None
    return best[1], lag_h


def wilson_lb(k: int, n: int, z: float = 1.96) -> float | None:
    """二项比例的 Wilson 95% 下界。小样本下远比正态近似可靠。"""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(max(p * (1 - p) / n + z * z / (4 * n * n), 0.0))
    return max(0.0, (centre - half) / den)


def net_edge_bps(win: float, price: float, fee_rate: float = 0.0,
                 slip_bps: float = 0.0) -> float | None:
    """每 1 美元名义的净期望（bps）。fee 按官方 feeRate×p×(1-p) 每股。"""
    if price is None or price <= 0 or win is None:
        return None
    fee_ps = fee_rate * price * (1 - price)
    slip_ps = slip_bps / 1e4 * price
    return (win - price - fee_ps - slip_ps) / price * 1e4


def bucket_table(rows, buckets, *, fee_rate: float, slip_bps: float) -> list[dict]:
    out = []
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= r["p"] < hi]
        if not sub:
            continue
        n = len(sub)
        k = sum(1 for r in sub if r["yes"])
        avg = sum(r["p"] for r in sub) / n
        rate = k / n
        lb = wilson_lb(k, n)
        out.append({
            "lo": lo, "hi": min(hi, 1.0), "n": n,
            "avg_price": round(avg, 4),
            "win_rate": round(rate, 4),
            "wilson_lb": round(lb, 4),
            "gross_bps": round(net_edge_bps(rate, avg, 0.0, 0.0), 1),
            "net_bps": round(net_edge_bps(rate, avg, fee_rate, slip_bps), 1),
            "lb_net_bps": round(net_edge_bps(lb, avg, fee_rate, slip_bps), 1),
            "median_vol": round(statistics.median(r["vol"] for r in sub)),
        })
    return out


def _print_bucket_table(title: str, table: list[dict], *, fee_rate: float,
                        slip_bps: float) -> None:
    print(f"\n{title}")
    print(f"  {'价格区间':<12}{'样本':>6}{'均价':>8}{'兑现率':>8}{'Wilson下界':>11}"
          f"{'毛边bps':>9}{'净边bps':>9}{'下界净bps':>10}  判定")
    for b in table:
        verdict = "**显著为正**" if (b["lb_net_bps"] or -1e9) > 0 else "否"
        print(f"  {b['lo']:.2f}-{b['hi']:.2f}  {b['n']:6d}{b['avg_price']:8.4f}"
              f"{b['win_rate']:8.4f}{b['wilson_lb']:11.4f}{b['gross_bps']:+9.1f}"
              f"{b['net_bps']:+9.1f}{b['lb_net_bps']:+10.1f}  {verdict}")
    print(f"  （净边 = 扣 fee {fee_rate:.2f}×p×(1-p) 与滑点 {slip_bps:.0f}bps 名义）")


def first_cross(path, threshold: float, max_price: float = 0.995):
    """价格首次跨入 [threshold, max_price] 的时点。

    这是可直接交易的入场事件（"第一次触到 0.95 就买"），
    每个市场只贡献一条，避免同一市场重复计数造成的伪样本量。
    """
    for i, (ts, p) in enumerate(path):
        if threshold <= p <= max_price:
            return ts, p, i
    return None


def first_cross_low(path, threshold: float, min_price: float = 0.005):
    """价格首次跌到 <= threshold（且 >= min_price）的时点，用于对手盘事件。"""
    for i, (ts, p) in enumerate(path):
        if min_price <= p <= threshold:
            return ts, p, i
    return None


def refine_cross(token: str, ts_ms: int, threshold: float, *, low: bool = False,
                 back_min: int = 70, fwd_min: int = 10):
    """小时级只是"首个整点已在区间"，真实首次触价要靠分钟级。

    实测小时级首触均价 0.9775，分钟级能拉回 0.95-0.96 —— 这个差别
    直接决定边际（0.9775 只有 230bps 毛边，0.955 有 470bps）。
    """
    s = int(ts_ms / 1000 - back_min * 60)
    e = int(ts_ms / 1000 + fwd_min * 60)
    try:
        d = http_json(f"{CLOB}/prices-history?market={token}"
                      f"&startTs={s}&endTs={e}&fidelity=1", timeout=30)
    except ProbeError:
        return None
    pts = []
    for x in d.get("history", []):
        v = _f(x.get("p"))
        if v is not None:
            pts.append((_norm_ts(x["t"]), v))
    pts.sort()
    for t, v in pts:
        if (v <= threshold) if low else (v >= threshold):
            return t, v
    return None


def event_stats(rows, fee_rate: float, slip_bps: float) -> dict | None:
    n = len(rows)
    if n == 0:
        return None
    k = sum(1 for r in rows if r["yes"])
    avg = sum(r["p"] for r in rows) / n
    lb = wilson_lb(k, n)
    return {"n": n, "avg_price": round(avg, 4), "win_rate": round(k / n, 4),
            "wilson_lb": round(lb, 4),
            "gross_bps": round(net_edge_bps(k / n, avg), 1),
            "net_bps": round(net_edge_bps(k / n, avg, fee_rate, slip_bps), 1),
            "lb_net_bps": round(net_edge_bps(lb, avg, fee_rate, slip_bps), 1),
            "median_hours_to_close": round(statistics.median(r["lead_h"] for r in rows), 2),
            "median_vol": round(statistics.median(r["vol"] for r in rows))}


def _print_event(title: str, st: dict | None, fee_rate: float, slip_bps: float) -> None:
    if not st:
        print(f"\n{title}：无样本")
        return
    flag = "**下界净为正**" if (st["lb_net_bps"] or -1e9) > 0 else "否"
    print(f"\n{title}")
    print(f"  n={st['n']}  入场均价 {st['avg_price']:.4f}  兑现率 {st['win_rate']:.4f}  "
          f"Wilson 下界 {st['wilson_lb']:.4f}")
    print(f"  毛边 {st['gross_bps']:+.1f}bps  净边(费{fee_rate:.2f}+滑{slip_bps:.0f}) "
          f"{st['net_bps']:+.1f}bps  下界净 {st['lb_net_bps']:+.1f}bps  → {flag}")
    print(f"  入场时距停盘中位 {st['median_hours_to_close']:.1f}h  "
          f"中位成交额 ${st['median_vol']:,.0f}")


def cmd_calibrate(args) -> int:
    """已结算二元市场：价格 vs 实际结果。

    两种采样口径，回答的是两个不同问题：
      A. 固定时间窗（停盘前 6/24/48h）：某一时刻的横截面定价准不准。
         样本天然稀少——多数高成交市场在最后几小时已经贴在 0.001。
      B. 首次跨入区间（事件研究）：从 0.95 第一次被触及时买入，赚不赚。
         每个市场一条，样本量足，且直接对应可执行的入场规则。

    锚点用 closedTime（实测与最后成交相差中位 0.4h），
    不用 endDate（与真实关闭相差中位 -195h、最大 3453h）。
    """
    windows = tuple(sorted({float(w) for w in str(args.windows).split(",") if w.strip()}))
    fee_rate, slip = args.fee, args.slip
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mcache = DATA_DIR / f"cache_markets_{args.markets}_{int(args.min_vol)}.json"
    mk = None
    if not args.no_cache and mcache.exists():
        try:
            mk = json.loads(mcache.read_text(encoding="utf-8"))
            print(f"用市场缓存 {mcache.name}（{len(mk)} 个，--no-cache 可强制重拉）")
        except (json.JSONDecodeError, OSError):
            mk = None
    if mk is None:
        print("拉已结算市场…", flush=True)
        mk = fetch_closed_markets(args.markets, args.min_vol,
                                  max_pages=args.markets // 100 + 20)
        if mk and not args.no_cache:
            mcache.write_text(json.dumps(mk, ensure_ascii=False), encoding="utf-8")
    print(f"  {len(mk)} 个（成交额 >= ${args.min_vol:,.0f}）")
    if not mk:
        return 1

    eids = set()
    for m in mk:
        ev = (m.get("events") or [{}])[0]
        if ev.get("id"):
            eids.add(ev["id"])
    tcache = DATA_DIR / "cache_event_tags.json"
    tag_map = {}
    if not args.no_cache and tcache.exists():
        try:
            tag_map = json.loads(tcache.read_text(encoding="utf-8"))
            print(f"用标签缓存（{len(tag_map)} 个 event）")
        except (json.JSONDecodeError, OSError):
            tag_map = {}
    missing = [e for e in eids if e not in tag_map]
    if missing:
        print(f"拉事件标签（{len(missing)} 个 event 待拉，共 {len(eids)} 个）…", flush=True)
        tag_map.update(fetch_event_tags(missing, threads=min(16, args.threads)))
        if not args.no_cache:
            tcache.write_text(json.dumps(tag_map, ensure_ascii=False), encoding="utf-8")

    errs = []

    def one(m):
        try:
            return _one(m)
        except Exception as exc:          # 单个市场的脏数据不该毁掉整轮
            if len(errs) < 5:
                errs.append(f"{type(exc).__name__}: {exc}")
            return None

    def _one(m):
        anchor = _parse_end(m.get("closedTime")) or _parse_end(m.get("endDate"))
        if not anchor:
            return None
        try:
            prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
        except (json.JSONDecodeError, TypeError):
            return None
        if not prices or len(prices) != 2:
            return None
        y, n = _f(prices[0]), _f(prices[1])
        if y is None or n is None or abs(y + n - 1.0) > 1e-6 or y not in (0.0, 1.0):
            return None                      # 只留干净的二元结果（排除 0.5/0.5 无效）
        toks = _tokens(m)
        path = price_path(toks[0], anchor)
        if not path:
            return None
        ev = (m.get("events") or [{}])[0]
        cat = classify(tag_map.get(ev.get("id")), m.get("question") or "")
        base = {"id": m.get("id"), "q": (m.get("question") or "")[:120], "cat": cat,
                "vol": _f(m.get("volumeNum")) or 0.0, "yes": y == 1.0,
                "fee_type": m.get("feeType")}
        fixed, events = [], []
        for h in windows:
            got = sample_at(path, anchor - int(h * 3_600_000))
            if not got:
                continue
            p, lag = got
            if not (0.01 <= p <= 0.99):
                continue
            longer = [w for w in windows if w > h]
            prev = None
            if longer:
                got_prev = sample_at(path, anchor - int(longer[0] * 3_600_000))
                if got_prev:
                    prev = got_prev[0]
            fixed.append({**base, "h": h, "p": p, "lag_h": round(lag, 2), "prev_p": prev})
        for thr in (0.95, 0.98):
            hit = first_cross(path, thr)
            if not hit:
                continue
            ts, p, idx = hit
            censored = (idx == 0)        # 14 天窗口起点已在区间内，真实首触不可知
            if not censored:
                fine = refine_cross(toks[0], ts, thr)
                if fine:
                    ts, p = fine
            events.append({**base, "thr": thr, "p": p, "censored": censored,
                           "lead_h": round((anchor - ts) / 3_600_000.0, 2)})
        # 对手盘：YES 首次跌到 <= 0.05 / 0.02，等价于低价买 NO
        for thr in (0.95, 0.98):
            hit = first_cross_low(path, 1.0 - thr)
            if not hit:
                continue
            ts, p, idx = hit
            censored = (idx == 0)
            if not censored:
                fine = refine_cross(toks[0], ts, 1.0 - thr, low=True)
                if fine:
                    ts, p = fine
            events.append({**base, "thr": -thr, "p": 1.0 - p, "yes": not base["yes"],
                           "censored": censored,
                           "lead_h": round((anchor - ts) / 3_600_000.0, 2)})
        if not fixed and not events:
            return None
        return {"fixed": fixed, "events": events}

    t0 = time.time()
    fixed_rows, event_rows, used = [], [], set()
    with cf.ThreadPoolExecutor(max_workers=args.threads) as ex:
        for res in ex.map(one, mk):
            if not res:
                continue
            fixed_rows.extend(res["fixed"])
            event_rows.extend(res["events"])
            if res["fixed"] or res["events"]:
                used.add((res["fixed"] or res["events"])[0]["id"])
    print(f"  可用市场 {len(used)} / {len(mk)}，固定窗观测 {len(fixed_rows)}，"
          f"跨入事件 {len(event_rows)}（{time.time()-t0:.1f}s）")
    if errs:
        print(f"  单市场异常 {len(errs)} 例（已隔离，未中断本轮）：")
        for e in errs[:5]:
            print(f"    {e}")

    summary = {"generated_utc": int(time.time() * 1000), "anchor": "closedTime",
               "windows": list(windows), "min_volume": args.min_vol,
               "markets_requested": args.markets, "markets_used": len(used),
               "fixed_observations": len(fixed_rows),
               "event_observations": len(event_rows),
               "assumptions": {"fee_rate": fee_rate, "slip_bps": slip,
                               "fee_formula": "feeRate * p * (1-p) per share",
                               "price_source": "clob prices-history, hourly"},
               "fixed_horizon": {}, "events": {}, "event_by_price": {},
               "event_by_category": {}, "event_by_lead": {},
               "mirror_events": {}, "failures": []}

    # ================= A. 固定时间窗 =================
    print("\n" + "=" * 78)
    print("A. 固定时间窗（横截面定价）")
    for h in windows:
        sub = [r for r in fixed_rows if r["h"] == h]
        if not sub:
            continue
        table = bucket_table(sub, BUCKETS_HIGH, fee_rate=fee_rate, slip_bps=slip)
        _print_bucket_table(f"停盘前 {int(h)}h｜YES 侧（n={len(sub)}）", table,
                            fee_rate=fee_rate, slip_bps=slip)
        summary["fixed_horizon"][f"{int(h)}h"] = {"n": len(sub), "buckets": table}

    # ================= B. 首次跨入事件 =================
    print("\n" + "=" * 78)
    print("B. 首次跨入区间（可执行入场事件，每市场一条）")
    cens = {thr: sum(1 for r in event_rows if r["thr"] == thr and r["censored"])
            for thr in (0.95, 0.98)}
    summary["censored"] = {f"{k:.2f}": v for k, v in cens.items()}
    print(f"  14 天窗口起点已在区间内（截断，真实首触不可知）："
          f"0.95 → {cens[0.95]} 个，0.98 → {cens[0.98]} 个")
    for thr in (0.95, 0.98):
        sub = [r for r in event_rows if r["thr"] == thr and not r["censored"]]
        st = event_stats(sub, fee_rate, slip)
        _print_event(f"首次触及 {thr:.2f} 买入 YES（n={len(sub)}）", st, fee_rate, slip)
        summary["events"][f"{thr:.2f}"] = st
        for lo, hi in ((thr, thr + 0.01), (thr + 0.01, thr + 0.02),
                       (thr + 0.02, thr + 0.03), (thr + 0.03, 0.995)):
            part = [r for r in sub if lo <= r["p"] < hi]
            if len(part) < 5:
                continue
            st2 = event_stats(part, fee_rate, slip)
            summary["event_by_price"].setdefault(f"{thr:.2f}", []).append(st2)
            print(f"    入场价 {lo:.2f}-{hi:.2f}: n={st2['n']:4d} 均价 {st2['avg_price']:.4f} "
                  f"兑现 {st2['win_rate']:.4f} 下界 {st2['wilson_lb']:.4f} "
                  f"净边 {st2['net_bps']:+7.1f}bps 下界净 {st2['lb_net_bps']:+7.1f}bps")

    # 分品类（0.95 事件）
    print("\n0.95 事件按品类：")
    for cat in ("crypto", "sports", "politics", "other"):
        sub = [r for r in event_rows if r["thr"] == 0.95 and r["cat"] == cat
               and not r["censored"]]
        if len(sub) < 5:
            continue
        st = event_stats(sub, fee_rate, slip)
        summary["event_by_category"][cat] = st
        print(f"  {cat:9s} n={st['n']:4d} 均价 {st['avg_price']:.4f} 兑现 {st['win_rate']:.4f} "
              f"下界 {st['wilson_lb']:.4f} 毛边 {st['gross_bps']:+7.1f}bps "
              f"下界净 {st['lb_net_bps']:+7.1f}bps 距停盘 {st['median_hours_to_close']:.1f}h")

    # 按入场时距停盘时长拆解
    print("\n0.95 事件按入场时距停盘时长：")
    leads = (("<=6h", 0, 6), ("6-24h", 6, 24), ("24-72h", 24, 72), (">72h", 72, 1e9))
    for name, lo, hi in leads:
        sub = [r for r in event_rows if r["thr"] == 0.95 and lo <= r["lead_h"] < hi
               and not r["censored"]]
        if len(sub) < 5:
            continue
        st = event_stats(sub, fee_rate, slip)
        summary["event_by_lead"][name] = st
        print(f"  {name:7s} n={st['n']:4d} 均价 {st['avg_price']:.4f} 兑现 {st['win_rate']:.4f} "
              f"下界 {st['wilson_lb']:.4f} 毛边 {st['gross_bps']:+7.1f}bps "
              f"下界净 {st['lb_net_bps']:+7.1f}bps")

    # 翻车明细
    fails = [r for r in event_rows if r["thr"] == 0.95 and not r["yes"]
             and not r["censored"]]
    fails.sort(key=lambda r: -r["vol"])
    summary["failures"] = fails[:40]
    print(f"\n0.95 事件中翻车（买入后归零）{len(fails)} 条：")
    for r in fails[:15]:
        print(f"  入场 {r['p']:.4f} 距停盘 {r['lead_h']:7.1f}h 量 ${r['vol']:>11,.0f} "
              f"{r['cat']:8s} {r['q'][:52]}")

    # ================= C. 对手盘 =================
    print("\n" + "=" * 78)
    print("C. 对手盘镜像（YES 跌到低位时买 NO，等价于在 1-p 上做多）")
    for thr in (0.95, 0.98):
        sub = [r for r in event_rows if r["thr"] == -thr and not r["censored"]]
        st = event_stats(sub, fee_rate, slip)
        _print_event(f"YES 首次跌破 {1-thr:.2f} → 买 NO（n={len(sub)}）", st, fee_rate, slip)
        summary["mirror_events"][f"{1-thr:.2f}"] = st

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    out = DATA_DIR / f"calibration_{stamp}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    detail = DATA_DIR / f"calibration_detail_{stamp}.json"
    detail.write_text(json.dumps({"fixed": fixed_rows, "events": event_rows},
                                 ensure_ascii=False), encoding="utf-8")
    print(f"\n写入 {out}\n明细 {detail}")
    return 0


# ---------------------------------------------------------------- 生命周期

@dataclass
class Opp:
    key: str
    kind: str
    title: str
    first_ms: int
    last_ms: int
    sweeps: int = 1
    best_sum_ask: float = 9.9
    max_deployable: float = 0.0
    best_net_bps: dict = field(default_factory=dict)
    complete: bool = True
    n_legs: int = 0

    def duration_s(self, sweep_s: float) -> float:
        # 保守：首末之间 + 一个扫描周期（首见轮与末见轮都可能覆盖半个周期）
        return (self.last_ms - self.first_ms) / 1000.0 + sweep_s


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="Polymarket 只读盘口套利探测")
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("snapshot")
    s1.add_argument("--max-events", type=int, default=1200)
    s1.add_argument("--threads", type=int, default=8)
    s1.add_argument("--max-tokens", type=int, default=4000)
    s1.add_argument("--min-liq", type=float, default=5000.0)

    s2 = sub.add_parser("probe")
    s2.add_argument("--minutes", type=float, default=10.0)
    s2.add_argument("--max-events", type=int, default=1200)
    s2.add_argument("--max-tokens", type=int, default=4000)
    s2.add_argument("--threads", type=int, default=8)
    s2.add_argument("--rediscover-min", type=float, default=5.0)
    s2.add_argument("--min-liq", type=float, default=5000.0)
    s3 = sub.add_parser("tail")
    s3.add_argument("--max-events", type=int, default=1200)
    s3.add_argument("--threads", type=int, default=8)
    s3.add_argument("--min-liq", type=float, default=0.0)
    s3.add_argument("--max-hours", type=float, default=48.0)
    s3.add_argument("--lo", type=float, default=0.90)
    s3.add_argument("--hi", type=float, default=0.995)
    s4 = sub.add_parser("calibrate")
    s4.add_argument("--markets", type=int, default=300)
    s4.add_argument("--min-vol", type=float, default=10_000.0)
    s4.add_argument("--windows", default="6,24,48", help="结算前小时数，逗号分隔")
    s4.add_argument("--fee", type=float, default=0.01, help="feeRate（0/0.01/0.05）")
    s4.add_argument("--slip", type=float, default=35.0, help="滑点 bps（名义）")
    s4.add_argument("--threads", type=int, default=16)
    s4.add_argument("--no-cache", action="store_true", help="忽略市场/标签缓存，强制重拉")
    args = p.parse_args(argv)
    return {"snapshot": cmd_snapshot, "probe": cmd_probe, "tail": cmd_tail,
            "calibrate": cmd_calibrate}[args.cmd](args)


def _universe(args):
    print("发现中…", flush=True)
    u = discover(args.max_events, threads=min(6, args.threads))
    pre = prefilter(u, min_liq=getattr(args, "min_liq", 5000.0))
    print(f"  事件 {u['meta']['events_total']} | negRisk {u['meta']['neg_events']} "
          f"({u['meta']['neg_legs']} 腿) | 二元 {u['meta']['binaries']} | {u['meta']['discover_s']}s")
    print(f"  预筛：negRisk 候选 {len(pre['neg_hot'])}，二元候选 {len(pre['bin_hot'])}，"
          f"negRisk 缺腿 {len(pre['neg_incomplete_hint'])}")
    return u, pre


def _hot_tokens(pre, max_tokens: int) -> list[str]:
    toks = []
    for ev in pre["neg_hot"]:
        for leg in ev["legs"]:
            toks.append(leg["yes_token"])
    for m in pre["bin_hot"]:
        toks.extend([m["yes_token"], m["no_token"]])
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:max_tokens]


def cmd_snapshot(args) -> int:
    u, pre = _universe(args)
    toks = _hot_tokens(pre, args.max_tokens)
    t0 = time.time()
    books = fetch_books(toks, threads=args.threads)
    dt = time.time() - t0
    print(f"  盘口 {len(books)}/{len(toks)} 本，{dt:.2f}s（{len(books)/max(dt,1e-9):.0f} 本/s）")

    rows = []
    for ev in pre["neg_hot"]:
        r = eval_neg(ev, books)
        if r:
            rows.append(r)
    for m in pre["bin_hot"]:
        r = eval_binary(m, books)
        if r:
            rows.append(r)

    for r in sorted(rows, key=lambda x: -x["gross_edge"])[:20]:
        if not r["complete"]:
            print(f"  {r['kind']:8s} sum_ask={r['sum_ask']:.4f} mid={r['sum_mid']} "
                  f"** 疑似缺腿（不可套利）**  {r['title'][:44]}")
            continue
        f0 = r["f0.00_c100"]["net_bps"]
        f1 = r["f0.01_c100"]["net_bps"]
        f5 = r["f0.05_c100"]["net_bps"]
        print(f"  {r['kind']:8s} sum_ask={r['sum_ask']:.4f} mid={r['sum_mid']} "
              f"depth=${r['max_deployable']:.0f} "
              f"| $100 净(fee0/1%/5%) {f0:+.1f}/{f1:+.1f}/{f5:+.1f}bps  {r['title'][:44]}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"snapshot_{int(time.time())}.json"
    out.write_text(json.dumps({"meta": u["meta"], "prefilter":
                               {"neg_hot": len(pre["neg_hot"]), "bin_hot": len(pre["bin_hot"]),
                                "neg_incomplete": len(pre["neg_incomplete_hint"])},
                               "books": len(books), "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


def cmd_probe(args) -> int:
    u, pre = _universe(args)
    toks = _hot_tokens(pre, args.max_tokens)
    print(f"热点 token {len(toks)}，轮询 {args.minutes} 分钟\n")

    opps: dict[str, Opp] = {}
    incomplete: dict[str, dict] = {}
    sweeps = 0
    sweep_times = []
    last_discover = time.time()
    deadline = time.time() + args.minutes * 60
    all_rows = []

    while time.time() < deadline:
        st = time.time()
        try:
            books = fetch_books(toks, threads=args.threads)
        except ProbeError as exc:
            print(f"  盘口失败：{exc}")
            time.sleep(2)
            continue
        now_ms = int(time.time() * 1000)
        rows = []
        for ev in pre["neg_hot"]:
            r = eval_neg(ev, books, deep=False)
            if r:
                rows.append(r)
        for m in pre["bin_hot"]:
            r = eval_binary(m, books, deep=False)
            if r:
                rows.append(r)

        sweep_s = time.time() - st
        sweep_times.append(sweep_s)
        sweeps += 1

        for r in rows:
            if r["gross_edge"] <= 0:
                continue
            if not r["complete"]:
                # 看起来有价差，但 mids 不接近 1：结果集疑似缺腿，单列，不进"可吃"
                prev = incomplete.get(r["key"])
                if prev is None or r["gross_edge"] > prev["gross_edge"]:
                    incomplete[r["key"]] = {"title": r["title"], "kind": r["kind"],
                                            "sum_ask": r["sum_ask"], "sum_mid": r["sum_mid"],
                                            "gross_edge": r["gross_edge"], "n_legs": r["n_legs"]}
                continue
            o = opps.get(r["key"])
            best_net = {fr: r[f"f{fr:.2f}_c100"]["net_bps"] for fr in FEE_RATES}
            if o is None:
                opps[r["key"]] = Opp(key=r["key"], kind=r["kind"], title=r["title"],
                                     first_ms=now_ms, last_ms=now_ms,
                                     best_sum_ask=r["sum_ask"],
                                     max_deployable=r["max_deployable"],
                                     best_net_bps=best_net, complete=r["complete"],
                                     n_legs=r["n_legs"])
            else:
                o.last_ms = now_ms
                o.sweeps += 1
                o.best_sum_ask = min(o.best_sum_ask, r["sum_ask"])
                o.max_deployable = max(o.max_deployable, r["max_deployable"])
                for k, v in best_net.items():
                    o.best_net_bps[k] = max(o.best_net_bps.get(k, -9e9), v)
        all_rows.extend({"t": now_ms, **r} for r in rows if r["complete"] and r["gross_edge"] > 0)

        # 每轮打印一行进度（不刷屏）
        if sweeps % 5 == 1:
            alive = sum(1 for o in opps.values() if now_ms - o.last_ms < 2 * sweep_s * 1000)
            print(f"  [{sweeps:3d}] {sweep_s:.2f}s/轮  盘口 {len(books)}  本轮机会 "
                  f"{sum(1 for r in rows if r['complete'] and r['gross_edge']>0)}  "
                  f"累计 {len(opps)}  存活中 {alive}", flush=True)

        if time.time() - last_discover > args.rediscover_min * 60:
            print("  重新发现…", flush=True)
            u2, pre2 = _universe(args)
            pre, toks = pre2, _hot_tokens(pre2, args.max_tokens)
            last_discover = time.time()

    median_sweep = statistics.median(sweep_times) if sweep_times else 0.0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    with (DATA_DIR / f"opps_{stamp}.jsonl").open("w", encoding="utf-8") as fh:
        for r in all_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary = _summarize(opps, sweeps, median_sweep, len(toks), len(all_rows))
    summary["incomplete_apparent"] = sorted(incomplete.values(),
                                            key=lambda x: -x["gross_edge"])[:30]
    summary["incomplete_count"] = len(incomplete)
    summary["incomplete_max_edge"] = round(
        max([x["gross_edge"] for x in incomplete.values()], default=0.0), 4)
    (DATA_DIR / f"probe_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(summary)
    return 0


def _summarize(opps, sweeps, median_sweep, n_tokens, n_obs) -> dict:
    rows = []
    for o in sorted(opps.values(), key=lambda x: -x.max_deployable):
        dur = o.duration_s(median_sweep)
        rows.append({"key": o.key, "kind": o.kind, "title": o.title,
                     "sweeps_seen": o.sweeps, "duration_s": round(dur, 2),
                     "best_sum_ask": round(o.best_sum_ask, 6),
                     "max_deployable_usd": round(o.max_deployable, 2),
                     "best_net_bps_fee0": round(o.best_net_bps.get(0.0, 0), 2),
                     "best_net_bps_fee1": round(o.best_net_bps.get(0.01, 0), 2),
                     "best_net_bps_fee5": round(o.best_net_bps.get(0.05, 0), 2),
                     "n_legs": o.n_legs})
    long_lived = [r for r in rows if r["duration_s"] >= 3.0]
    return {
        "sweeps": sweeps, "median_sweep_s": round(median_sweep, 3),
        "tokens": n_tokens, "observations": n_obs,
        "opportunities": len(rows),
        "opps_3s": len(long_lived),
        "opps_net_positive_fee0": sum(1 for r in rows if r["best_net_bps_fee0"] > 0),
        "opps_net_positive_fee1": sum(1 for r in rows if r["best_net_bps_fee1"] > 0),
        "opps_net_positive_fee5": sum(1 for r in rows if r["best_net_bps_fee5"] > 0),
        "max_deployable_usd": max([r["max_deployable_usd"] for r in rows], default=0.0),
        "top": sorted(rows, key=lambda r: -r["duration_s"])[:30],
        "by_kind": dict(Counter(r["kind"] for r in rows)),
    }


def _print_summary(s: dict) -> None:
    print("\n=== 探测汇总 ===")
    print(f"扫描 {s['sweeps']} 轮，中位 {s['median_sweep_s']}s/轮，热点 {s['tokens']} token，"
          f"机会观测 {s['observations']} 条")
    print(f"出现过的机会 {s['opportunities']} 个（{s['by_kind']}），存活 >=3s 的 {s['opps_3s']} 个")
    print(f"净利>0 个数：fee 0% → {s['opps_net_positive_fee0']}，"
          f"1% → {s['opps_net_positive_fee1']}，5% → {s['opps_net_positive_fee5']}")
    print(f"最大可部署美元深度 ${s['max_deployable_usd']:.0f}")
    print(f"另有 {s.get('incomplete_count', 0)} 个\"看起来有价差但结果集疑似缺腿\"的事件，"
          f"最大表观价差 {s.get('incomplete_max_edge', 0)*100:.1f}%，已单列、不计入可吃")
    print("\n存活最久的 10 个：")
    for r in s["top"][:10]:
        print(f"  {r['duration_s']:6.1f}s  {r['kind']:8s} sum={r['best_sum_ask']:.4f} "
              f"depth=${r['max_deployable_usd']:.0f} "
              f"net(fee0/1%/5%)={r['best_net_bps_fee0']:+.1f}/{r['best_net_bps_fee1']:+.1f}/"
              f"{r['best_net_bps_fee5']:+.1f}bps  {r['title'][:40]}")


if __name__ == "__main__":
    sys.exit(main())
