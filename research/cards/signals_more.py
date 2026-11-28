"""剩余策略卡的信号函数（S04、S06、S09–S19、S21–S29）。

同样遵守 `signals_cards.py` 的约定。这里只记各卡实现时**必须避开的坑**，
因为每一张都会以不同方式重新引入未来函数：

  · **IFVG / Breaker**：必须等「原方向被收盘穿越」那一刻才翻转，不能在
    缺口刚形成时就假设它会被反转。
  · **Silver Bullet**：时段窗口用 UTC 小时，不能用交易所本地时间。
  · **Spring / UTAD**：区间是**先由过往数据识别出来**的，再在之后找假突破。
    反过来（先看到假突破再画区间）就是事后诸葛。
  · **SOS / SOW**：量能 z-score 必须用**当时之前**的成交量分布，不能用全样本。
  · **VSA**：同理，effort/result 的比较基准是滚动窗口。
  · **VWAP**：日内锚点切分要跟 K 线周期对齐，锚点错了整条线都是错的。
"""

from __future__ import annotations

import structure as S
from signals_cards import Cache, _atr, _rr, _idx_of


DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _vol_z(cs, i, n=50):
    """bar i 收盘时的成交量 z-score，用 i-n..i-1（**不含当根**，避免自我参照）。"""
    if i < n + 1:
        return None
    w = [cs[k]["v"] for k in range(i - n, i)]
    mu = sum(w) / n
    var = sum((x - mu) ** 2 for x in w) / max(n - 1, 1)
    sd = var ** 0.5
    if sd <= 0:
        return None
    return (cs[i]["v"] - mu) / sd


def _sma(cs, i, n):
    if i < n - 1:
        return None
    return sum(cs[k]["c"] for k in range(i - n + 1, i + 1)) / n


def _body(b):
    return abs(b["c"] - b["o"])


def _is_bull(b):
    return b["c"] > b["o"]


# ---------------------------------------------------------------------------
# S04 Inversion FVG
# ---------------------------------------------------------------------------

def s04_ifvg(cache: Cache, retest_max_bars=40, touch_atr=0.15, r_mult=2.0):
    """原方向 FVG 被**收盘穿越**后翻转为反向区间，回踩该区间入场。

    翻转时刻 = 首次出现 `c < lo`（看涨缺口被跌穿）那根 bar 的收盘。
    此前这个缺口只能是支撑，不能说它是阻力。
    """
    cs = cache.candles
    by_i: dict[int, list] = {}
    for f in cache.fvgs:
        k = _idx_of(cache, f.t)
        if k >= 0:
            by_i.setdefault(k, []).append(f)
    live: list = []
    inverted: list[dict] = []
    state: dict[int, str] = {}

    def fn(i, ctx):
        bar = cs[i]
        for f in by_i.get(i, ()):
            live.append(f)
            state[id(f)] = "normal"
        av = _atr(cs, i)
        if not av:
            return None

        # 检查翻转
        for f in live:
            if state.get(id(f)) != "normal":
                continue
            if f.direction == "bull" and bar["c"] < f.lo:
                state[id(f)] = "inverted"
                inverted.append({"f": f, "at": i, "dir": "bear"})
            elif f.direction == "bear" and bar["c"] > f.hi:
                state[id(f)] = "inverted"
                inverted.append({"f": f, "at": i, "dir": "bull"})
        if len(live) > 60:
            live[:] = live[-60:]
        if len(inverted) > 40:
            inverted[:] = inverted[-40:]

        for rec in reversed(inverted):
            f = rec["f"]
            if i - rec["at"] > retest_max_bars or i <= rec["at"]:
                continue
            if rec["dir"] == "bull":
                # 翻转后的缺口变成需求区：回踩到 hi 附近且收盘在其上
                if bar["l"] <= f.hi + touch_atr * av and bar["c"] > f.lo:
                    stop = f.lo - 0.6 * av
                    price = bar["c"]
                    if price - stop < 0.15 * av:
                        continue
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
            else:
                if bar["h"] >= f.lo - touch_atr * av and bar["c"] < f.hi:
                    stop = f.hi + 0.6 * av
                    price = bar["c"]
                    if stop - price < 0.15 * av:
                        continue
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S06 Breaker Block
# ---------------------------------------------------------------------------

