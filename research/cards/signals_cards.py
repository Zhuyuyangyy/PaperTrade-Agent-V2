"""30 张卡的信号函数实现。

每张卡是一个**确定性函数**，输入 bar 下标与上下文，输出 `signal dict`。
LLM 完全不参与——这是 Phase 3 的机械基线。Phase 4 才在同一个信号上
叠加 LLM 过滤，两者共享同一套执行与成本口径，才有可比性。

统一约定：
  · 信号在 bar i 收盘确认，成交在 i+1 开盘（引擎负责）
  · stop 用结构位或 ATR 倍数，target 默认 2R（不用「随便设个 2:1」，
    而是必须能被 `invalidates_if` 里的结构失效点解释）
  · `limit=None` 表示市价追入，否则是回踩挂单

**为什么 target 要显式给**：只给止损不给目标，回测就无法计算固定 R 倍数
的期望值，只能靠 time exit 平仓，结果会强烈依赖 `max_hold_bars` 这个
人为参数。给 2R 至少让「平均 R」这个指标有意义。
"""

from __future__ import annotations

import structure as S


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _atr(cs, i, n=14):
    return S.atr(cs, i, n)


def _ctx_atr(sig, cs, i, n=14):
    sig["atr"] = _atr(cs, i, n)
    return sig


def _rr(side, price, stop, r_mult=2.0):
    """按 R 倍数算目标价。"""
    risk = abs(price - stop)
    if risk <= 0:
        return None
    return price + r_mult * risk if side == "long" else price - r_mult * risk


class Cache:
    """每轮回测只算一次结构，避免 signal_fn 里重复计算。

    这是必需的——`structure_events` 在 5.4 万根上是 O(n)，如果每根 bar
    都调一次就是 O(n²)，单币回测要跑几小时。

    **索引必须挂在实例上，不能放模块级 dict 用 id(cache) 做键。**
    CPython 在对象被回收后会复用 id，跑多币时第二个 Cache 可能拿到第一个
    的 id，于是 `_IDX_CACHE[id(cache)]` 命中的是上一个币的时间索引。
    症状极其诡异：单独跑某张卡有几百笔成交，放进 runner 跑多币就全是
    零信号——而这正是第一版踩的坑。
    """

    def __init__(self, candles, left=5, right=5, min_gap_atr=0.5):
        self.candles = candles
        self.pivots = S.pivots(candles, left, right)
        self.events = S.structure_events(candles, self.pivots)
        self.fvgs = S.find_fvgs(candles, min_gap_atr=min_gap_atr)
        self.obs = S.find_order_blocks(candles)
        self.sweeps = S.find_sweeps(candles, self.pivots)
        self.ranges = S.find_ranges(candles)

        # 按 bar 下标建索引，供 O(1) 查询「bar i 时刻已知的事件」
        self.ev_at: dict[int, list] = {}
        for e in self.events:
            self.ev_at.setdefault(e.t, []).append(e)

        self.piv_at: dict[int, list] = {}
        for p in self.pivots:
            self.piv_at.setdefault(p.confirmed_at, []).append(p)

        self._t2i: dict[int, int] | None = None
        self._piv_by_i: dict[int, S.Pivot] | None = None

    # ---- 惰性索引，挂在实例上，跟着 Cache 一起死 ----

    def idx(self, t: int) -> int:
        """时间戳 → bar 下标。找不到返回 -1。"""
        if self._t2i is None:
            self._t2i = {c["t"]: k for k, c in enumerate(self.candles)}
        return self._t2i.get(t, -1)

    def pivot_by_i(self, pi: int) -> S.Pivot | None:
        if self._piv_by_i is None:
            self._piv_by_i = {p.i: p for p in self.pivots}
        return self._piv_by_i.get(pi)


# ---------------------------------------------------------------------------
# S01 BOS Continuation
# ---------------------------------------------------------------------------

