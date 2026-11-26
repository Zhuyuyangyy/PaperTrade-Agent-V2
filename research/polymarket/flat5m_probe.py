#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1 终局测试：BTC 5 分钟平值市场（`btc-updown-5m-*`）双路 WS 延迟。

为什么要单独做这个市场
----------------------
`latency_probe.py` 测的是"above $X"障碍型市场，5bps 现货变动对应的公允概率
变化 < 1 tick，盘口根本不该动，所以量到的 14ms 与安慰剂同值，判定为 churn。
5 分钟平值市场不同：窗口内还有 Gamma，越小的时间窗，同样的现货变动对应
越大的概率跳变。这是低延迟 Crypto 线唯一还有理论空间的地方。

结算基准（必须先钉死，否则口径全错）
------------------------------------
Chainlink BTC/USD **TWAP-60s** data stream，不是币安现货。
`feeType=crypto_btc_5m_15m_fees`，`takerBaseRate=0.05`，
`fee = 份数 × 0.05 × p × (1-p)` 每股 → p=0.50 时是名义的 2.5%（最贵档）。
maker 永远 0。

三道口径（写进代码，不靠自觉）
------------------------------
1. **只统计方向性反应**：现货在 `--ref-ms` 内跳动 >= `--jump-bps` 且
   同向 token 的报价在 `--horizon-ms` 内同向走 >= 2 ticks 才算"盘口响应"。
   不满足的记录为 no-response，单独报占比——这才是"滞后"的真实分母。
2. **安慰剂对照**：同一 token，把 t0 前移 5s，量"到下一次 >=2 tick 同向变动"
   的距离。若与真实 t0 分布无差别，说明只是 churn。
3. **深度用 ladder 累加**，不用 best-ask size 冒充——best 档几百份不代表能吃。

三类数字
--------
- 延迟：t1(报价同向 2 ticks)、t2(旧卖一档被吃/撤)、残留深度
- 净利：**stale-ask 回放**——t0 瞬间按可见 ladder 吃到 $cap，持到结算，
  扣真实 fee。这是"能不能赚钱"的直接回答，不经任何公允价建模。
- 分档：前 4 分钟 / 最后 60 秒，Gamma 完全不同，必须分开报。

用法
----
    python research/polymarket/flat5m_probe.py --minutes 30
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import math
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

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BIN_WS = "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade"
WINDOW_S = 300
TICK = 0.01
FEE_RATE = 0.05          # crypto_btc_5m_15m_fees 的 taker 档，已由 fee-rate 接口 base_fee=1000 佐证


# ---------------------------------------------------------------- 发现