def s06_breaker(cache: Cache, retest_max_bars=40, touch_atr=0.15, r_mult=2.0):
    """被收盘穿越的订单块翻转为反向入场区。

    与 S04 的差别只在「以什么区间为准」：S04 用缺口，S06 用 OB。
    两者高度重合，必须做信号相关性检查——如果相关系数 > 0.8，
    它们不是两个独立策略，收益不能相加。
    """
    cs = cache.candles
    by_c: dict[int, list] = {}
    for o in cache.obs:
        by_c.setdefault(o.confirmed_at, []).append(o)
    live: list = []
    broken: list[dict] = []
    state: dict[int, str] = {}

    def fn(i, ctx):
        bar = cs[i]
        for o in by_c.get(i, ()):
            live.append(o)
            state[id(o)] = "normal"
        av = _atr(cs, i)
        if not av:
            return None
        for o in live:
            if state.get(id(o)) != "normal":
                continue
            if o.direction == "bull" and bar["c"] < o.lo:
                state[id(o)] = "broken"
                broken.append({"o": o, "at": i, "dir": "bear"})
            elif o.direction == "bear" and bar["c"] > o.hi:
                state[id(o)] = "broken"
                broken.append({"o": o, "at": i, "dir": "bull"})
        if len(live) > 80:
            live[:] = live[-80:]
        if len(broken) > 40:
            broken[:] = broken[-40:]

        for rec in reversed(broken):
            o = rec["o"]
            if i - rec["at"] > retest_max_bars or i <= rec["at"]:
                continue
            if rec["dir"] == "bull":
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
# S09 Silver Bullet
# ---------------------------------------------------------------------------

def s09_silver_bullet(cache: Cache, hours=(10, 11, 14, 15, 18, 19),
                      touch_atr=0.1, r_mult=2.0):
    """小时窗口内的 FVG 回踩。

    `hours` 是 UTC 小时。窗口内出现并在窗口内被回踩才算——
    出了窗口就不再是 Silver Bullet，而是普通 FVG（S03）。
    """
    cs = cache.candles
    by_i: dict[int, list] = {}
    for f in cache.fvgs:
        k = _idx_of(cache, f.t)
        if k >= 0:
            by_i.setdefault(k, []).append(f)
    live: list = []
    hours = set(hours)

    def fn(i, ctx):
        bar = cs[i]
        h_utc = (bar["t"] // 3_600_000) % 24
        for f in by_i.get(i, ()):
            live.append({"f": f, "at": i})
        if len(live) > 50:
            live[:] = live[-50:]
        if h_utc not in hours:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        for rec in reversed(live):
            f = rec["f"]
            if i - rec["at"] > 12:      # 只在近期缺口上
                continue
            if f.filled_at is not None and f.filled_at <= bar["t"]:
                continue
            if f.direction == "bull":
                if bar["l"] <= f.hi + touch_atr * av and bar["c"] > f.lo:
                    stop = f.lo - 0.6 * av
                    price = bar["c"]
                    if price - stop < 0.15 * av:
                        continue
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
            else:
                if bar["h"] >= f.lo - touch_atr * av and bar["c"] < f.hi:
                    stop = f.hi + 0.6 * av
                    price = bar["c"]
                    if stop - price < 0.15 * av:
                        continue
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S10 / S11 Wyckoff Spring / UTAD
# ---------------------------------------------------------------------------

def s10_spring(cache: Cache, max_wait=80, require_second_test=True, r_mult=2.0):
    """区间下沿假跌破后收回 → 做多。可选等二次测试。

    「区间」来自 `find_ranges`，它们在 `end_i` 时刻就已确定，之后才允许
    出现假突破。这是本卡因果性的关键。
    """
    cs = cache.candles
    rg_by_end: dict[int, list] = {}
    for r in cache.ranges:
        rg_by_end.setdefault(r.end_i, []).append(r)
    recent: list = []
    spring: dict | None = None

    def fn(i, ctx):
        nonlocal spring
        for r in rg_by_end.get(i, ()):
            recent.append(r)
        if len(recent) > 20:
            recent[:] = recent[-20:]
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]

        # 寻找新的 Spring
        if spring is None:
            for r in reversed(recent):
                if i - r.end_i > max_wait:
                    continue
                if bar["l"] < r.lo and bar["c"] > r.lo and bar["c"] > bar["o"]:
                    spring = {"r": r, "low": bar["l"], "at": i}
                    break
            if spring is None:
                return None
            if require_second_test:
                return None         # 等二次测试

        # 二次测试：回踩但不破 Spring 低点
        if spring is not None:
            if bar["l"] < spring["low"]:
                spring = None       # 失效
                return None
            if i - spring["at"] > max_wait:
                spring = None
                return None
            if require_second_test:
                r = spring["r"]
                if (bar["l"] > spring["low"] and bar["l"] <= r.lo + 0.5 * av
                        and bar["c"] > r.lo):
                    stop = spring["low"] - 0.3 * av
                    price = bar["c"]
                    spring = None
                    if price - stop < 0.15 * av:
                        return None
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
            else:
                r = spring["r"]
                stop = spring["low"] - 0.3 * av
                price = bar["c"]
                spring = None
                if price - stop < 0.15 * av:
                    return None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