def s01_bos_continuation(cache: Cache, confirm_bars=2, retest_max_bars=20,
                         r_mult=2.0):
    """BOS 之后回抽至结构位（被突破的 pivot），同向再确认则入场。

    这个「再确认」用的是**收盘重新站上/站下结构位**，不是简单的触价——
    触价就走等于没有任何过滤，跟直接追突破没区别。
    """
    cs = cache.candles
    pending: dict | None = None

    def fn(i, ctx):
        nonlocal pending
        # 新 BOS 出现 → 记下等待回抽
        for e in cache.ev_at.get(i, ()):
            if e.kind in ("BOS", "CHoCH"):
                pending = {"e": e, "at": i}

        if pending is None:
            return None
        e, started = pending["e"], pending["at"]
        if i - started > retest_max_bars:
            pending = None
            return None
        if i - started < confirm_bars:
            return None

        bar = cs[i]
        av = _atr(cs, i)
        if not av:
            return None

        if e.direction == "bull":
            # 回抽到突破位附近（0.3 ATR 容差），且收盘在其上方
            if bar["l"] <= e.level + 0.3 * av and bar["c"] > e.level:
                stop = e.level - 1.0 * av
                price = bar["c"]
                if price - stop < 0.15 * av:
                    return None
                pending = None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        else:
            if bar["h"] >= e.level - 0.3 * av and bar["c"] < e.level:
                stop = e.level + 1.0 * av
                price = bar["c"]
                if stop - price < 0.15 * av:
                    return None
                pending = None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S02 CHoCH Reversal
# ---------------------------------------------------------------------------

def s02_choch_reversal(cache: Cache, retest_max_bars=20, r_mult=2.0):
    """CHoCH 之后的反转入场：CHoCH 本身就是方向改变信号，直接顺新方向。"""
    cs = cache.candles

    def fn(i, ctx):
        av = _atr(cs, i)
        if not av:
            return None
        for e in cache.ev_at.get(i, ()):
            if e.kind != "CHoCH":
                continue
            bar = cs[i]
            if e.direction == "bull":
                stop = min(bar["l"], e.level) - 0.5 * av
                price = bar["c"]
                if price - stop < 0.15 * av:
                    continue
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
            else:
                stop = max(bar["h"], e.level) + 0.5 * av
                price = bar["c"]
                if stop - price < 0.15 * av:
                    continue
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S03 FVG Retest
# ---------------------------------------------------------------------------

def s03_fvg_retest(cache: Cache, entry_at="edge", retest_max_bars=40,
                   touch_atr=0.1, r_mult=2.0):
    """FVG 回踩。edge=缺口近端（缺口靠价格一侧），mid=缺口中点。

    只在**回踩时**入场，不追缺口方向。这是 S03 与「追突破」的核心差异，
    也是必须做对照的地方——如果 S03 和 S01 相关性 0.9，说明它不是独立策略。

    **注意 entry_at 的语义**（这里踩过坑）：
      看涨缺口 lo..hi（lo=前一根高点，hi=第三根低点），价格上涨后回踩，
      价格会从上往下进入缺口，先碰到的是 **hi**，所以「近端」= hi。
      看跌缺口同理，近端 = lo（价格从下往上回踩）。
      写成 `hi if entry_at=="edge" else mid` 对两种方向都成立，
      不需要再按 direction 分叉——之前那个三重 if 是纯粹写错。
    """
    cs = cache.candles
    live: list[S.FVG] = []
    # 键必须是 **bar 下标**，不是时间戳。混用这两者不会报错，只会让
    # `by_t.get(i)` 永远返回空——所有 FVG 卡静默变成「零信号」。
    by_i: dict[int, list] = {}
    for f in cache.fvgs:
        k = _idx_of(cache, f.t)
        if k >= 0:
            by_i.setdefault(k, []).append(f)
    seen: set[int] = set()

    def fn(i, ctx):
        bar = cs[i]
        for f in by_i.get(i, ()):
            live.append(f)
        av = _atr(cs, i)
        if not av:
            return None

        # 用 bar 的 t 而不是 filled_at 判断：filled_at 是「之后」才知道的，
        # 在 bar i 决策时只能看 filled_at <= bar.t（严格小于当前才已发生）
        live[:] = [f for f in live
                   if f.filled_at is None or f.filled_at >= bar["t"]]
        if len(live) > 40:
            live[:] = live[-40:]

        for f in reversed(live):
            if f.i in seen:
                # 同一缺口只取第一次回踩，避免同一缺口刷十几次交易
                continue
            if i - _idx_of(cache, f.t) > retest_max_bars:
                continue
            mid = (f.lo + f.hi) / 2
            ref = f.hi if entry_at == "edge" else mid

            if f.direction == "bull":
                # 回踩到 ref 附近（容差 touch_atr），且收盘仍在缺口 lo 之上
                if bar["l"] <= ref + touch_atr * av and bar["c"] > f.lo:
                    stop = f.lo - 0.6 * av
                    price = bar["c"]
                    if price - stop < 0.15 * av:
                        continue
                    seen.add(f.i)
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
            else:
                ref = f.lo if entry_at == "edge" else mid
                # 看跌缺口在价格上方，回踩时价格从下往上碰到 lo
                if bar["h"] >= ref - touch_atr * av and bar["c"] < f.hi:
                    stop = f.hi + 0.6 * av
                    price = bar["c"]
                    if stop - price < 0.15 * av:
                        continue
                    seen.add(f.i)
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
        return None

    return fn


