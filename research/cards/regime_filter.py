"""状态过滤策略：把「上涨+高波」这个观察变成可回测的策略。

**与 `regime_split.py` 的区别很关键**：
  · `regime_split.py` 是**事后**把已成交的交易按状态分组，看哪组赚钱。
    它是观察，不是策略——因为分成四组再挑最好的，本身就在做多重比较。
  · 本模块把状态判断**前置到信号层**：只有处于目标状态时才允许出信号。
    这才是能上实盘的东西，也才能拿去做 Walk-forward。

三个必须处理的工程问题：

1. **状态切换的滞后**。状态标签每根 bar 都可能翻转（趋势 60 根窗口的
   收益率符号会在临界点附近抖动）。如果状态一变就切换，会产生大量假
   信号。用 `min_hold` 强制状态至少维持 N 根才允许切换。

2. **状态判断必须和信号判断用同一时刻的信息**。状态用 bar i 收盘算，
   信号也用 bar i 收盘算，两者都延后一根成交。不能用 i+1 的状态配 i
   的信号。

3. **趋势窗口 60 根在 15m 上只有 15 小时**，在 4h 上是 10 天。同样参数
   在不同周期含义完全不同，必须显式按周期配置，不能写死。
"""

from __future__ import annotations

import structure as S


# 各周期的合理趋势窗口（bar 数）与波动窗口
# 原则：趋势窗口约等于「2 周」；15m 上 2 周 = 1344 根过多，取 4 天 = 384 根。
REGIME_WINDOWS = {
    "15m": {"trend": 384, "vol": 96, "vol_hist": 480},
    "1h": {"trend": 168, "vol": 48, "vol_hist": 240},
    "4h": {"trend": 60, "vol": 40, "vol_hist": 200},
}

TARGET_STATES = ("涨·高波",)


def build_regime(candles: list[dict], trend_lb: int = 60, vol_lb: int = 40,
                 vol_window: int = 200, high_vol_pct: float = 0.5,
                 min_hold: int = 6) -> list[str | None]:
    """为每根 bar 打状态标签，严格因果。

    `min_hold` 是防抖动：标签变化后必须先维持 min_hold 根才承认。
    实现上不是"延迟切换"（那会引入前视），而是"只在候选标签连续出现
    min_hold 次后才切到它"——任何时刻的标签都只依赖已发生的数据。
    """
    n = len(candles)
    out: list[str | None] = [None] * n
    atr_hist: list[float] = []

    cur: str | None = None
    streak = 0
    cand: str | None = None

    for i in range(n):
        a = S.atr(candles, i, 14)
        if a:
            atr_hist.append(a / candles[i]["c"])
        if i < max(trend_lb, vol_lb) or len(atr_hist) < 50:
            continue

        ret = candles[i]["c"] / candles[i - trend_lb]["c"] - 1
        up = ret > 0

        cur_vol = atr_hist[-1]
        hist = atr_hist[-vol_window:] if len(atr_hist) > vol_window else atr_hist
        rank = sum(1 for x in hist if x <= cur_vol) / len(hist)
        high = rank > high_vol_pct

        want = ("涨" if up else "跌") + ("·高波" if high else "·低波")

        if want == cand:
            streak += 1
        else:
            cand = want
            streak = 1

        if want == cur:
            streak = 0
        elif streak >= min_hold:
            cur = want
            streak = 0

        out[i] = cur

    return out


def regime_for(tf: str) -> dict:
    """取该周期的窗口配置（原始键名，供阅读与断言用）。"""
    return REGIME_WINDOWS.get(tf, REGIME_WINDOWS["4h"])


def regime_kwargs(tf: str, min_hold: int = 6) -> dict:
    """把周期配置转成 `build_regime` 的关键字参数。

    **不要写 `build_regime(cs, **regime_for(tf))`**——`REGIME_WINDOWS` 用的
    是短键（`trend`/`vol`/`vol_hist`），而函数签名是 `trend_lb`/`vol_lb`/
    `vol_window`。直接解包会 `TypeError`，但更糟的情况是某天两边恰好名字
    撞上、语义不同，静默传错窗口。收敛到一个入口，改名只有一处要跟。
    """
    w = regime_for(tf)
    return {"trend_lb": w["trend"], "vol_lb": w["vol"],
            "vol_window": w["vol_hist"], "min_hold": min_hold}


def with_regime_filter(signal_fn, regimes: list[str | None],
                       targets=TARGET_STATES):
    """把状态过滤套在任意信号函数外面。

    这一层的价值在于**正交**：信号函数完全不知道状态过滤的存在，
    所以「过滤后是否更好」是干净的对照实验。如果让信号函数内部
    自己判断状态，就没法分离两者的贡献。
    """
    targets = set(targets)

    def fn(i, ctx):
        st = regimes[i] if i < len(regimes) else None
        if st not in targets:
            return None
        return signal_fn(i, ctx)

    return fn