"""因果结构识别层：市场结构、FVG、订单块、流动性清扫。

**这是整个策略卡研究的地基，也是最容易埋前视的地方。** 三个具体的坑：

1. **pivot 确认有延迟**。一个「高点」只有在其右侧也出现更低的 K 线之后才能
   确认。用左 n 右 n 的中心窗口判 pivot，在 bar i 就会用到 bar i+n 的数据——
   回测里这叫未来函数，却不会报错，只会让策略看起来很准。
   本模块的 `pivots` 在每个 bar 上返回「此刻已经可以确认的」pivot，
   确认时间戳是 `index + right`，不是 pivot 本身的位置。

2. **FVG 的形成需要三根 K 线**。第 3 根收盘后才算成立，不能在缺口刚出现时
   就假设它有效。

3. **结构突破（BOS）需要收盘确认**。盘中穿越前高不算突破——那是最常见的
   假突破陷阱，而 S09 Turtle Soup 恰恰靠这个吃饭。

所有函数都是纯函数：给定 candles 和参数，返回按时间索引的数组。没有随机、
没有联网、不读全局状态。测试用「截断后半段数据，前半段结果必须不变」来
证明因果性。
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def atr(candles: list[dict], i: int, n: int = 14) -> float | None:
    """bar i 收盘时可知的 ATR（用 i-n+1..i）。"""
    if i < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l = candles[k]["h"], candles[k]["l"]
        pc = candles[k - 1]["c"] if k > 0 else candles[k]["o"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def rolling_extreme(vals: list[float], n: int, mode: str) -> list[float | None]:
    """过去 n 根（含当根）的极值。因果。"""
    out: list[float | None] = [None] * len(vals)
    for i in range(len(vals)):
        if i < n - 1:
            continue
        w = vals[i - n + 1: i + 1]
        out[i] = max(w) if mode == "max" else min(w)
    return out


# ---------------------------------------------------------------------------
# Pivot：带确认延迟的高低点
# ---------------------------------------------------------------------------

@dataclass
class Pivot:
    i: int              # pivot 所在的 bar 下标
    price: float
    kind: str           # 'high' | 'low'
    confirmed_at: int    # 能在哪根 bar 收盘后确认（>= i + right）
    prominence: float = 0.0   # 显著度（相对窗口另一侧的极值距离），用于分级


def pivots(candles: list[dict], left: int = 3, right: int = 3,
           min_prom_atr: float = 0.0, atr_n: int = 14) -> list[Pivot]:
    """全部 pivot，`confirmed_at` 标明何时可用。

    判据：bar i 的高点是「左 left 根与右 right 根都更低」。右侧那 right 根
    是确认所必需的代价——所以 confirmed_at = i + right。

    用法约束：在 bar t 做决策时，只能看 confirmed_at <= t 的 pivot。

    `min_prom_atr` 是**级别过滤**，不是可有可无的润色。左 3 右 3 在 1h 上
    只覆盖 6 小时，几乎每 5 根就产出一个 pivot，全部当成结构点会让 BOS
    退化成高频噪音（实测 BTC 1h 有 7683 个 BOS，平均 7 根一次）。显著度
    定义：高点到窗口内最低点的距离，用 ATR 归一，跨币可比。这跟 `left`/
    `right` 不同——`left`/`right` 决定「是不是局部极值」，prominence 决定
    「这个极值值不值得当结构」。
    """
    out: list[Pivot] = []
    n = len(candles)
    need_atr = min_prom_atr > 0
    for i in range(left, n - right):
        hi, lo = candles[i]["h"], candles[i]["l"]
        left_w = candles[i - left: i]
        right_w = candles[i + 1: i + 1 + right]
        win = left_w + right_w
        if all(b["h"] < hi for b in left_w) and all(b["h"] < hi for b in right_w):
            prom = hi - min(b["l"] for b in win)
            if not need_atr or (atr(candles, i + right, atr_n) or 1e18) * min_prom_atr <= prom:
                out.append(Pivot(i, hi, "high", i + right, prom))
        if all(b["l"] > lo for b in left_w) and all(b["l"] > lo for b in right_w):
            prom = max(b["h"] for b in win) - lo
            if not need_atr or (atr(candles, i + right, atr_n) or 1e18) * min_prom_atr <= prom:
                out.append(Pivot(i, lo, "low", i + right, prom))
    return out


def pivots_asof(ps: list[Pivot], t: int) -> list[Pivot]:
    """bar t 收盘时已经确认的 pivot 子集。这是唯一允许使用的视图。"""
    return [p for p in ps if p.confirmed_at <= t]


# ---------------------------------------------------------------------------
# 市场结构：BOS / CHoCH / MSS
# ---------------------------------------------------------------------------

@dataclass
class StructureEvent:
    t: int              # 事件在哪个 bar 收盘确认
    kind: str           # 'BOS' | 'CHoCH'
    direction: str      # 'bull' | 'bear'
    level: float        # 被突破的价位
    pivot_i: int        # 被突破的那个 pivot


def structure_events(candles: list[dict], ps: list[Pivot]) -> list[StructureEvent]:
    """逐 bar 判定结构突破。

    规则（全部收盘确认，不看盘中）：
      · 最近一个已确认、且尚未被突破的 swing high 被收盘价突破 → 多头 BOS
      · 若突破前的结构方向是向下的 → 记为 CHoCH（性质改变）
      · 对称处理空头

    实现要点：维护「已确认 pivot」队列，突破后就标记 consumed。之前写成
    两个指针共用、还 pop 掉队列尾部，导致一突破就立刻把新 pivot 也消耗掉，
    最终一个事件都出不来。
    """
    out: list[StructureEvent] = []
    n = len(candles)
    by_confirm: dict[int, list[Pivot]] = {}
    for p in ps:
        by_confirm.setdefault(p.confirmed_at, []).append(p)

    highs: list[Pivot] = []      # 已确认、未被突破
    lows: list[Pivot] = []
    trend = 0

    for t in range(n):
        for p in by_confirm.get(t, ()):
            if p.kind == "high":
                highs.append(p)
            else:
                lows.append(p)

        c = candles[t]["c"]

        # 多头：收盘突破最近一个未消耗的高点。
        # 注意 Pivot 的字段是 `i`（自身位置），确认延迟已经在 by_confirm 里
        # 表达过了（pivot 只在 confirmed_at 那天进队列），所以这里不必再比一次
        # 时间——同一个 bar 里 pivot 刚确认就同 bar 突破是合法的（右侧 K 线
        # 已经收盘，信息是已知的）。
        if highs:
            h = highs[-1]
            if c > h.price:
                kind = "CHoCH" if trend < 0 else "BOS"
                out.append(StructureEvent(t, kind, "bull", h.price, h.i))
                highs.pop()          # 只消耗这一个，不动 lows
                trend = 1

        # 空头：收盘跌破最近一个未消耗的低点
        if lows:
            l = lows[-1]
            if c < l.price:
                kind = "CHoCH" if trend > 0 else "BOS"
                out.append(StructureEvent(t, kind, "bear", l.price, l.i))
                lows.pop()
                trend = -1
    return out


# ---------------------------------------------------------------------------
# FVG：三根 K 线的失衡缺口
# ---------------------------------------------------------------------------

@dataclass
class FVG:
    i: int              # 缺口所在的中枢 bar
    t: int              # 成立时间（第 3 根 bar 的时间）
    direction: str      # 'bull'（看涨缺口，支撑）| 'bear'
    lo: float
    hi: float
    filled_at: int | None = None    # 被完全回补的 bar 时间
    touched_at: int | None = None   # 第一次被触碰的时间


def find_fvgs(candles: list[dict], min_gap_atr: float = 0.0,
              atr_n: int = 14) -> list[FVG]:
    """三根 K 线定义的缺口。

    看涨 FVG：bar i-1 的高 < bar i+1 的低，缺口区间 = [high[i-1], low[i+1]]
    看跌 FVG：bar i-1 的低 > bar i+1 的高，缺口区间 = [high[i+1], low[i-1]]

    成立时间 = bar i+1 的收盘（第 3 根）。这是硬约束，不能提前。
    `min_gap_atr` 过滤太小的缺口（用 ATR 归一，避免不同币不可比）。
    """
    out: list[FVG] = []
    for i in range(1, len(candles) - 1):
        a = candles[i - 1]
        b = candles[i + 1]
        gap = None
        if a["h"] < b["l"]:
            gap = ("bull", a["h"], b["l"])
        elif a["l"] > b["h"]:
            gap = ("bear", b["h"], a["l"])
        if gap is None:
            continue
        direction, lo, hi = gap
        if min_gap_atr > 0:
            av = atr(candles, i + 1, atr_n)
            if av is None or (hi - lo) < min_gap_atr * av:
                continue
        out.append(FVG(i, candles[i + 1]["t"], direction, lo, hi))

    # 标记触碰与回补：只在缺口成立之后扫描
    idx = {c["t"]: k for k, c in enumerate(candles)}
    for f in out:
        start = idx[f.t] + 1
        for k in range(start, len(candles)):
            bar = candles[k]
            if f.direction == "bull":
                if f.touched_at is None and bar["l"] <= f.hi:
                    f.touched_at = bar["t"]
                if bar["c"] < f.lo:
                    f.filled_at = bar["t"]
                    break
            else:
                if f.touched_at is None and bar["h"] >= f.lo:
                    f.touched_at = bar["t"]
                if bar["c"] > f.hi:
                    f.filled_at = bar["t"]
                    break
    return out


def fvgs_asof(fvgs: list[FVG], t: int) -> list[FVG]:
    """bar t 收盘时已成立、且尚未被完全回补的缺口。"""
    return [f for f in fvgs if f.t <= t and (f.filled_at is None or f.filled_at > t)]


# ---------------------------------------------------------------------------
# Order Block：位移行情之前的最后一根反向 K 线
# ---------------------------------------------------------------------------

@dataclass
class OrderBlock:
    i: int
    t: str
    direction: str      # 'bull'（需求区）| 'bear'（供给区）
    lo: float
    hi: float
    impulse_atr: float  # 离开这个块的位移幅度（ATR 倍数），用于排序质量
    confirmed_at: int = -1   # 位移走完的那根 bar（= i + lookback），此前不可用


def find_order_blocks(candles: list[dict], impulse_atr_min: float = 1.2,
                      atr_n: int = 14, lookback: int = 3) -> list[OrderBlock]:
    """位移定义：连续 lookback 根同向，累计幅度 >= impulse_atr_min × ATR。

    需求区（bull OB）= 那波拉升之前最后一根阴线的实体区间。
    这是朴素但可计算的定义；更复杂的「未缓解 OB」需要额外状态，留到后续。

    **因果性**：OB 的成立需要看到位移走完，也就是 bar `i + lookback` 收盘。
    所以 `confirmed_at = i + lookback`，而 OB 区间本身落在更早的 bar k 上。
    回测在 bar t 决策时只能用 `confirmed_at <= t` 的 OB——否则就是拿「后面
    涨了 1.2 个 ATR」这件事去回指一根更早的阴线，典型未来函数。
    """
    out: list[OrderBlock] = []
    for i in range(atr_n + lookback, len(candles) - lookback):
        av = atr(candles, i, atr_n)
        if not av:
            continue
        move = candles[i + lookback]["c"] - candles[i]["c"]
        if abs(move) < impulse_atr_min * av:
            continue
        direction = "bull" if move > 0 else "bear"
        # 往回找最后一根反向 K 线
        for k in range(i, max(i - 10, 0), -1):
            b = candles[k]
            bearish = b["c"] < b["o"]
            bullish = b["c"] > b["o"]
            if (direction == "bull" and bearish) or (direction == "bear" and bullish):
                lo, hi = min(b["o"], b["c"]), max(b["o"], b["c"])
                out.append(OrderBlock(k, b["t"], direction, lo, hi,
                                      round(abs(move) / av, 2),
                                      i + lookback))
                break
    return out


def order_blocks_asof(obs: list[OrderBlock], t: int) -> list[OrderBlock]:
    """bar t 收盘时已确认的订单块。"""
    return [o for o in obs if o.confirmed_at <= t]


# ---------------------------------------------------------------------------
# 流动性清扫：突破前高/前低后收回
# ---------------------------------------------------------------------------

@dataclass
class Sweep:
    t: int              # 收回的那根 bar 时间
    direction: str      # 'high'（扫了上方流动性）| 'low'
    level: float        # 被扫掉的极值
    extreme: float      # 扫的过程中打到的极值
    pivot_i: int


def find_sweeps(candles: list[dict], ps: list[Pivot], within: int = 3,
                min_wick_atr: float = 0.15, atr_n: int = 14) -> list[Sweep]:
    """盘中突破已确认的 pivot，但收盘收回其内侧。

    `within` 限制：pivot 被扫必须发生在它确认之后、且不超过 within 根——
    太久以前的 pivot 早失效了。
    `min_wick_atr` 要求影线足够长，否则只是普通穿越。
    """
    idx = {c["t"]: k for k, c in enumerate(candles)}
    out: list[Sweep] = []
    for p in ps:
        start = p.confirmed_at
        end = min(p.confirmed_at + within + 1, len(candles))
        for k in range(start, end):
            bar = candles[k]
            av = atr(candles, k, atr_n)
            if not av:
                continue
            if p.kind == "high" and bar["h"] > p.price and bar["c"] < p.price:
                if bar["h"] - max(bar["c"], bar["o"]) >= min_wick_atr * av:
                    out.append(Sweep(bar["t"], "high", p.price, bar["h"], p.i))
                    break
            elif p.kind == "low" and bar["l"] < p.price and bar["c"] > p.price:
                if min(bar["c"], bar["o"]) - bar["l"] >= min_wick_atr * av:
                    out.append(Sweep(bar["t"], "low", p.price, bar["l"], p.i))
                    break
    return out


# ---------------------------------------------------------------------------
# 交易区间（威科夫的前提：必须先有可识别的区间）
# ---------------------------------------------------------------------------

@dataclass
class Range:
    start_i: int
    end_i: int
    hi: float
    lo: float

    @property
    def height(self) -> float:
        return self.hi - self.lo

    @property
    def mid(self) -> float:
        return (self.hi + self.lo) / 2


def find_ranges(candles: list[dict], window: int = 40,
                max_width_atr: float = 4.0, atr_n: int = 14) -> list[Range]:
    """滑动窗口找「横向区间」：窗口内高低差 <= max_width_atr × ATR。

    这是威科夫分析的入场券——用户明确说过「已经形成可识别的交易区间，
    而非任意一个局部高低点」。区间宽度用 ATR 归一，跨币可比。
    """
    out: list[Range] = []
    last_end = -1
    for i in range(window, len(candles)):
        if i <= last_end:
            continue
        av = atr(candles, i, atr_n)
        if not av:
            continue
        w = candles[i - window + 1: i + 1]
        hi = max(b["h"] for b in w)
        lo = min(b["l"] for b in w)
        if (hi - lo) <= max_width_atr * av:
            out.append(Range(i - window + 1, i, hi, lo))
            last_end = i
    return out