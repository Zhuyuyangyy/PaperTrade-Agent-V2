"""Polymarket CLOB WebSocket 毫秒级探测器。

回答 Phase 0 报告里唯一没被证伪的问题：**REST 轮询看不到的 <2s 闪现，到底存不存在。**

设计要点
--------
1. 不重建整本簿也能判套利：`price_change` 每条 change 都带交易所当时的
   `best_bid` / `best_ask`，直接拿来算，避免本地撮合状态漂移。
2. 同时维护逐档阶梯，用 `book` 快照初始化、用 `price_change` 增量更新，
   并**自检**本地最优价与消息自带的 best 是否一致；不一致率单独报，不自欺。
3. 套利必须扣费：`fee = feeRate × p × (1−p)` 每条腿。p=0.5 时两条腿合计
   ≈50bps，一个最小报价单位（0.1¢）根本不够付——所以同时报"毛价差"和"净价差"。
4. 区分"真机会"和"报价拼接假象"：两条腿的推送是异步的，A 更新而 B 还是旧值时
   会出现瞬时 sum<1。只有**两条腿都在该状态下各自刷新过一次**才算 confirmed。

用法
----
    python research/polymarket/ws_probe.py --minutes 10
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import threading
import time
from pathlib import Path

try:                      # 只有真正建 WS 连接时才需要；单元测试只测纯函数解析逻辑，
    import websocket      # 不该因为环境没装这个包就让整个测试收集阶段失败。
except ModuleNotFoundError:   # pragma: no cover
    class _MissingWebsocket:
        """占位实现：保留调用点写法不变，只在真正建连时报出可操作的错误。"""
        WebSocketTimeoutException = TimeoutError

        @staticmethod
        def create_connection(*_a, **_kw):
            raise RuntimeError(
                "缺少 websocket-client：pip install websocket-client"
            )

    websocket = _MissingWebsocket()

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arb_probe as ap  # noqa: E402

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
FEE_RATE = 0.01          # 官方 0.01 / 0.05 两档；校准里 0.01 为主口径
FRESH_MS = 10_000        # 一条腿超过 10s 没更新就不参与判定
COMPLETE_MID_LO, COMPLETE_MID_HI = 0.97, 1.03   # 与 REST 阶段同一判据
DUR_BUCKETS = ((0, 100), (100, 500), (500, 1000), (1000, 3000),
               (3000, 10000), (10000, 60_000), (60_000, 10 ** 12))


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Feed:
    """一条 WebSocket 连接：订阅一批 token，解析推送并交给回调。"""

    def __init__(self, tokens, on_msg, *, shard_id=0):
        self.tokens = list(tokens)
        self.on_msg = on_msg
        self.shard_id = shard_id
        self.ws = None
        self.stop = False
        self.msgs = 0
        self.books = 0
        self.pc = 0
        self.trades = 0
        self.reconnects = 0
        self.error = None

    def _ping(self):
        while not self.stop:
            time.sleep(8)
            try:
                if self.ws:
                    self.ws.send("PING")
            except Exception:
                return

    def run(self):
        threading.Thread(target=self._ping, daemon=True).start()
        while not self.stop:
            try:
                self.ws = websocket.create_connection(
                    WS_URL, timeout=20, http_proxy_host=None,
                    http_no_proxy=["*"], enable_multithread=True)
                self.ws.send(json.dumps({"assets_ids": self.tokens, "type": "market"}))
                while not self.stop:
                    self.ws.settimeout(15)
                    raw = self.ws.recv()
                    if not raw:
                        continue
                    if raw.strip().upper() == "PONG":
                        continue
                    self.msgs += 1
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for it in (d if isinstance(d, list) else [d]):
                        et = it.get("event_type")
                        if et == "book":
                            self.books += 1
                        elif et == "price_change":
                            self.pc += 1
                        elif et == "last_trade_price":
                            self.trades += 1
                        self.on_msg(it)
            except Exception as exc:                     # 断线重连，不静默退出
                self.error = f"{type(exc).__name__}: {str(exc)[:80]}"
                if self.stop:
                    break
                self.reconnects += 1
                if self.reconnects > 20:
                    break
                time.sleep(2)
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.quote = {}                 # token -> {bid, ask, ts, bid_sz, ask_sz}
        self.markets = {}               # cid -> {y, n, q, liq}
        self.neg_events = {}            # eid -> {legs, title, n_legs}
        self.token_index = {}           # token -> [(kind, key)]
        self.open = {}                  # episode key -> dict
        self.closed = []                # 已结束 episode
        self.stats = collections.Counter()
        self.mismatch = collections.Counter()
        self.min_sum = {}               # key -> 历史最小 sum_ask
        self.updates = 0
        self.evaluated = set()          # 至少完成过一次完整判定的结构
        self.incomplete = set()         # Σ mid 不在 [0.97,1.03]，结果集缺腿

    # ---------------- 行情写入 ----------------
    def on_msg(self, msg):
        et = msg.get("event_type")
        ts = int(msg.get("timestamp") or 0)
        with self.lock:
            self.updates += 1
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
                # 本 API 的 bids 与 REST 一样不保证有序，两侧都取极值
                if asks:
                    best = min(asks, key=lambda x: x[0])
                    q["ask"], q["ask_sz"] = best[0], best[1]
                    q["ask_levels"] = sorted(asks)[:8]
                if bids:
                    best = max(bids, key=lambda x: x[0])
                    q["bid"], q["bid_sz"] = best[0], best[1]
                q["ts"] = ts or int(time.time() * 1000)
                self._touch(tok)
                return

            if et != "price_change":
                return
            for ch in (msg.get("price_changes") or []):
                tok = ch.get("asset_id")
                if not tok:
                    continue
                q = self.quote.setdefault(tok, {})
                bb, ba = _f(ch.get("best_bid")), _f(ch.get("best_ask"))
                if ba is not None:
                    q["ask"] = ba
                if bb is not None:
                    q["bid"] = bb
                q["ts"] = ts or int(time.time() * 1000)
                self._touch(tok)

    def _touch(self, tok):
        refs = self.token_index.get(tok)
        if not refs:
            return
        now = self.quote[tok].get("ts") or int(time.time() * 1000)
        for kind, key in refs:
            if kind == "bin":
                self._eval_binary(key, now, tok)
            else:
                self._eval_neg(key, now, tok)

    # ---------------- 判定 ----------------
    def _fresh(self, tok, now):
        q = self.quote.get(tok)
        if not q or q.get("ask") is None:
            return None
        if now - (q.get("ts") or 0) > FRESH_MS:
            return None
        return q

    def _close(self, key, end, reason):
        """结束一个 episode。陈旧导致的结束要按最后一次有效判定的时刻封口，
        否则行情一断，这个 episode 会永远挂在 open 里，汇总时算不出时长。"""
        ep = self.open.get(key)
        if ep is None:
            return
        ep["end"] = end
        ep["dur_ms"] = max(0, end - ep["start"])
        if ep["confirmed"] is not None:
            ep["conf_dur_ms"] = max(0, end - ep["confirmed"])
        ep["close_reason"] = reason
        self.closed.append(ep)
        del self.open[key]

    def _eval_binary(self, cid, now, tok):
        m = self.markets.get(cid)
        if not m:
            return
        key = f"bin:{cid}"
        self.evaluated.add(key)
        qy, qn = self._fresh(m["y"], now), self._fresh(m["n"], now)
        if not qy or not qn:
            ep = self.open.get(key)
            if ep is not None:
                self._close(key, ep.get("last_ok", ep["start"]), "stale")
            return
        self._judge(key, [qy["ask"], qn["ask"]],
                    [qy.get("ask_sz"), qn.get("ask_sz")], now, m.get("q", ""), tok)

    def _eval_neg(self, eid, now, tok):
        ev = self.neg_events.get(eid)
        if not ev:
            return
        key = f"neg:{eid}"
        asks, szs, mids = [], [], []
        for tok in ev["legs"]:
            q = self._fresh(tok, now)
            if not q:
                ep = self.open.get(key)
                if ep is not None:
                    self._close(key, ep.get("last_ok", ep["start"]), "stale")
                return
            asks.append(q["ask"])
            szs.append(q.get("ask_sz"))
            if q.get("bid") is not None:
                mids.append((q["bid"] + q["ask"]) / 2)
        # 完备性：Σ mid 明显低于 1 说明结果集缺腿（如 AFC 小组赛只列了 4 个队），
        # 买齐 YES 的期望赔付不是 $1，表观价差全是假的。REST 阶段踩过同一个坑。
        if len(mids) == len(ev["legs"]):
            smid = sum(mids)
            if not (COMPLETE_MID_LO <= smid <= COMPLETE_MID_HI):
                self.incomplete.add(eid)
                ep = self.open.get(key)
                if ep is not None:
                    self._close(key, ep.get("last_ok", ep["start"]), "incomplete")
                return
        self.evaluated.add(key)
        self._judge(key, asks, szs, now, ev.get("title", ""), tok)

    def _judge(self, key, asks, sizes, now, title, tok):
        s = sum(asks)
        prev = self.min_sum.get(key)
        if prev is None or s < prev:
            self.min_sum[key] = s
        ep = self.open.get(key)
        if s >= 1.0:
            if ep is not None:
                self._close(key, now, "spread")
            return
        # 毛价差 < 0：开或更新
        fee = sum(FEE_RATE * p * (1 - p) for p in asks)
        net = 1.0 - s - fee
        if ep is None:
            ep = {"key": key, "title": title, "start": now, "min_sum": s,
                  "max_net": net, "n_legs": len(asks), "updates": 0,
                  "confirmed": None, "legs": len(asks),
                  "depth_shares": min([z for z in sizes if z is not None], default=None),
                  "asks": [round(a, 4) for a in asks], "seen": {tok},
                  "last_ok": now}
            self.open[key] = ep
        else:
            ep["updates"] += 1
            ep["seen"].add(tok)
            ep["last_ok"] = now
            ep["min_sum"] = min(ep["min_sum"], s)
            ep["max_net"] = max(ep["max_net"], net)
            # 只有**每条腿都在价差存在期间刷新过**才算真机会：
            # 单腿刷新造成的 sum<1 是异步推送的拼接假象，实盘吃不到
            if ep["confirmed"] is None and len(ep["seen"]) >= ep["n_legs"]:
                ep["confirmed"] = now

    # ---------------- 汇总 ----------------
    def summary(self):
        with self.lock:
            eps = list(self.closed) + list(self.open.values())
            for e in eps:
                e.setdefault("dur_ms", None)
            return eps


def discover(max_events: int, max_tokens: int, threads: int = 6,
             neg_share: float = 0.6):
    """活跃市场 → 二元市场与 negRisk 完备集，收集要订阅的 token。

    token 预算必须**按类分配**：先塞满二元市场会把 negRisk 的腿全挤掉，
    上一轮就是这样漏掉了全部多腿完备集（输出 negRisk 0 个）。
    """
    u = ap.discover(max_events, threads=threads)
    markets, neg, bin_tokens, neg_tokens = {}, {}, [], []
    for m in u["bin"]:
        cid = m["condition_id"]
        markets[cid] = {"y": m["yes_token"], "n": m["no_token"],
                        "q": m.get("question", "")[:90], "liq": m.get("liquidity", 0)}
        bin_tokens += [m["yes_token"], m["no_token"]]
    # negRisk：discover 已把每条腿的 yes_token 摊平
    for ev in u.get("neg") or []:
        legs = [l["yes_token"] for l in (ev.get("legs") or []) if l.get("yes_token")]
        if 2 <= len(legs) <= 12:
            neg[str(ev["event_id"])] = {"legs": legs,
                                        "title": (ev.get("title") or "")[:90]}
            neg_tokens += legs
    bin_tokens = list(dict.fromkeys(bin_tokens))
    neg_tokens = list(dict.fromkeys(neg_tokens))
    n_neg = int(max_tokens * max(0.0, min(1.0, neg_share)))
    tokens = bin_tokens[:max_tokens - n_neg] + neg_tokens[:n_neg]
    return markets, neg, tokens


def main(argv=None) -> int:
    global FEE_RATE
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Polymarket WebSocket 毫秒级套利探测")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--max-events", type=int, default=200)
    p.add_argument("--max-tokens", type=int, default=1500)
    p.add_argument("--shard-size", type=int, default=350)
    p.add_argument("--neg-share", type=float, default=0.6,
                   help="token 预算给 negRisk 的比例（1.0 = 只测多腿完备集）")
    p.add_argument("--fee", type=float, default=FEE_RATE)
    args = p.parse_args(argv)
    FEE_RATE = args.fee

    print("发现活跃市场…", flush=True)
    markets, neg, tokens = discover(args.max_events, args.max_tokens,
                                    neg_share=args.neg_share)
    tokset = set(tokens)
    markets = {k: v for k, v in markets.items() if v["y"] in tokset or v["n"] in tokset}
    neg = {k: v for k, v in neg.items() if all(t in tokset for t in v["legs"])}
    print(f"  二元市场 {len(markets)}，negRisk 完备集 {len(neg)}，token {len(tokens)}")

    st = State()
    st.markets = markets
    st.neg_events = neg
    for cid, m in markets.items():
        st.token_index.setdefault(m["y"], []).append(("bin", cid))
        st.token_index.setdefault(m["n"], []).append(("bin", cid))
    for eid, ev in neg.items():
        for t in ev["legs"]:
            st.token_index.setdefault(t, []).append(("neg", eid))

    shards = [tokens[i:i + args.shard_size] for i in range(0, len(tokens), args.shard_size)]
    feeds = [Feed(s, st.on_msg, shard_id=i) for i, s in enumerate(shards)]
    print(f"  {len(feeds)} 条连接，每条约 {len(shards[0])} 个 token", flush=True)
    for f in feeds:
        threading.Thread(target=f.run, daemon=True).start()

    t0 = time.time()
    total = 0
    while time.time() - t0 < args.minutes * 60:
        time.sleep(15)
        phase = "预热" if time.time() - t0 < 30 else "采集"
        print(f"  [{phase}] {time.time()-t0:5.0f}s 消息 {sum(f.msgs for f in feeds)} "
              f"已结束机会 {len(st.closed)} 进行中 {len(st.open)}", flush=True)

    for f in feeds:
        f.stop = True
    time.sleep(1)
    elapsed = time.time() - t0
    eps = st.summary()
    msgs = sum(f.msgs for f in feeds)

    print("\n" + "=" * 74)
    print(f"运行 {elapsed:.0f}s，消息 {msgs}（{msgs/max(elapsed,1):.0f}/s），"
          f"book {sum(f.books for f in feeds)}，price_change {sum(f.pc for f in feeds)}")
    print(f"重连 {sum(f.reconnects for f in feeds)} 次")

    def bucket(ms):
        for lo, hi in DUR_BUCKETS:
            if lo <= ms < hi:
                return f"{lo/1000:g}-{hi/1000:g}s" if hi <= 60_000 else f">{lo/1000:g}s"
        return "?"

    # 毛价差为负的 episode 才叫"机会"，这里全部都是
    real = [e for e in eps if e["min_sum"] < 1.0]
    confirmed = [e for e in real if e.get("conf_dur_ms") is not None]
    ev = st.evaluated
    n_bin = len([k for k in ev if k.startswith("bin:")])
    n_neg = len([k for k in ev if k.startswith("neg:")])
    fresh_s = FRESH_MS // 1000
    print(f"\n因结果集缺腿被排除的 negRisk 事件：{len(st.incomplete)}/{len(neg)}")
    print(f"\n完成过完整判定的结构：二元 {n_bin}/{len(markets)}，"
          f"negRisk {n_neg}/{len(neg)}（多腿要求全部腿在 {fresh_s}s 内都新鲜）")
    print(f"\n出现过的 sum_ask < 1.000 事件：{len(real)} 个"
          f"（其中双腿各自刷新过、确认非拼接假象：{len(confirmed)} 个）")

    if real:
        dur = [e["dur_ms"] for e in real if e.get("dur_ms")]
        print(f"  存活时长：中位 {statistics.median(dur) if dur else 0:.0f}ms  "
              f"最长 {max(dur) if dur else 0:.0f}ms")
        c = collections.Counter(bucket(e["dur_ms"]) for e in real if e.get("dur_ms"))
        print("  分布：" + "  ".join(f"{k}:{v}" for k, v in c.items()))
        best = sorted(real, key=lambda e: e["min_sum"])[:8]
        print("\n  最小 sum_ask 前 8：")
        for e in best:
            print(f"    sum={e['min_sum']:.4f} 净={e['max_net']*1e4:+7.1f}bps "
                  f"dur={e.get('dur_ms') or 0:6.0f}ms legs={e['n_legs']} "
                  f"{e['title'][:44]}")

    conf = [e for e in confirmed if e.get("conf_dur_ms")]
    if conf:
        c2 = collections.Counter(bucket(e["conf_dur_ms"]) for e in conf)
        print("\n  确认机会的存活分布：" + "  ".join(f"{k}:{v}" for k, v in c2.items()))
        over3 = [e for e in conf if e["conf_dur_ms"] >= 3000]
        net_pos = [e for e in over3 if e["max_net"] > 0]
        print(f"  存活 >=3s 的 {len(over3)} 个，其中扣费后仍为正 {len(net_pos)} 个")

    # 近失分布：看清"差多少"
    print("\n各结构的最小 sum_ask 分位（越小越接近套利）：")
    vals = sorted(st.min_sum.values())
    if vals:
        for q in (0, 1, 5, 25, 50):
            i = min(len(vals) - 1, int(len(vals) * q / 100))
            print(f"  p{q:<3d} {vals[i]:.4f}")
        print(f"  理论保本线（p=0.5 双腿 fee 1%）：sum_ask <= 0.9950")
        print(f"  最小报价单位 0.001 → 一个 tick 的价差只有 10bps，不够付两次费")

    out = ap.DATA_DIR / f"ws_{int(time.time())}.json"
    out.write_text(json.dumps({
        "generated_utc": int(time.time() * 1000), "seconds": round(elapsed, 1),
        "messages": msgs, "markets": len(markets), "neg_events": len(neg),
        "tokens": len(tokens), "fee_rate": args.fee,
        "episodes": [{k: (len(v) if isinstance(v, set) else v) for k, v in e.items()}
                     for e in sorted(real, key=lambda e: e["min_sum"])[:200]],
        "min_sum_percentiles": {f"p{q}": vals[min(len(vals)-1, int(len(vals)*q/100))]
                                for q in (0, 1, 5, 25, 50)} if vals else {},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