def s11_utad(cache: Cache, max_wait=80, require_second_test=True, r_mult=2.0):
    """S10 的镜像：区间上沿假突破后回落 → 做空。"""
    cs = cache.candles
    rg_by_end: dict[int, list] = {}
    for r in cache.ranges:
        rg_by_end.setdefault(r.end_i, []).append(r)
    recent: list = []
    utad: dict | None = None

    def fn(i, ctx):
        nonlocal utad
        for r in rg_by_end.get(i, ()):
            recent.append(r)
        if len(recent) > 20:
            recent[:] = recent[-20:]
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]

        if utad is None:
            for r in reversed(recent):
                if i - r.end_i > max_wait:
                    continue
                if bar["h"] > r.hi and bar["c"] < r.hi and bar["c"] < bar["o"]:
                    utad = {"r": r, "high": bar["h"], "at": i}
                    break
            if utad is None:
                return None
            if require_second_test:
                return None

        if utad is not None:
            if bar["h"] > utad["high"]:
                utad = None
                return None
            if i - utad["at"] > max_wait:
                utad = None
                return None
            if require_second_test:
                r = utad["r"]
                if (bar["h"] < utad["high"] and bar["h"] >= r.hi - 0.5 * av
                        and bar["c"] < r.hi):
                    stop = utad["high"] + 0.3 * av
                    price = bar["c"]
                    utad = None
                    if stop - price < 0.15 * av:
                        return None
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
            else:
                stop = utad["high"] + 0.3 * av
                price = bar["c"]
                utad = None
                if stop - price < 0.15 * av:
                    return None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S12 / S13 Wyckoff SOS+LPS / SOW+LPSY
# ---------------------------------------------------------------------------

