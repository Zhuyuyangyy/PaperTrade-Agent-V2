"""双路 WebSocket 延迟探测：外部事实落地 → Polymarket 盘口改价。

回答的问题：**信息差到底有没有可吃的时间窗。**
不是猜方向，是量"我比盘口快多少"。

设计
----
- 事件源（t0）：Binance 现货 aggTrade。两种触发——
  ① 1 秒内价格跃变 >= `--jump-bps`；② 价格穿越目标市场的行权价。
- 反应（t1）：该市场的 token 在 t0 之后的第一条 Polymarket 推送。
- 旧档消失（t2）：t0 前的卖一价不再是卖一的时刻（被吃或撤单重报）。
- 残留深度：t0 前卖一档的挂单量 × 价格（美元）。

时间基准
--------
两边都用**本地收包时刻**（time.time()），因为这才是"我能看到"的延迟；
同时记录交易所时间戳，单独报 feed lag，避免把网络延迟算成市场反应。

用法
----
    python research/polymarket/latency_probe.py --minutes 30
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import re
import statistics
import sys
import threading
import time
from pathlib import Path

import websocket

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arb_probe as ap  # noqa: E402

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams={streams}"
STRIKE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")


def parse_strike(q: str):
    m = STRIKE_RE.search(q or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def pick_targets(max_targets: int = 10, max_pages: int = 4):
    """挑行权价离现价最近的 BTC/ETH 'above $X' 市场。"""
    rows = []
    for off in range(0, max_pages * 100, 100):
        try:
            rows += ap.fetch_events(off)
        except ap.ProbeError:
            break
    out, seen = [], set()
    for e in rows:
        for m in (e.get("markets") or []):
            q = (m.get("question") or "")
            ql = q.lower()
            if not ap._tradable(m):
                continue
            if "above" not in ql and "dip to" not in ql:
                continue
            if "bitcoin" in ql or "btc" in ql:
                sym = "BTCUSDT"
            elif "ethereum" in ql or "eth " in ql:
                sym = "ETHUSDT"
            else:
                continue
            strike = parse_strike(q)
            if not strike:
                continue
            cid = m.get("conditionId")
            if not cid or cid in seen:
                continue
            toks = ap._tokens(m)
            if len(toks) != 2:
                continue
            seen.add(cid)
            out.append({"condition_id": cid, "symbol": sym, "strike": strike,
                        "question": q[:80], "yes": toks[0], "no": toks[1],
                        "up": "above" in ql, "volume": ap._f(m.get("volume24hr")) or 0.0,
                        "end": (m.get("endDate") or "")[:16]})
    return out


def last_price(symbol: str) -> float | None:
    """用 REST 现价给行权价距离排序（只用于挑标的，不参与测量）。"""
    try:
        d = ap.http_json(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                         timeout=10)
        return float(d["price"])
    except Exception:
        return None


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.quote = {}          # token -> dict(bid, ask, bid_sz, ask_sz, ts, exch)
        self.updates = []        # (ts, token, bid, ask)
        self.events = []         # dict(kind, symbol, t0, market, price, strike, snapshot)
        self.last_px = {}        # symbol -> (ts, price)
        self.history = collections.defaultdict(collections.deque)   # symbol -> deque[(ts, px)]
        self.last_emit = {}      # debounce key -> ts
        self.msgs = collections.Counter()
        self.feed_lag = {"bin": [], "poly": []}
        self.tokens = []
        self.strikes = {}        # symbol -> [(strike, market)]
        self.last_emit_px = {}   # symbol -> 上次事件时的价格

    # ---------- Binance ----------
    def on_binance(self, msg, now_ms):
        d = msg.get("data") or msg
        if d.get("e") != "aggTrade":
            return
        sym = d.get("s")
        px = float(d["p"])
        exch = int(d.get("T") or d.get("E") or 0)
        self.msgs["bin"] += 1
        if exch:
            self.feed_lag["bin"].append(now_ms - exch)
        with self.lock:
            h = self.history[sym]
            h.append((now_ms, px))
            while h and now_ms - h[0][0] > 5000:
                h.popleft()
            prev = self.last_px.get(sym)
            self.last_px[sym] = (now_ms, px)
            if prev is None:
                self.last_emit_px.setdefault(sym, px)
                return
            # 触发②：价格穿越行权价
            for strike, label in self.strikes.get(sym, []):
                if (prev[1] - strike) * (px - strike) < 0:
                    key = f"cross:{sym}:{strike}"
                    last = self.last_emit.get(key)
                    if last is None or now_ms - last > 1500:
                        self.last_emit[key] = now_ms
                        snap = {t: dict(self.quote.get(t) or {}) for t in self.tokens}
                        self.events.append({"kind": "cross", "symbol": sym, "t0": now_ms,
                                            "price": px, "move_bps": 0.0,
                                            "strike": strike, "label": label, "snap": snap})
            # 基准 = 上一次事件时的价格（累计口径）。用固定 1s 窗口会几乎打不出事件：
            # BTC 1 秒波动的标准差只有 ~0.9bps，15bps 是 17 个标准差。
            ref = self.last_emit_px.get(sym)
            if ref and ref > 0:
                move = (px / ref - 1) * 1e4
                if abs(move) >= self.jump_bps:
                    key = f"jump:{sym}"
                    last = self.last_emit.get(key)
                    if last is None or now_ms - last > 1000:
                        self.last_emit[key] = now_ms
                        snap = {t: dict(self.quote.get(t) or {}) for t in self.tokens}
                        self.events.append({"kind": "jump", "symbol": sym, "t0": now_ms,
                                            "price": px, "move_bps": round(move, 1),
                                            "snap": snap})

    # ---------- Polymarket ----------
    def on_poly(self, msg, now_ms):
        et = msg.get("event_type")
        exch = int(msg.get("timestamp") or 0)
        self.msgs["poly"] += 1
        if exch:
            self.feed_lag["poly"].append(now_ms - exch)
        with self.lock:
            if et == "book":
                tok = msg.get("asset_id")
                if not tok:
                    return
                asks = [(ap._f(l.get("price")), ap._f(l.get("size")))
                        for l in (msg.get("asks") or [])]
                bids = [(ap._f(l.get("price")), ap._f(l.get("size")))
                        for l in (msg.get("bids") or [])]
                asks = [(p, s) for p, s in asks if p is not None]
                bids = [(p, s) for p, s in bids if p is not None]
                q = self.quote.setdefault(tok, {})
                if asks:
                    p, s = min(asks, key=lambda x: x[0])
                    q["ask"], q["ask_sz"] = p, s
                if bids:
                    p, s = max(bids, key=lambda x: x[0])
                    q["bid"], q["bid_sz"] = p, s
                q["ts"], q["exch"] = now_ms, exch
                self.updates.append((now_ms, tok, q.get("bid"), q.get("ask")))
                return
            if et != "price_change":
                return
            for ch in (msg.get("price_changes") or []):
                tok = ch.get("asset_id")
                if not tok:
                    continue
                q = self.quote.setdefault(tok, {})
                bb, ba = ap._f(ch.get("best_bid")), ap._f(ch.get("best_ask"))
                if bb is not None:
                    q["bid"] = bb
                if ba is not None:
                    q["ask"] = ba
                q["ts"], q["exch"] = now_ms, exch
                self.updates.append((now_ms, tok, q.get("bid"), q.get("ask")))

    def snapshot(self, tokens):
        with self.lock:
            return {t: dict(self.quote.get(t) or {}) for t in tokens}


class PolyFeed:
    def __init__(self, tokens, rec):
        self.tokens = tokens
        self.rec = rec
        self.stop = False
        self.ws = None

    def run(self):
        def ping():
            while not self.stop:
                time.sleep(8)
                try:
                    if self.ws:
                        self.ws.send("PING")
                except Exception:
                    return
        threading.Thread(target=ping, daemon=True).start()
        while not self.stop:
            try:
                self.ws = websocket.create_connection(
                    POLY_WS, timeout=20, http_proxy_host=None,
                    http_no_proxy=["*"], enable_multithread=True)
                self.ws.send(json.dumps({"assets_ids": self.tokens, "type": "market"}))
                while not self.stop:
                    self.ws.settimeout(15)
                    raw = self.ws.recv()
                    if not raw or raw.strip().upper() == "PONG":
                        continue
                    now = int(time.time() * 1000)
                    d = json.loads(raw)
                    for it in (d if isinstance(d, list) else [d]):
                        self.rec.on_poly(it, now)
            except Exception:
                if self.stop:
                    break
                time.sleep(2)


class BinanceFeed:
    def __init__(self, symbols, rec):
        self.symbols = symbols
        self.rec = rec
        self.stop = False
        self.ws = None

    def run(self):
        streams = "/".join(f"{s.lower()}@aggTrade" for s in self.symbols)
        url = BINANCE_WS.format(streams=streams)
        while not self.stop:
            try:
                self.ws = websocket.create_connection(
                    url, timeout=20, http_proxy_host=None, http_no_proxy=["*"])
                while not self.stop:
                    self.ws.settimeout(15)
                    raw = self.ws.recv()
                    if not raw:
                        continue
                    now = int(time.time() * 1000)
                    self.rec.on_binance(json.loads(raw), now)
            except Exception:
                if self.stop:
                    break
                time.sleep(2)


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q / 100))]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="外部事实 → Polymarket 改价 延迟探测")
    p.add_argument("--minutes", type=float, default=30.0)
    p.add_argument("--targets", type=int, default=10)
    p.add_argument("--jump-bps", type=float, default=3.0)
    p.add_argument("--match-window-ms", type=int, default=10_000)
    args = p.parse_args(argv)

    print("挑选标的…", flush=True)
    cands = pick_targets(max_targets=args.targets * 4)
    px = {s: last_price(s) for s in ("BTCUSDT", "ETHUSDT")}
    print(f"  现价 BTC {px['BTCUSDT']}  ETH {px['ETHUSDT']}")
    for c in cands:
        ref = px.get(c["symbol"])
        c["dist"] = abs(ref / c["strike"] - 1) if ref and c["strike"] else 9.9
    cands.sort(key=lambda c: (c["dist"], c.get("end") or ""))
    targets, used = [], set()
    for c in cands:
        k = (c["symbol"], c["strike"], c["up"])
        if k in used:
            continue
        used.add(k)
        targets.append(c)
        if len(targets) >= args.targets:
            break
    if not targets:
        print("没有合适的阈值市场")
        return 1
    for c in targets:
        print(f"  {c['symbol']:8s} 行权 {c['strike']:>10,.0f} 距现价 {c['dist']*100:5.2f}% "
              f"{'above' if c['up'] else 'dip'} 到期 {c['end']}  {c['question'][:46]}")

    rec = Recorder()
    rec.jump_bps = args.jump_bps
    tokens = [t for c in targets for t in (c["yes"], c["no"])]
    rec.tokens = tokens
    for c in targets:
        rec.strikes.setdefault(c["symbol"], []).append((c["strike"], c["condition_id"]))
    by_token = {}
    for c in targets:
        by_token[c["yes"]] = c
        by_token[c["no"]] = c

    pf = PolyFeed(tokens, rec)
    bf = BinanceFeed(sorted({c["symbol"] for c in targets}), rec)
    threading.Thread(target=pf.run, daemon=True).start()
    threading.Thread(target=bf.run, daemon=True).start()

    t0 = time.time()
    while time.time() - t0 < args.minutes * 60:
        time.sleep(20)
        with rec.lock:
            n_ev = len(rec.events)
            n_up = len(rec.updates)
        print(f"  {time.time()-t0:5.0f}s bin {rec.msgs['bin']:>7d} poly {rec.msgs['poly']:>6d} "
              f"事件 {n_ev} 报价更新 {n_up}", flush=True)

    pf.stop = bf.stop = True
    time.sleep(1)
    elapsed = time.time() - t0
    print("\n" + "=" * 74)
    print(f"运行 {elapsed:.0f}s，Binance {rec.msgs['bin']} 条，Polymarket {rec.msgs['poly']} 条")

    lag_b = pct(rec.feed_lag["bin"], 50)
    lag_p = pct(rec.feed_lag["poly"], 50)
    print(f"本地时钟 − 交易所时间戳 中位：Binance {lag_b:+}ms，Polymarket {lag_p:+}ms")
    print("  （负值 = 本机时钟慢。t1−t0 两边都用本地时钟，偏移自动抵消，不影响结论）")

    events = rec.events
    n_jump = sum(1 for e in events if e["kind"] == "jump")
    n_cross = sum(1 for e in events if e["kind"] == "cross")
    print(f"\n事件：跃变（>={args.jump_bps}bps/1s）{n_jump} 个，穿越行权价 {n_cross} 个")
    if not events:
        print("  样本太少，延长运行或降低 --jump-bps")
        return 0
    moves = [e["move_bps"] for e in events]
    print(f"  跃变幅度：p25 {pct(moves,25):+.1f}  p50 {pct(moves,50):+.1f}  p90 {pct(moves,90):+.1f} bps")

    # 只保留该市场 token 的更新序列，按市场分组
    ups = sorted(rec.updates, key=lambda x: x[0])
    upd_ts = [u[0] for u in ups]
    idx_by_token = collections.defaultdict(list)
    for i, (ts, tok, _b, _a) in enumerate(ups):
        idx_by_token[tok].append(i)
    ts_by_token = {tok: [ups[i][0] for i in arr] for tok, arr in idx_by_token.items()}

    def first_update_after(tokens_, t0_ms):
        best = None
        for tok in tokens_:
            arr = idx_by_token.get(tok)
            if not arr:
                continue
            j = bisect.bisect_right(ts_by_token[tok], t0_ms)
            if j < len(arr):
                i = arr[j]
                if best is None or ups[i][0] < best[0]:
                    best = (ups[i][0], tok, i)
        return best

    # 事件发生时的盘口快照，用于算"旧档位"
    lat1, lat2, depths = [], [], []
    matched = 0
    rows = []
    for e in events:
        mkts = [c for c in targets if c["symbol"] == e["symbol"]]
        if not mkts:
            continue
        toks = [t for c in mkts for t in (c["yes"], c["no"])]
        hit = first_update_after(toks, e["t0"])
        if not hit or hit[0] - e["t0"] > args.match_window_ms:
            continue
        matched += 1
        t1, tok, i = hit
        lat1.append(t1 - e["t0"])
        c = by_token.get(tok)
        # 旧档：用事件前最后一条该 token 的报价
        pre = None
        for j in reversed(idx_by_token.get(tok, [])):
            if ups[j][0] <= e["t0"]:
                pre = ups[j]
                break
        if pre and pre[3] is not None:
            # t2：卖一价不再是原来那个价的时刻
            t2 = None
            for j in idx_by_token.get(tok, []):
                ts, _tk, _b, a = ups[j]
                if ts > e["t0"] and a is not None and abs(a - pre[3]) > 1e-9:
                    t2 = ts
                    break
            if t2:
                lat2.append(t2 - e["t0"])
            snap = e.get("snap") or {}
            q = snap.get(tok) or {}
            if q.get("ask") is not None and q.get("ask_sz"):
                depths.append(q["ask_sz"] * q["ask"])
        rows.append({"t0": e["t0"], "t1_ms": t1 - e["t0"],
                     "symbol": e["symbol"], "move_bps": e["move_bps"],
                     "strike": (c or {}).get("strike"), "question": (c or {}).get("question", "")})

    print(f"  其中 {matched} 个在 {args.match_window_ms/1000:.0f}s 内出现 Polymarket 更新")

    # ---- 安慰剂对照：同样的"到下一次更新"距离，但 t0 提前 5 秒（与事件无关）----
    placebo = []
    for e in events:
        toks = [t for c in targets if c["symbol"] == e["symbol"] for t in (c["yes"], c["no"])]
        hit = first_update_after(toks, e["t0"] - 5000)
        if hit:
            placebo.append(min(hit[0] - (e["t0"] - 5000), args.match_window_ms))
    if placebo:
        print(f"\n安慰剂对照（t0 前移 5s，同一批 token）：p25 {pct(placebo,25)}ms "
              f"p50 {pct(placebo,50)}ms p90 {pct(placebo,90)}ms")
        print("  → 若与上面 t1 分布接近，说明'很快就有更新'只是盘口 churn，不是反应")
    if lat1:
        print(f"\nt1 − t0（事实落地 → 首次改价）：")
        print(f"  p25 {pct(lat1,25)}ms  p50 {pct(lat1,50)}ms  p90 {pct(lat1,90)}ms  "
              f"min {min(lat1)}ms  max {max(lat1)}ms")
        within = {300: 0, 500: 0, 1000: 0, 3000: 0}
        for x in lat1:
            for k in within:
                if x <= k:
                    within[k] += 1
        print("  分布：" + "  ".join(f"<={k}ms {v/len(lat1)*100:.0f}%" for k, v in within.items()))
    if lat2:
        print(f"\nt2 − t0（旧卖一价被吃/重报）：")
        print(f"  p25 {pct(lat2,25)}ms  p50 {pct(lat2,50)}ms  p90 {pct(lat2,90)}ms")
    gaps = []
    for tok, arr in idx_by_token.items():
        tsl = [ups[i][0] for i in arr]
        gaps += [b - a for a, b in zip(tsl, tsl[1:])]
    if gaps:
        print(f"\n盘口更新间隔（决定反应速度下限）：p25 {pct(gaps,25)}ms "
              f"p50 {pct(gaps,50)}ms p90 {pct(gaps,90)}ms（n={len(gaps)}）")

    # ---- 方向相关性：多时间尺度。若 0.5s 无相关、5s 有，说明市场反应慢但在反应 ----
    print("\n方向相关性：Binance 变动 vs 该市场 YES 卖一在 N 毫秒后的变动")
    print(f"  {'前瞻窗口':<10}{'n':>5}{'相关系数':>9}{'同号%':>7}{'|dP|中位bps':>12}  解读")
    horizons = (500, 2000, 5000, 15000)
    corr_by_h = {}
    for H in horizons:
        pairs = []
        for e in events:
            mk = [c for c in targets if c["symbol"] == e["symbol"] and c["up"]]
            if not mk:
                continue
            c = min(mk, key=lambda x: abs(x["strike"] - e["price"]))
            arr = idx_by_token.get(c["yes"]) or []
            if not arr:
                continue
            tsl = ts_by_token[c["yes"]]
            i0 = bisect.bisect_right(tsl, e["t0"]) - 1
            i1 = bisect.bisect_right(tsl, e["t0"] + H) - 1
            if i0 < 0 or i1 <= i0:
                continue
            a0, a1 = ups[arr[i0]][3], ups[arr[i1]][3]
            if not a0 or not a1:
                continue
            pairs.append((e["move_bps"], (a1 / a0 - 1) * 1e4))
        if len(pairs) < 10:
            continue
        xs = [a for a, _ in pairs]
        ys = [b for _, b in pairs]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        sx = (sum((a - mx) ** 2 for a in xs) / len(pairs)) ** 0.5
        sy = (sum((b - my) ** 2 for b in ys) / len(pairs)) ** 0.5
        c2 = (sum((a - mx) * (b - my) for a, b in pairs) / len(pairs)) / (sx * sy) if sx and sy else 0.0
        same = sum(1 for a, b in pairs if a * b > 0) / len(pairs) * 100
        med = statistics.median(abs(b) for b in ys)
        corr_by_h[H] = {"n": len(pairs), "corr": round(c2, 3),
                        "same_sign_pct": round(same), "med_abs_dp_bps": round(med, 2)}
        tag = "无反应" if abs(c2) < 0.1 else ("有反应" if abs(c2) > 0.3 else "弱相关")
        print(f"  {H:<10}{len(pairs):>5}{c2:>9.3f}{same:>7.0f}{med:>12.2f}  {tag}")

    print(f"\n事件时刻卖一档残留深度（美元）中位："
          f"{statistics.median(depths) if depths else 0:,.0f}")

    out = ap.DATA_DIR / f"latency_{int(time.time())}.json"
    out.write_text(json.dumps({
        "generated_utc": int(time.time() * 1000), "seconds": round(elapsed, 1),
        "targets": [{k: c[k] for k in ("symbol", "strike", "question", "up", "dist")}
                    for c in targets],
        "binance_msgs": rec.msgs["bin"], "poly_msgs": rec.msgs["poly"],
        "feed_lag_ms": {"binance_p50": lag_b, "poly_p50": lag_p},
        "events": len(events), "matched": matched,
        "latency_t1_ms": {"p25": pct(lat1,25), "p50": pct(lat1,50), "p90": pct(lat1,90)},
        "latency_t2_ms": {"p25": pct(lat2,25), "p50": pct(lat2,50), "p90": pct(lat2,90)},
        "correlation_by_horizon": corr_by_h,
        "residual_depth_usd_p50": statistics.median(depths) if depths else 0,
        "update_gap_ms": {"p25": pct(gaps,25), "p50": pct(gaps,50), "p90": pct(gaps,90)},
        "samples": rows[:500],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