def _idx_of(cache: Cache, t: int) -> int:
    """时间戳 → 下标。委托给 Cache 实例，避免模块级 id 缓存。"""
    return cache.idx(t)


# ---------------------------------------------------------------------------
# S05 Order Block Retest
# ---------------------------------------------------------------------------

def s05_ob_retest(cache: Cache, retest_max_bars=40, touch_atr=0.15,
                  r_mult=2.0):
    """订单块回踩。OB 的 `confirmed_at` 保证因果。"""
    cs = cache.candles
    live: list[S.OrderBlock] = []
    by_c: dict[int, list] = {}
    for o in cache.obs:
        by_c.setdefault(o.confirmed_at, []).append(o)

    def fn(i, ctx):
        bar = cs[i]
        for o in by_c.get(i, ()):
            live.append(o)
        if len(live) > 60:
            live[:] = live[-60:]
        av = _atr(cs, i)
        if not av:
            return None

        for o in reversed(live):
            if i - o.confirmed_at > retest_max_bars:
                continue
            if o.direction == "bull":
                if bar["l"] <= o.hi + touch_atr * av and bar["c"] > o.lo:
                    stop = o.lo - 0.5 * av
                    price = bar["c"]
                    if price - stop < 0.15 * av:
                        continue
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
            else:
                if bar["h"] >= o.lo - touch_atr * av and bar["c"] < o.hi:
                    stop = o.hi + 0.5 * av
                    price = bar["c"]
                    if stop - price < 0.15 * av:
                        continue
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S07 Liquidity Sweep + MSS
# ---------------------------------------------------------------------------

def s07_sweep_mss(cache: Cache, require_mss=True, mss_window=5, r_mult=2.0):
    """扫流动性后收回，再要一个反向结构确认。

    **顺序不能反**：扫完之后才可能出现 MSS。之前在同一条 bar 上既要求
    「有 sweep」又要求「近 5 根内有 MSS」，等于要求 MSS 发生在 sweep 之前，
    所以只能匹配到个位数样本。正确做法是 sweep 之后再等 `mss_window` 根。

    `require_mss=True` 与 `False` 是一个关键消融：如果 False 的表现和 True
    差不多，说明「结构确认」这个环节没有贡献，S07 就退化成纯 sweep 策略。
    """
    cs = cache.candles
    sw_by_i: dict[int, list] = {}
    for s in cache.sweeps:
        k = _idx_of(cache, s.t)
        if k >= 0:
            sw_by_i.setdefault(k, []).append(s)

    armed: dict | None = None

    def fn(i, ctx):
        nonlocal armed
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]

        for s in sw_by_i.get(i, ()):
            armed = {"s": s, "at": i}
            break

        if armed is None:
            return None
        s, at = armed["s"], armed["at"]
        if i - at > mss_window:
            armed = None
            return None
        if i == at:
            # 清扫当根：若要求 MSS，则此时还没有反向结构，必须继续等
            if require_mss:
                return None

        if require_mss and i > at:
            want = "bear" if s.direction == "high" else "bull"
            if not _has_event_at(cache, i, want):
                return None

        if s.direction == "high":
            stop = s.extreme + 0.3 * av
            price = bar["c"]
            armed = None
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        stop = s.extreme - 0.3 * av
        price = bar["c"]
        armed = None
        if price - stop < 0.15 * av:
            return None
        return {"side": "long", "stop": stop,
                "target": _rr("long", price, stop, r_mult),
                "limit": None, "atr": av}

    return fn