def window_ts(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(now) - int(now) % WINDOW_S


def fetch_window(ts: int, retries: int = 3):
    """按 slug 取一个 5m 窗口。slug 完全可算法化，不需要搜索接口。"""
    slug = f"btc-updown-5m-{ts}"
    for i in range(retries):
        try:
            d = ap.http_json(f"{ap.GAMMA}/events?slug={slug}", timeout=15)
        except ap.ProbeError:
            if i == retries - 1:
                return None
            time.sleep(0.4 * (i + 1))
            continue
        if not d:
            return None
        e = d[0]
        ms = e.get("markets") or []
        if not ms:
            return None
        m = ms[0]
        toks = ap._tokens(m)
        if len(toks) != 2:
            return None
        return {"ts": ts, "slug": slug, "title": e.get("title") or "",
                "condition_id": m.get("conditionId"),
                "up": toks[0], "down": toks[1],
                "accepting": bool(m.get("acceptingOrders")),
                "closed": bool(m.get("closed")),
                "start": ts, "end": ts + WINDOW_S,
                "volume": ap._f(m.get("volume")) or 0.0}
    return None


def resolution(ts: int):
    """窗口收盘后取结算：返回 'up' / 'down' / None。"""
    d = fetch_window(ts)
    if not d:
        return None
    try:
        e = ap.http_json(f"{ap.GAMMA}/events?slug={d['slug']}", timeout=15)[0]
        m = (e.get("markets") or [{}])[0]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not prices or not m.get("closed"):
            return None
        vals = [ap._f(x) for x in prices]
        if vals[0] is None:
            return None
        return "up" if vals[0] >= 0.5 else "down"
    except Exception:
        return None


# ---------------------------------------------------------------- 账本

class Ladder:
    """维护单 token 的完整双边 ladder，才能算真实可吃美元深度。

    `book` 给全量快照；`price_change` 给单个价位的增量（size=0 表示撤单）。
    """

    __slots__ = ("asks", "bids", "ts", "exch", "n")

    def __init__(self):
        self.asks: dict[float, float] = {}
        self.bids: dict[float, float] = {}
        self.ts = 0
        self.exch = 0
        self.n = 0

    def apply_book(self, msg, now, exch):
        self.asks.clear()
        self.bids.clear()
        for l in (msg.get("asks") or []):
            p, s = ap._f(l.get("price")), ap._f(l.get("size"))
            if p is not None and s and s > 0:
                self.asks[p] = s
        for l in (msg.get("bids") or []):
            p, s = ap._f(l.get("price")), ap._f(l.get("size"))
            if p is not None and s and s > 0:
                self.bids[p] = s
        self.ts, self.exch, self.n = now, exch, self.n + 1

    def apply_change(self, price, size, side, now, exch, best_bid=None, best_ask=None):
        p, s = ap._f(price), ap._f(size)
        if p is not None:
            book = self.asks if (side or "").upper() == "SELL" else self.bids
            if s is None or s <= 0:
                book.pop(p, None)
            else:
                book[p] = s
        self.ts, self.exch, self.n = now, exch, self.n + 1

    @property
    def best_ask(self):
        return min(self.asks) if self.asks else None

    @property
    def best_bid(self):
        return max(self.bids) if self.bids else None

    def ask_depth(self, cap_usd: float, max_ticks: int | None = None):
        """从最优卖一往上逐档吃，返回 (份数, 成本, 最高吃到价, 档数)。

        只有这个数字才是"我能吃到多少"，best-ask size 会严重高估也严重低估。
        """
        if not self.asks:
            return 0.0, 0.0, None, 0
        best = min(self.asks)
        shares = cost = 0.0
        top = None
        k = 0
        for p in sorted(self.asks):
            if max_ticks is not None and round((p - best) / TICK) > max_ticks:
                break
            sz = self.asks[p]
            room = (cap_usd - cost) / p
            if room <= 1e-9:
                break
            take = min(sz, room)
            if take <= 1e-9:
                break
            shares += take
            cost += take * p
            top = p
            k += 1
            if cost >= cap_usd - 1e-9:
                break
        return shares, cost, top, k

    def snap(self):
        if not self.asks and not self.bids:
            return None
        return {"best_ask": self.best_ask, "best_bid": self.best_bid,
                "asks": sorted((round(p, 4), round(s, 2)) for p, s in self.asks.items())[:12],
                "bids": sorted(((round(p, 4), round(s, 2)) for p, s in self.bids.items()),
                               reverse=True)[:12]}


class Probe:
    def __init__(self, jump_bps: float, ref_ms: int, horizon_ms: int, cooldown_ms: int):
        self.lock = threading.Lock()
        self.books: dict[str, Ladder] = {}
        self.windows: dict[int, dict] = {}       # ts -> window meta
        self.token_win: dict[str, tuple[int, str]] = {}   # token -> (ts, 'up'/'down')
        self.spot: collections.deque = collections.deque()   # (ts_ms, px)
        self.events: list[dict] = []
        self.updates: dict[str, list] = collections.defaultdict(list)
        self.msgs = collections.Counter()
        self.feed_lag = {"bin": [], "poly": []}
        self.jump_bps = jump_bps
        self.ref_ms = ref_ms
        self.horizon_ms = horizon_ms
        self.cooldown_ms = cooldown_ms
        self.last_emit = {}
        self.version = 0

    # ---------- 订阅管理 ----------
    def tokens(self):
        with self.lock:
            return list(self.token_win)

    def add_window(self, w):
        with self.lock:
            self.windows[w["ts"]] = w
            self.token_win[w["up"]] = (w["ts"], "up")
            self.token_win[w["down"]] = (w["ts"], "down")
            self.version += 1

    def drop_before(self, ts):
        with self.lock:
            gone = [t for t, (w, _) in self.token_win.items() if w < ts]
            for t in gone:
                self.token_win.pop(t, None)
                self.books.pop(t, None)
            if gone:
                self.version += 1

    # ---------- Binance ----------
    def on_binance(self, msg, now_ms):
        d = msg.get("data") or msg
        if d.get("e") != "aggTrade":
            return
        px = ap._f(d.get("p"))
        if px is None:
            return
        exch = int(d.get("T") or d.get("E") or 0)
        self.msgs["bin"] += 1
        if exch:
            self.feed_lag["bin"].append(now_ms - exch)
        with self.lock:
            self.spot.append((now_ms, px))
            while self.spot and now_ms - self.spot[0][0] > 60_000:
                self.spot.popleft()
            self._check_triggers(now_ms, px)

    def _ref_price(self, now_ms, px):
        """`ref_ms` 之前最近的一笔价格；不足则取 deque 里最老的。窗口太短就不触发。"""
        target = now_ms - self.ref_ms
        if not self.spot:
            return None
        if self.spot[0][0] > target:
            return None          # 历史不够，宁可不触发
        best = None
        for ts, p in reversed(self.spot):
            if ts <= target:
                best = (ts, p)
                break
        return best[1] if best else None

    def _check_triggers(self, now_ms, px):
        ref = self._ref_price(now_ms, px)
        if not ref or ref <= 0:
            return
        move = (px / ref - 1) * 1e4
        if abs(move) < self.jump_bps:
            return
        if now_ms - self.last_emit.get("jump", 0) < self.cooldown_ms:
            return
        self.last_emit["jump"] = now_ms
        side = "up" if move > 0 else "down"
        # 快照所有活跃 token 的 ladder，供 t2 / 深度 / 回放使用
        snap = {t: (self.books[t].snap() if t in self.books else None)
                for t in self.token_win}
        ev = {"kind": "jump", "t0": now_ms, "px": px, "ref_px": ref,
              "move_bps": round(move, 2), "side": side,
              "win": None, "snap": snap}
        # 归属：优先当前窗口（t0 落在窗口内且该窗口尚未收盘）
        for ts, w in self.windows.items():
            if w["start"] * 1000 <= now_ms < w["end"] * 1000:
                ev["win"] = ts
                break
        if ev["win"] is None:
            return
        ev["phase"] = ("last60" if now_ms >= (ev["win"] + WINDOW_S) * 1000 - 60_000
                       else "first4m")
        ev["t_in_win_ms"] = now_ms - ev["win"] * 1000
        self.events.append(ev)

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
                if not tok or tok not in self.token_win:
                    return
                lb = self.books.setdefault(tok, Ladder())
                lb.apply_book(msg, now_ms, exch)
                self._touch(tok, now_ms, lb)
                return
            if et != "price_change":
                return
            for ch in (msg.get("price_changes") or []):
                tok = ch.get("asset_id")
                if not tok or tok not in self.token_win:
                    continue
                lb = self.books.setdefault(tok, Ladder())
                lb.apply_change(ch.get("price"), ch.get("size"), ch.get("side"),
                                now_ms, exch, ch.get("best_bid"), ch.get("best_ask"))
                self._touch(tok, now_ms, lb)

    def _touch(self, tok, now_ms, lb):
        """只在 **2 tick 网格上一旦变了** 才记一条，把 churn 挡在测量之外。

        预检里 9514 条推送只有 4 个唯一 ask 值，若按原始流算"下一次更新"，
        t1 会恒等于 0——这正是上一轮得出 14ms=14ms 的原因。
        """
        a, b = lb.best_ask, lb.best_bid
        hist = self.updates[tok]
        if hist:
            _ts, pa, pb = hist[-1]
            if pa == a and pb == b:
                return
        hist.append((now_ms, a, b))

    # ---------- 汇总 ----------
    def finish(self, cap_usd: float, skip_before: int | None = None):
        """事件级测量。

        方向语义（必须钉死，写错过一次）
        --------------------------------
        现货上涨 ⇒ "up" token 变贵（ask **升**）、"down" token 变便宜（ask **降**）。
        所以对受益 token（B=w[side]），机会是"它的 ask 还停在旧价"，机会消失 =
        B 的 ask 上行。对对手 token（O），确认信号是它的 ask 下行。

        判定一个事件是"真重定价"还是"churn"，看 B 上行 **且** O 下行。
        """
        with self.lock:
            events = list(self.events)
            updates = {k: list(v) for k, v in self.updates.items()}
            windows = dict(self.windows)
        res = {ts: resolution(ts) for ts in windows}

        out = {"windows": {}, "events": events}
        for ts, w in windows.items():
            out["windows"][ts] = {"title": w["title"], "res": res.get(ts),
                                  "volume": w["volume"]}

        def first_at(hist, tsl, i0, t_anchor, pred, limit_ms):
            """从 i0 之后找第一个满足 pred 的时刻，返回绝对毫秒或 None。"""
            for j in range(i0 + 1, len(hist)):
                t, a, _b = hist[j]
                if t - t_anchor > limit_ms:
                    return None
                if a is not None and pred(a):
                    return t
            return None

        rows = []
        for ev in events:
            ts = ev["win"]
            w = windows.get(ts)
            if not w:
                continue
            if skip_before is not None and ts < skip_before:
                continue
            side = ev["side"]
            opp = "down" if side == "up" else "up"
            tok_b, tok_o = w[side], w[opp]
            hist, hist_o = updates.get(tok_b) or [], updates.get(tok_o) or []
            if len(hist) < 2:
                continue
            tsl = [h[0] for h in hist]
            i0 = bisect.bisect_right(tsl, ev["t0"]) - 1
            if i0 < 0:
                continue
            ref_ask = hist[i0][1]
            if ref_ask is None:
                continue
            # 只在还有 tick 空间时才算：ask >= 0.98 时"涨 2 ticks"不可能
            need = min(2, round((1.0 - ref_ask) / TICK))
            if need < 1:
                continue

            # t_change：B 的 ask 第一次变化（退出旧价 = 我只剩这么长的窗口）
            t_change = first_at(hist, tsl, i0, ev["t0"],
                                lambda a: abs(a - ref_ask) > 1e-9, self.horizon_ms)
            # t_up：B 的 ask 上行 >= need ticks（重定价走完）
            t_up = first_at(hist, tsl, i0, ev["t0"],
                            lambda a, tg=ref_ask + need * TICK - 1e-9: a >= tg,
                            self.horizon_ms)
            # t_opp：对手 ask 下行 >= 1 tick（方向确认）
            t_opp = None
            if len(hist_o) >= 2:
                tsl_o = [h[0] for h in hist_o]
                io = bisect.bisect_right(tsl_o, ev["t0"]) - 1
                if io >= 0 and hist_o[io][1] is not None:
                    ref_o = hist_o[io][1]
                    t_opp = first_at(hist_o, tsl_o, io, ev["t0"],
                                     lambda a, r=ref_o: a <= r - TICK + 1e-9,
                                     self.horizon_ms)

            # ---- 安慰剂：同一 token，锚点前移 5s，量到其 ask 第一次变化的距离 ----
            pl = None
            p0 = ev["t0"] - 5000
            ip = bisect.bisect_right(tsl, p0) - 1
            if ip >= 0 and hist[ip][1] is not None:
                pr = hist[ip][1]
                t = first_at(hist, tsl, ip, p0,
                             lambda a, r=pr: abs(a - r) > 1e-9, self.horizon_ms)
                pl = (t - p0) if t else None

            snap = (ev.get("snap") or {}).get(tok_b)
            replay = None
            depth_usd = 0.0
            if snap and snap.get("asks"):
                lb = Ladder()
                lb.asks = {p: s for p, s in snap["asks"]}
                shares, cost, top, lv = lb.ask_depth(cap_usd)
                if shares > 0:
                    avg = cost / shares
                    fee = shares * FEE_RATE * avg * (1 - avg)
                    won = res.get(ts) == side
                    payoff = shares if won else 0.0
                    replay = {"shares": shares, "cost": cost, "fee": fee,
                              "avg": avg, "top": top, "levels": lv,
                              "won": won, "pnl": payoff - cost - fee,
                              "ret_bps": (payoff - cost - fee) / cost * 1e4}
                    depth_usd = cost

            rows.append({
                "t0": ev["t0"], "win": ts, "phase": ev["phase"], "side": side,
                "move_bps": ev["move_bps"], "ref_ask": ref_ask, "need_ticks": need,
                "t_change": (t_change - ev["t0"]) if t_change else None,
                "t_up": (t_up - ev["t0"]) if t_up else None,
                "t_opp": (t_opp - ev["t0"]) if t_opp else None,
                "placebo": pl, "replay": replay, "depth_usd": depth_usd,
                "book": snap,
            })
        out["rows"] = rows
        return out


# ---------------------------------------------------------------- 连接

class PolyFeed:
    def __init__(self, probe: Probe):
        self.p = probe
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
            toks = self.p.tokens()
            if not toks:
                time.sleep(0.3)
                continue
            ver = self.p.version
            try:
                self.ws = websocket.create_connection(
                    POLY_WS, timeout=20, http_proxy_host=None, http_no_proxy=["*"],
                    enable_multithread=True)
                self.ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                while not self.stop:
                    if self.p.version != ver:
                        break            # 窗口滚动 → 重新订阅
                    self.ws.settimeout(1.0)
                    try:
                        raw = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw or raw.strip().upper() == "PONG":
                        continue
                    now = int(time.time() * 1000)
                    d = json.loads(raw)
                    for it in (d if isinstance(d, list) else [d]):
                        self.p.on_poly(it, now)
            except Exception:
                if self.stop:
                    break
                time.sleep(1.5)
            finally:
                try:
                    self.ws and self.ws.close()
                except Exception:
                    pass


class BinanceFeed:
    def __init__(self, probe: Probe):
        self.p = probe
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                ws = websocket.create_connection(BIN_WS, timeout=20,
                                                 http_proxy_host=None, http_no_proxy=["*"])
                while not self.stop:
                    ws.settimeout(5)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        continue
                    self.p.on_binance(json.loads(raw), int(time.time() * 1000))
            except Exception:
                if self.stop:
                    break
                time.sleep(1.5)


def discover(p: Probe, horizon_windows: int = 3):
    """滚动发现：当前窗口 + 后续 horizon 个，丢掉更早的。返回最新窗口起点。"""
    cur = window_ts()
    p.drop_before(cur)
    for k in range(horizon_windows):
        ts = cur + k * WINDOW_S
        if ts in p.windows:
            continue
        w = fetch_window(ts)
        if w and w["accepting"] and not w["closed"]:
            p.add_window(w)
    return cur


# ---------------------------------------------------------------- 统计

def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q / 100))]


def boot_ci(vals, reps=4000, q=(2.5, 97.5)):
    """按窗口做整簇重采样的 bootstrap——同一窗口内的事件高度相关，
    逐笔独立重采样会把 CI 压得过窄。"""
    if len(vals) < 3:
        return None
    rng = __import__("random").Random(20261125)
    n = len(vals)
    means = []
    for _ in range(reps):
        s = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(sum(s) / n)
    means.sort()
    return means[int(reps * q[0] / 100)], means[int(reps * q[1] / 100)]


def summarize(out, cap_usd: float, n_windows: int):
    rows = out["rows"]
    wins = out["windows"]
    print("\n" + "=" * 78)
    print(f"覆盖窗口 {len(wins)} 个（计划 {n_windows}）")
    for ts in sorted(wins):
        w = wins[ts]
        print(f"  {ts}  {w['title'][:46]:<46} 结算 {str(w['res']):<5} vol {w['volume']:.0f}")
    if not rows:
        print("\n没有可用事件——检查 --jump-bps 是否过高，或网络是否通")
        return

    print(f"\n事件 {len(rows)} 个（现货 jump >= 门槛，已在 2 tick 网格上滤掉 churn）")
    for phase in ("first4m", "last60"):
        sub = [r for r in rows if r["phase"] == phase]
        if not sub:
            continue
        print(f"\n--- {phase}  n={len(sub)} ---")
        tch = [r["t_change"] for r in sub if r["t_change"] is not None]
        tup = [r["t_up"] for r in sub if r["t_up"] is not None]
        top = [r["t_opp"] for r in sub if r["t_opp"] is not None]
        pls = [r["placebo"] for r in sub if r["placebo"] is not None]
        print(f"  B 退出旧 ask：{len(tch)}/{len(sub)} ({len(tch)/len(sub)*100:.0f}%)"
              + (f"  p25 {pct(tch,25)}ms p50 {pct(tch,50)}ms p90 {pct(tch,90)}ms"
                 if tch else ""))
        print(f"  B 上行 >= 2 ticks：{len(tup)}/{len(sub)} ({len(tup)/len(sub)*100:.0f}%)"
              + (f"  p25 {pct(tup,25)}ms p50 {pct(tup,50)}ms p90 {pct(tup,90)}ms"
                 if tup else ""))
        print(f"  对手 ask 下行 >= 1 tick：{len(top)}/{len(sub)} ({len(top)/len(sub)*100:.0f}%)"
              + (f"  p25 {pct(top,25)}ms p50 {pct(top,50)}ms p90 {pct(top,90)}ms"
                 if top else ""))
        if pls:
            print(f"  安慰剂（锚点前移 5s，到 ask 第一次变化）p50 {pct(pls,50)}ms"
                  f"  n={len(pls)}")

    # ---- stale-ask 回放 ----
    print(f"\n{'='*78}\nstale-ask 回放（每笔吃满 ${cap_usd:.0f}，扣真实 fee 0.05×p×(1-p)）")
    for phase in ("first4m", "last60", None):
        sub = [r["replay"] for r in rows
               if r.get("replay") and (phase is None or r["phase"] == phase)]
        if not sub:
            continue
        tag = phase or "合计"
        wr = sum(1 for x in sub if x["won"]) / len(sub)
        tot_cost = sum(x["cost"] for x in sub)
        tot_pnl = sum(x["pnl"] for x in sub)
        rets = [x["ret_bps"] for x in sub]
        print(f"\n  [{tag}] n={len(sub)}  胜率 {wr*100:.1f}%  "
              f"总投入 ${tot_cost:,.0f}  总净 ${tot_pnl:+,.2f}  "
              f"净收益率 {tot_pnl/tot_cost*1e4:+.0f}bps")
        ci = boot_ci(rets)
        if ci:
            print(f"    单笔 p50 {statistics.median(rets):+.0f}bps  "
                  f"均值 {statistics.mean(rets):+.0f}bps  "
                  f"bootstrap 95%CI [{ci[0]:+.0f}, {ci[1]:+.0f}]bps")
        print(f"    单笔均费 ${statistics.mean([x['fee'] for x in sub]):.3f}"
              f"（占成本 {statistics.mean([x['fee']/x['cost'] for x in sub])*1e4:.0f}bps）")
        print(f"    平均 {statistics.mean([x['levels'] for x in sub]):.1f} 档，"
              f"平均成本价 {statistics.mean([x['avg'] for x in sub]):.4f}")

    # ---- 硬门槛判定 ----
    print(f"\n{'='*78}\n硬门槛判定（用户设定）")
    rep_all = [r["replay"] for r in rows if r.get("replay")]
    alltup = [r["t_up"] for r in rows if r["t_up"] is not None]
    med_up = pct(alltup, 50)
    net_bps = (sum(x["pnl"] for x in rep_all) / sum(x["cost"] for x in rep_all) * 1e4
               if rep_all else None)
    med_depth = statistics.median([r["depth_usd"] for r in rows if r["depth_usd"] > 0]) \
        if any(r["depth_usd"] > 0 for r in rows) else None
    ok1 = med_up is not None and med_up >= 300
    ok2 = net_bps is not None and net_bps > 0
    ok3 = med_depth is not None and med_depth >= 200
    print(f"  1) p50(B 上行 2 ticks 耗时) = "
          f"{f'{med_up}ms' if med_up is not None else 'NA'}  （<300ms 判死）→ "
          f"{'** 死 **' if med_up is not None and med_up < 300 else ('过' if med_up is not None else 'NA')}")
    print(f"  2) 扣费净利 = {f'{net_bps:+.0f}bps' if net_bps is not None else 'NA'}"
          f"  （<=0 判死）→ "
          f"{'** 死 **' if net_bps is not None and net_bps <= 0 else ('过' if net_bps is not None else 'NA')}")
    print(f"  3) 可吃深度中位 = {f'${med_depth:,.0f}' if med_depth is not None else 'NA'}"
          f"  （<$200 判死）→ "
          f"{'** 死 **' if med_depth is not None and med_depth < 200 else ('过' if med_depth is not None else 'NA')}")
    verdict = (med_up is not None and med_up < 300) or \
              (net_bps is not None and net_bps <= 0) or \
              (med_depth is not None and med_depth < 200)
    print(f"\n  结论：{'低延迟 Crypto 证伪 → 切入 Step 2 气象' if verdict else '未触发判死线，需扩样'}")
    return {"med_t_up_ms": med_up, "net_bps": net_bps,
            "med_depth_usd": med_depth, "killed": bool(verdict)}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="BTC 5m 平值市场双路 WS 延迟终局测试")
    p.add_argument("--minutes", type=float, default=30.0)
    p.add_argument("--jump-bps", type=float, default=5.0)
    p.add_argument("--ref-ms", type=int, default=5000,
                   help="现货变动的参照窗口（默认 5s：1s 内 5bps 是 5.5σ，几乎打不出事件）")
    p.add_argument("--horizon-ms", type=int, default=30_000)
    p.add_argument("--cooldown-ms", type=int, default=3000)
    p.add_argument("--cap", type=float, default=200.0, help="回放每笔吃到的美元上限")
    args = p.parse_args(argv)

    # 不做窗口对齐等待：等待会白烧时间且容易被外层 timeout 杀掉。
    # 起点取当前窗口，跑满 n 个完整窗口后停在边界上。
    probe = Probe(args.jump_bps, args.ref_ms, args.horizon_ms, args.cooldown_ms)
    cur = discover(probe)
    n_win = max(1, int(round(args.minutes * 60 / WINDOW_S)))
    print(f"起点窗口 {cur}，计划跑 {n_win} 个完整窗口（{args.minutes:.0f} 分钟）", flush=True)
    for ts in sorted(probe.windows):
        w = probe.windows[ts]
        print(f"  订阅 {ts}  {w['title'][:50]}", flush=True)

    threading.Thread(target=PolyFeed(probe).run, daemon=True).start()
    threading.Thread(target=BinanceFeed(probe).run, daemon=True).start()

    t_start = time.time()
    end_at = cur + n_win * WINDOW_S + 10      # 收满窗口后再给 10s 让尾单推送落地
    last_cur = cur
    while time.time() < end_at:
        time.sleep(5)
        c = window_ts()
        if c != last_cur:
            last_cur = c
            discover(probe)
            live = [ts for ts in probe.windows if ts >= c]
            print(f"  [{time.strftime('%H:%M:%S')}] 滚动到 {c}，活跃窗口 {live}，"
                  f"事件 {len(probe.events)}，bin {probe.msgs['bin']} poly {probe.msgs['poly']}",
                  flush=True)

    print("\n录制结束，等待最后窗口结算…", flush=True)
    # 只等 **已收盘** 的窗口（end <= now），别等仍在进行中的那个，
    # 否则 resolution() 永远返回 None，循环空转到超时。
    #
    # 硬上限 150s：这段等待只是给账本补上"赢没赢"的标签，不影响已经录到的
    # 推送和事件。之前有过一次教训——等待期间被外部 kill，643,205 条推送
    # 随进程一起蒸发，一个字节都没落盘。所以必须：
    #   1) 有硬截止，绝不超过 150s；
    #   2) 先把原始数据落盘，再做任何可能超时的网络动作。
    deadline = time.time() + 150
    while time.time() < deadline:
        closed = [ts for ts in probe.windows if ts + WINDOW_S < int(time.time())]
        if closed and all(resolution(ts) is not None for ts in closed):
            break
        time.sleep(4)

    # ---- 先落盘，再算报告 ----
    # 顺序很关键：finish()/summarize() 万一抛异常，原始数据已经安全。
    out = probe.finish(args.cap, skip_before=cur)
    raw_path = ap.DATA_DIR / f"flat5m_raw_{int(time.time())}.json"
    try:
        raw_path.write_text(json.dumps({
            "generated_utc": int(time.time() * 1000),
            "params": vars(args),
            "seconds": round(time.time() - t_start, 1),
            "msgs": dict(probe.msgs),
            "feed_lag_p50_ms": {"binance": pct(probe.feed_lag["bin"], 50),
                                "poly": pct(probe.feed_lag["poly"], 50)},
            "windows": sorted(probe.windows),
            "events": out if isinstance(out, list) else [],
        }, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"原始数据已落盘 {raw_path}", flush=True)
    except Exception as e:
        print(f"落盘失败（不影响后续报告）：{type(e).__name__} {e}", flush=True)

    verdict = summarize(out, args.cap, n_win)

    path = ap.DATA_DIR / f"flat5m_{int(time.time())}.json"
    path.write_text(json.dumps({
        "generated_utc": int(time.time() * 1000),
        "params": vars(args),
        "seconds": round(time.time() - t_start, 1),
        "msgs": dict(probe.msgs),
        "feed_lag_p50_ms": {"binance": pct(probe.feed_lag["bin"], 50),
                            "poly": pct(probe.feed_lag["poly"], 50)},
        "verdict": verdict,
        **out,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())