def s12_sos_lps(cache: Cache, vol_z=1.0, max_wait=40, r_mult=2.0):
    """放量突破区间上沿（SOS），随后回踩不破区间中部（LPS）→ 做多。"""
    cs = cache.candles
    rg_by_end: dict[int, list] = {}
    for r in cache.ranges:
        rg_by_end.setdefault(r.end_i, []).append(r)
    recent: list = []
    sos: dict | None = None

    def fn(i, ctx):
        nonlocal sos
        for r in rg_by_end.get(i, ()):
            recent.append(r)
        if len(recent) > 20:
            recent[:] = recent[-20:]
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]

        if sos is None:
            z = _vol_z(cs, i)
            if z is None or z < vol_z:
                return None
            for r in reversed(recent):
                if i - r.end_i > 60:
                    continue
                if bar["c"] > r.hi and bar["c"] > bar["o"]:
                    sos = {"r": r, "at": i}
                    break
            return None

        if i - sos["at"] > max_wait:
            sos = None
            return None
        r = sos["r"]
        # LPS：回踩到区间中部之上，收盘重新走强
        if bar["c"] < r.mid:
            sos = None
            return None
        if bar["l"] <= r.hi + 0.5 * av and bar["c"] > r.hi:
            stop = r.mid - 0.3 * av
            price = bar["c"]
            sos = None
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


def s13_sow_lpsy(cache: Cache, vol_z=1.0, max_wait=40, r_mult=2.0):
    """S12 镜像。"""
    cs = cache.candles
    rg_by_end: dict[int, list] = {}
    for r in cache.ranges:
        rg_by_end.setdefault(r.end_i, []).append(r)
    recent: list = []
    sow: dict | None = None

    def fn(i, ctx):
        nonlocal sow
        for r in rg_by_end.get(i, ()):
            recent.append(r)
        if len(recent) > 20:
            recent[:] = recent[-20:]
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]

        if sow is None:
            z = _vol_z(cs, i)
            if z is None or z < vol_z:
                return None
            for r in reversed(recent):
                if i - r.end_i > 60:
                    continue
                if bar["c"] < r.lo and bar["c"] < bar["o"]:
                    sow = {"r": r, "at": i}
                    break
            return None

        if i - sow["at"] > max_wait:
            sow = None
            return None
        r = sow["r"]
        if bar["c"] > r.mid:
            sow = None
            return None
        if bar["h"] >= r.lo - 0.5 * av and bar["c"] < r.lo:
            stop = r.mid + 0.3 * av
            price = bar["c"]
            sow = None
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S14 / S15 Breakout Retest / False Breakout Reversal
# ---------------------------------------------------------------------------

def s14_breakout_retest(cache: Cache, channel=20, retest_bars=10, r_mult=2.0):
    """突破 N 周期高点后，回踩该高点确认再入场。"""
    cs = cache.candles
    brk: dict | None = None

    def fn(i, ctx):
        nonlocal brk
        if i < channel:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        if brk is None:
            hi = max(b["h"] for b in cs[i - channel:i])
            lo = min(b["l"] for b in cs[i - channel:i])
            if bar["c"] > hi:
                brk = {"dir": "bull", "level": hi, "at": i}
            elif bar["c"] < lo:
                brk = {"dir": "bear", "level": lo, "at": i}
            return None

        if i - brk["at"] > retest_bars:
            brk = None
            return None
        lv = brk["level"]
        if brk["dir"] == "bull":
            if bar["c"] < lv - 0.5 * av:
                brk = None
                return None
            if bar["l"] <= lv + 0.2 * av and bar["c"] > lv:
                stop = lv - 1.0 * av
                price = bar["c"]
                brk = None
                if price - stop < 0.15 * av:
                    return None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        else:
            if bar["c"] > lv + 0.5 * av:
                brk = None
                return None
            if bar["h"] >= lv - 0.2 * av and bar["c"] < lv:
                stop = lv + 1.0 * av
                price = bar["c"]
                brk = None
                if stop - price < 0.15 * av:
                    return None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


def s15_false_breakout(cache: Cache, lookback=20, reclaim_bars=2, r_mult=2.0):
    """**两段式**假突破：先有一根收盘突破 N 周期极值，随后 `reclaim_bars`
    根内收盘又回到极值内侧。

    与 S29 的区别（这里踩过坑，两者最初写成了一模一样）：
      · S29（Turtle Soup）：**同一根** K 线内刺穿并收回，即影线突破。
      · S15：**先突破、后收回**，中间隔了至少一根 K 线。收盘突破是「真
        突破」的必要条件，所以 S15 过滤掉的全是影线刺穿，剩下的是「收盘
        站上去又被打回来」——这才是假突破里信息量更高的那一类。
    最初两者都用了 `bar["h"] > hi and bar["c"] < hi`，导致 2588 笔成交、
    每一个统计量完全相同。那是实现重复，不是两个独立假设。
    """
    cs = cache.candles
    broken: dict | None = None

    def fn(i, ctx):
        nonlocal broken
        if i < lookback + 1:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        hi = max(b["h"] for b in cs[i - lookback:i])
        lo = min(b["l"] for b in cs[i - lookback:i])

        # 阶段一：确认过一次收盘突破
        if broken is not None:
            if i - broken["at"] > reclaim_bars:
                broken = None
            else:
                lv, d = broken["level"], broken["dir"]
                if d == "bull" and bar["c"] < lv:
                    # 收盘跌回突破位内侧 = 假突破成立
                    stop = max(cs[broken["at"]]["h"], bar["h"]) + 0.3 * av
                    price = bar["c"]
                    broken = None
                    if stop - price < 0.15 * av:
                        return None
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
                if d == "bear" and bar["c"] > lv:
                    stop = min(cs[broken["at"]]["l"], bar["l"]) - 0.3 * av
                    price = bar["c"]
                    broken = None
                    if price - stop < 0.15 * av:
                        return None
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
                return None

        # 阶段零：记录收盘突破
        if bar["c"] > hi:
            broken = {"dir": "bull", "level": hi, "at": i}
        elif bar["c"] < lo:
            broken = {"dir": "bear", "level": lo, "at": i}
        return None

    return fn


# ---------------------------------------------------------------------------
# S16 / S17 / S18 / S19 裸K
# ---------------------------------------------------------------------------

def s16_sr_rejection(cache: Cache, touch_atr=0.2, min_wick_atr=0.3, r_mult=2.0):
    """在已确认 pivot 水平上出现拒绝影线。"""
    cs = cache.candles
    piv_at: dict[int, list] = {}
    for p in cache.pivots:
        piv_at.setdefault(p.confirmed_at, []).append(p)
    live: list = []

    def fn(i, ctx):
        for p in piv_at.get(i, ()):
            live.append(p)
        if len(live) > 30:
            live[:] = live[-30:]
        if not live:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        for p in reversed(live[-12:]):
            if p.kind == "high" and abs(bar["h"] - p.price) <= touch_atr * av:
                wick = bar["h"] - max(bar["c"], bar["o"])
                if wick >= min_wick_atr * av and bar["c"] < p.price:
                    stop = bar["h"] + 0.3 * av
                    price = bar["c"]
                    if stop - price < 0.15 * av:
                        continue
                    return {"side": "short", "stop": stop,
                            "target": _rr("short", price, stop, r_mult),
                            "limit": None, "atr": av}
            if p.kind == "low" and abs(bar["l"] - p.price) <= touch_atr * av:
                wick = min(bar["c"], bar["o"]) - bar["l"]
                if wick >= min_wick_atr * av and bar["c"] > p.price:
                    stop = bar["l"] - 0.3 * av
                    price = bar["c"]
                    if price - stop < 0.15 * av:
                        continue
                    return {"side": "long", "stop": stop,
                            "target": _rr("long", price, stop, r_mult),
                            "limit": None, "atr": av}
        return None

    return fn


def s17_pin_bar(cache: Cache, wick_body_ratio=2.5, min_wick_atr=0.5, r_mult=2.0):
    """长影线小实体：影线 >= ratio × 实体，且 >= min_wick_atr × ATR。"""
    cs = cache.candles

    def fn(i, ctx):
        if i < 1:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        body = _body(bar)
        upper = bar["h"] - max(bar["c"], bar["o"])
        lower = min(bar["c"], bar["o"]) - bar["l"]
        if body <= 0:
            return None
        if lower >= wick_body_ratio * body and lower >= min_wick_atr * av:
            stop = bar["l"] - 0.3 * av
            price = bar["c"]
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        if upper >= wick_body_ratio * body and upper >= min_wick_atr * av:
            stop = bar["h"] + 0.3 * av
            price = bar["c"]
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


def s18_engulfing(cache: Cache, min_body_ratio=1.2, r_mult=2.0):
    """吞没：本根实体完全覆盖前一根，且实体更大。"""
    cs = cache.candles

    def fn(i, ctx):
        if i < 1:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        p, b = cs[i - 1], cs[i]
        pb, bb = _body(p), _body(b)
        if pb <= 0 or bb < min_body_ratio * pb:
            return None
        if _is_bull(b) and not _is_bull(p):
            if b["c"] > p["o"] and b["o"] < p["c"]:
                stop = min(b["l"], p["l"]) - 0.2 * av
                price = b["c"]
                if price - stop < 0.15 * av:
                    return None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        if not _is_bull(b) and _is_bull(p):
            if b["c"] < p["o"] and b["o"] > p["c"]:
                stop = max(b["h"], p["h"]) + 0.2 * av
                price = b["c"]
                if stop - price < 0.15 * av:
                    return None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


def s19_inside_bar(cache: Cache, max_wait=8, r_mult=2.0):
    """母子线：突破母体区间方向入场。"""
    cs = cache.candles
    ib: dict | None = None

    def fn(i, ctx):
        nonlocal ib
        if i < 2:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar, prev = cs[i], cs[i - 1]
        if ib is None:
            if prev["h"] <= cs[i - 2]["h"] and prev["l"] >= cs[i - 2]["l"]:
                mother = cs[i - 2]
                ib = {"hi": mother["h"], "lo": mother["l"], "at": i - 1}
            return None
        if i - ib["at"] > max_wait:
            ib = None
            return None
        if bar["c"] > ib["hi"]:
            stop = ib["lo"] - 0.2 * av
            price = bar["c"]
            rec, ib = ib, None
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        if bar["c"] < ib["lo"]:
            stop = ib["hi"] + 0.2 * av
            price = bar["c"]
            ib = None
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S21 / S22 / S23 趋势
# ---------------------------------------------------------------------------

def s21_squeeze(cache: Cache, squeeze_pct=0.25, atr_n=14, lookback=80,
                r_mult=2.0):
    """波动率压缩末端突破。

    压缩判定：当前 ATR 在过去 `lookback` 根 ATR 中的分位 <= squeeze_pct。
    分位用的是**历史 ATR 序列**，所以必须从 i-lookback 开始取，不能全样本。
    """
    cs = cache.candles

    def fn(i, ctx):
        if i < lookback + atr_n:
            return None
        av = _atr(cs, i, atr_n)
        if not av:
            return None
        hist = []
        for k in range(i - lookback, i):
            a = _atr(cs, k, atr_n)
            if a:
                hist.append(a)
        if len(hist) < lookback // 2:
            return None
        rank = sum(1 for a in hist if a <= av) / len(hist)
        if rank > squeeze_pct:
            return None
        bar = cs[i]
        hi = max(b["h"] for b in cs[i - 20:i])
        lo = min(b["l"] for b in cs[i - 20:i])
        if bar["c"] > hi:
            stop = bar["c"] - 1.5 * av
            if bar["c"] - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", bar["c"], stop, r_mult),
                    "limit": None, "atr": av}
        if bar["c"] < lo:
            stop = bar["c"] + 1.5 * av
            return {"side": "short", "stop": stop,
                    "target": _rr("short", bar["c"], stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


def s22_trend_pullback(cache: Cache, ma_fast=20, ma_slow=50,
                       pullback_atr=0.5, r_mult=2.0):
    """慢均线之上，回踩快均线后收盘重新走强。"""
    cs = cache.candles

    def fn(i, ctx):
        if i < ma_slow + 2:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        f = _sma(cs, i, ma_fast)
        s = _sma(cs, i, ma_slow)
        pf = _sma(cs, i - 1, ma_fast)
        if None in (f, s, pf):
            return None
        bar = cs[i]
        if s > pf and bar["c"] > s:
            if bar["l"] <= f + pullback_atr * av and bar["c"] > f:
                stop = min(bar["l"], f) - 0.5 * av
                price = bar["c"]
                if price - stop < 0.15 * av:
                    return None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        if s < pf and bar["c"] < s:
            if bar["h"] >= f - pullback_atr * av and bar["c"] < f:
                stop = max(bar["h"], f) + 0.5 * av
                price = bar["c"]
                if stop - price < 0.15 * av:
                    return None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


def s23_momentum(cache: Cache, body_atr=1.2, r_mult=2.0):
    """强实体 K 线后的短期延续。"""
    cs = cache.candles

    def fn(i, ctx):
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        body = _body(bar)
        if body < body_atr * av:
            return None
        if _is_bull(bar):
            stop = bar["o"] - 0.3 * av
            price = bar["c"]
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        stop = bar["o"] + 0.3 * av
        price = bar["c"]
        if stop - price < 0.15 * av:
            return None
        return {"side": "short", "stop": stop,
                "target": _rr("short", price, stop, r_mult),
                "limit": None, "atr": av}

    return fn


# ---------------------------------------------------------------------------
# S24 CVD Divergence
# ---------------------------------------------------------------------------

def _cvd(cs) -> list[float]:
    """累计成交量 delta：主动买 - 主动卖。缺字段则返回空。"""
    if not cs or "taker_buy_v" not in cs[0]:
        return []
    out, acc = [], 0.0
    for c in cs:
        tb = c.get("taker_buy_v")
        if tb is None:
            return []
        acc += 2 * tb - c["v"]
        out.append(acc)
    return out


def s24_cvd_divergence(cache: Cache, window=30, min_div_atr=0.5, r_mult=2.0):
    """价格新高但 CVD 未新高（顶背离）→ 做空；反之做多。"""
    cs = cache.candles
    cvd = _cvd(cs)
    if not cvd:
        return None, "缺 taker_buy_volume 字段"

    def fn(i, ctx):
        if i < window + 1:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        w = cs[i - window:i]
        hi = max(b["h"] for b in w)
        lo = min(b["l"] for b in w)
        cv_w = cvd[i - window:i]
        cv_hi = max(cv_w)
        cv_lo = min(cv_w)

        # 顶背离：价格创窗口新高，CVD 没有
        if bar["h"] > hi and cvd[i] < cv_hi:
            if (bar["h"] - hi) >= min_div_atr * av:
                stop = bar["h"] + 0.3 * av
                price = bar["c"]
                if stop - price < 0.15 * av:
                    return None
                return {"side": "short", "stop": stop,
                        "target": _rr("short", price, stop, r_mult),
                        "limit": None, "atr": av}
        # 底背离
        if bar["l"] < lo and cvd[i] > cv_lo:
            if (lo - bar["l"]) >= min_div_atr * av:
                stop = bar["l"] - 0.3 * av
                price = bar["c"]
                if price - stop < 0.15 * av:
                    return None
                return {"side": "long", "stop": stop,
                        "target": _rr("long", price, stop, r_mult),
                        "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S27 VSA
# ---------------------------------------------------------------------------

def s27_vsa(cache: Cache, vol_zscore=2.0, spread_atr=0.8, r_mult=2.0):
    """放量滞涨 / 放量滞跌：高量能但推进幅度小 → 趋势衰竭。"""
    cs = cache.candles

    def fn(i, ctx):
        av = _atr(cs, i)
        if not av:
            return None
        z = _vol_z(cs, i)
        if z is None or z < vol_zscore:
            return None
        bar = cs[i]
        spread = bar["h"] - bar["l"]
        body = _body(bar)
        # 放量但实体很小 = effort 大 result 小
        if body > spread_atr * av * 0.5:
            return None
        if spread < 0.5 * av:
            return None
        # 在上影线处做空 / 下影线处做多
        upper = bar["h"] - max(bar["c"], bar["o"])
        lower = min(bar["c"], bar["o"]) - bar["l"]
        if upper > lower and upper >= 0.5 * av:
            stop = bar["h"] + 0.3 * av
            price = bar["c"]
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        if lower > upper and lower >= 0.5 * av:
            stop = bar["l"] - 0.3 * av
            price = bar["c"]
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S28 VWAP
# ---------------------------------------------------------------------------

def s28_vwap(cache: Cache, dev_atr=2.0, vwap_bars=24, min_bars=8, r_mult=2.0):
    """价格偏离 VWAP 超过 dev_atr → 均值回归。

    **锚点必须按周期自适应。** 日内 VWAP 在 4h 上不成立——一天只有 6 根
    K 线，`i - day_start >= 8` 这个门槛永远达不到，信号恒为 0。这是第一版
    在本周期上整张卡失效的原因。改用**滚动 N 根**的 VWAP，N 按周期给出：
    1h→24（一天），4h→42（约一周），15m→96（一天）。

    日内锚点仍然保留为可选项（`vwap_bars=None`），因为 S28 的原始假设确实
    是日内口径；但默认必须用能跑通的那一个，否则等于把卡废掉。
    """
    cs = cache.candles
    pv: list[float] = []
    vv: list[float] = []
    acc_pv = acc_v = 0.0

    def fn(i, ctx):
        nonlocal acc_pv, acc_v
        bar = cs[i]
        tp = (bar["h"] + bar["l"] + bar["c"]) / 3
        pv.append(tp * bar["v"])
        vv.append(bar["v"])
        acc_pv += tp * bar["v"]
        acc_v += bar["v"]

        # 滑动窗口：超出 n 就把最老的减掉
        n = vwap_bars
        if len(pv) > n:
            acc_pv -= pv[-n - 1]
            acc_v -= vv[-n - 1]
        if len(pv) < min_bars or acc_v <= 0:
            return None

        vwap = acc_pv / acc_v
        av = _atr(cs, i)
        if not av:
            return None
        dev = (bar["c"] - vwap) / av
        if dev >= dev_atr:
            stop = bar["h"] + 0.3 * av
            price = bar["c"]
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        if dev <= -dev_atr:
            stop = bar["l"] - 0.3 * av
            price = bar["c"]
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn


# ---------------------------------------------------------------------------
# S29 Turtle Soup
# ---------------------------------------------------------------------------

def s29_turtle_soup(cache: Cache, lookback=20, r_mult=2.0):
    """创 N 周期新低后收盘收回前低之上 → 做多（前高低同理做空）。

    与 S07 的区别：S07 要求「扫掉已确认 pivot 且出现 MSS」，S29 只用
    N 周期极值 + 收盘收回。所以 S29 是 S07 的**更宽松版本**，
    信号相关性必然高——这正是必须做相关性检查的原因。
    """
    cs = cache.candles

    def fn(i, ctx):
        if i < lookback + 1:
            return None
        av = _atr(cs, i)
        if not av:
            return None
        bar = cs[i]
        hi = max(b["h"] for b in cs[i - lookback:i])
        lo = min(b["l"] for b in cs[i - lookback:i])
        if bar["h"] > hi and bar["c"] < hi:
            stop = bar["h"] + 0.3 * av
            price = bar["c"]
            if stop - price < 0.15 * av:
                return None
            return {"side": "short", "stop": stop,
                    "target": _rr("short", price, stop, r_mult),
                    "limit": None, "atr": av}
        if bar["l"] < lo and bar["c"] > lo:
            stop = bar["l"] - 0.3 * av
            price = bar["c"]
            if price - stop < 0.15 * av:
                return None
            return {"side": "long", "stop": stop,
                    "target": _rr("long", price, stop, r_mult),
                    "limit": None, "atr": av}
        return None

    return fn