def _has_event_at(cache: Cache, i: int, direction: str) -> bool:
    """bar i 收盘时是否正好确认了指定方向的结构事件。"""
    return any(e.direction == direction for e in cache.ev_at.get(i, ()))


# ---------------------------------------------------------------------------
# S08 OTE (0.62–0.79 回撤)
# ---------------------------------------------------------------------------

def s08_ote(cache: Cache, fib_lo=0.62, fib_hi=0.79, retest_max_bars=40,
            r_mult=2.0):
    """BOS 之后的 0.62–0.79 回撤区入场。

    位移腿 = BOS 的起点 pivot 到终点价格。回撤比例按这条腿算。
    """
    cs = cache.candles
    leg: dict | None = None

    def fn(i, ctx):
        nonlocal leg
        for e in cache.ev_at.get(i, ()):
            if e.kind not in ("BOS", "CHoCH"):
                continue
            p = _pivot_by_i(cache, e.pivot_i)
            if p is None:
                continue
            leg = {"dir": e.direction, "start": p.price, "end": cs[i]["c"],
                   "at": i}
        if leg is None:
            return None
        if i - leg["at"] > retest_max_bars:
            leg = None
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        span = abs(leg["end"] - leg["start"])
        if span < 1.0 * av:
            return None

        if leg["dir"] == "bull":
            lo = leg["end"] - fib_hi * span
            hi = leg["end"] - fib_lo * span
            if bar["l"] <= hi and bar["c"] > lo:
                stop = leg["start"] - 0.3 * av
                price = bar["c"]
                if price - stop < 0.15 * av:
                    return None
                leg = None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        else:
            lo = leg["end"] + fib_lo * span
            hi = leg["end"] + fib_hi * span
            if bar["h"] >= lo and bar["c"] < hi:
                stop = leg["start"] + 0.3 * av
                price = bar["c"]
                if stop - price < 0.15 * av:
                    return None
                leg = None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


def _pivot_by_i(cache: Cache, pi: int):
    """pivot 下标 → Pivot。同样委托给实例。"""
    return cache.pivot_by_i(pi)


# ---------------------------------------------------------------------------
# S20 Donchian Breakout（简单基准，先实现以便校准引擎）
# ---------------------------------------------------------------------------

def s20_donchian(cache: Cache, channel=20, atr_mult=2.0, r_mult=2.0):
    """N 周期新高突破。经典海龟，用作引擎校准的基准策略。

    如果这个在成本后完全为负，说明引擎的成本口径合理（已知海龟在
    高费率下会被吃掉）；如果它显示巨大盈利，那一定是引擎有 bug。
    """
    cs = cache.candles

    def fn(i, ctx):
        if i < channel:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        hi = max(b["h"] for b in cs[i - channel:i])
        lo = min(b["l"] for b in cs[i - channel:i])
        bar = cs[i]
        if bar["c"] > hi:
            stop = bar["c"] - atr_mult * av
            if bar["c"] - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", bar["c"], stop, r_mult),
                    "limit": None, "atr": av}
        if bar["c"] < lo:
            stop = bar["c"] + atr_mult * av
            return {"side": "short", "stop": stop,
                    "target": _rr("short", bar["c"], stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn