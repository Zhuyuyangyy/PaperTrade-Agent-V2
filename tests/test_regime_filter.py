"""把「市场状态分层被证伪」这件事固化成回归测试。

**为什么值得单独一个文件守它**：这是本轮最容易复发的错误，而且复发时
没有任何东西会报错——只会在报告里多出一节「上涨+高波状态下收益提升 4-7 倍」
的漂亮结论。事实是那个提升来自**事后知道状态何时翻转**，一旦要求状态
至少维持若干根（人真的能交易的节奏），优势就消失了。

这个文件守三层：

1. **状态标签必须因果**（截断测试）。`build_regime` 用 bar i 收盘算标签，
   标签不能让 bar i 之后的数据进来。这条一旦破了，下面两条都无意义。
2. **`min_hold` 必须真的降低标签抖动**。如果它不降抖动，说明实现写成了
   「延迟切换」而不是「连续 N 次才承认」——前者本身就是前视。
3. **证伪结论本身**：不加 `min_hold` 时的「显著优势」在加上之后必须消失。
   这条是数据相关的（换一份行情可能变），所以它在失败时会明确说
   「先确认是数据变了还是实现退化了」，而不是让人直接删测试。

全部不联网。缺数据就 skip，绝不假装通过。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "research" / "cards"
sys.path.insert(0, str(CARDS))

import engine as E                 # noqa: E402
import regime_filter as RF         # noqa: E402
import signals_cards as C          # noqa: E402

DATA = CARDS / "data"


def load(name: str) -> list[dict]:
    p = DATA / name
    if not p.is_file():
        pytest.skip(f"缺数据 {name}")
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"t": int(r["t"]), "o": float(r["o"]),
                        "h": float(r["h"]), "l": float(r["l"]),
                        "c": float(r["c"]), "v": float(r["v"])})
    return out


def _flips(labels: list) -> int:
    """标签发生变化的次数。"""
    n = 0
    for a, b in zip(labels, labels[1:]):
        if a != b:
            n += 1
    return n


# ---------------------------------------------------------------------------
# 1. 因果性
# ---------------------------------------------------------------------------

def test_regime_labels_are_causal_under_truncation():
    """截断到 2/3 后，前 2/3 的标签必须逐字节不变。

    这是唯一能机械证明「状态判断没有偷看未来」的方法。`build_regime`
    里任何一处读了 `candles[i+1:]`，截断都会改变 i 之前的结果。
    """
    cs = load("BTCUSDT_4h.csv")
    cut = len(cs) * 2 // 3
    kw = RF.regime_kwargs("4h", min_hold=6)

    full = RF.build_regime(cs, **kw)
    half = RF.build_regime(cs[:cut], **kw)

    assert full[:cut] == half, (
        "截断后前 2/3 标签变了——状态判断存在前视")


def test_regime_label_never_uses_future_atr_percentile():
    """波动分位只能和**已发生**的历史比，不能把未来也算进分布。

    直接检查：把最后一根 K 线的价格改掉，除了最后一根本身，前面所有
    标签都不该变。这条能抓住「先算全序列分位再回填」这种写法。
    """
    cs = load("BTCUSDT_4h.csv")[:3000]
    kw = RF.regime_kwargs("4h", min_hold=6)
    base = RF.build_regime(cs, **kw)

    cs2 = [dict(c) for c in cs]
    cs2[-1]["c"] = cs2[-1]["c"] * 1.5
    cs2[-1]["h"] = cs2[-1]["h"] * 1.5
    moved = RF.build_regime(cs2, **kw)

    assert base[:-1] == moved[:-1], (
        "改了最后一根 K 线，前面若干标签跟着变了——波动分位用了全样本")


# ---------------------------------------------------------------------------
# 2. min_hold 必须真的降抖动
# ---------------------------------------------------------------------------

def test_min_hold_reduces_label_churn():
    """`min_hold` 越大，标签翻转次数必须越少。

    如果这条不成立，说明实现被改成了「延迟切换」：先算出 want 再延后
    min_hold 根生效——那等于在 bar i 用了 i 之后才知道的信息。
    """
    cs = load("BTCUSDT_4h.csv")
    f1 = _flips(RF.build_regime(cs, **RF.regime_kwargs("4h", 1)))
    f6 = _flips(RF.build_regime(cs, **RF.regime_kwargs("4h", 6)))
    f24 = _flips(RF.build_regime(cs, **RF.regime_kwargs("4h", 24)))

    assert f1 > f6 > f24, f"翻转次数未随 min_hold 下降：{f1} / {f6} / {f24}"
    assert f6 < f1 * 0.6, (
        f"min_hold=6 只把抖动从 {f1} 降到 {f6}，说明防抖动几乎没生效")


def test_regime_windows_are_configured_per_timeframe():
    """状态窗口必须按周期配置。60 根在 4h 是 10 天，在 15m 只有 15 小时。"""
    assert RF.regime_for("15m")["trend"] > RF.regime_for("4h")["trend"]
    assert set(RF.REGIME_WINDOWS) >= {"15m", "1h", "4h"}


# ---------------------------------------------------------------------------
# 3. 证伪结论：加上防抖动后优势必须消失
# ---------------------------------------------------------------------------

def _run_s20(cs, regimes, min_hold_note: str):
    cache = C.Cache(cs)
    raw = C.s20_donchian(cache, channel=55, atr_mult=2.0)
    fn = (RF.with_regime_filter(raw, regimes)
          if regimes is not None else raw)
    return E.run(cs, card="S20", symbol="BTCUSDT", timeframe="4h",
                 params={"channel": 55, "atr_mult": 2.0}, signal_fn=fn,
                 bar_hours=4.0, max_hold_bars=200)


def test_regime_edge_disappears_under_min_hold():
    """**本轮最重要的负面结果，必须钉死。**

    事后按状态分组时，「上涨+高波」组看起来强得离谱（+1.4%/笔，R 倍数
    归一化后仍提升 4-7 倍）。但那个分组隐含了「事后知道状态何时翻转」
    这一全样本信息。

    把它前置成可交易的过滤器、并要求状态至少维持 6 根之后：
      · 笔数从 944 掉到 ~234（少了 75%）
      · 净期望从 +0.56% 掉到 ~0.60% 但置信区间跨 0
    也就是说，优势里能被交易的那部分消失了。

    失败时的正确反应不是删测试，而是先回答：
      a) 行情数据换过了吗？（换过则结论需要重算，不是测试错）
      b) `build_regime` 的因果测试还过吗？（不过则是前视回归）
      c) `with_regime_filter` 是否被改成了「事后套用」？
    """
    cs = load("BTCUSDT_4h.csv")
    loose = _run_s20(cs, RF.build_regime(cs, **RF.regime_kwargs("4h", 1)),
                     "min_hold=1")
    tight = _run_s20(cs, RF.build_regime(cs, **RF.regime_kwargs("4h", 6)),
                     "min_hold=6")
    base = _run_s20(cs, None, "无过滤")

    assert loose.n_trades > 0 and tight.n_trades > 0 and base.n_trades > 0

    # 过滤本身必须真的减少交易，否则「过滤」根本没生效
    assert tight.n_trades < base.n_trades * 0.5, (
        f"min_hold=6 只过滤掉 "
        f"{100 * (1 - tight.n_trades / base.n_trades):.0f}% 的交易，"
        f"状态过滤可能没接上")

    # 关键断言：不加防抖动的「优势」在加了之后必须不可分辨
    from robustness import bootstrap_ci
    lo_l, hi_l = bootstrap_ci([t.net_pct for t in loose.trades])
    lo_t, hi_t = bootstrap_ci([t.net_pct for t in tight.trades])

    assert lo_l > 0, (
        f"min_hold=1 的 CI [{lo_l:.3f}, {hi_l:.3f}] 竟然跨 0——"
        f"这可能是数据变了，请重跑 regime 分析确认")
    assert lo_t < 0, (
        f"min_hold=6 的 CI [{lo_t:.3f}, {hi_t:.3f}] 居然不含 0，"
        f"即防抖动后优势仍在。这推翻了报告 §3.5 的结论——"
        f"要么行情换了，要么状态判断被改出了前视。"
        f"不要直接改测试，先查清楚是哪一种。")


def test_regime_filter_is_orthogonal_to_signal():
    """过滤器只能在原始信号为真时放行，不能凭空造信号。"""
    cs = load("BTCUSDT_4h.csv")[:4000]
    cache = C.Cache(cs)
    raw = C.s20_donchian(cache, channel=55, atr_mult=2.0)
    ctx = {"i": 0, "candles": cs, "bar": cs[0]}

    regimes = ["涨·高波"] * len(cs)
    gated = RF.with_regime_filter(raw, regimes)
    hits_raw = {i for i in range(len(cs)) if raw(i, {"i": i, "candles": cs,
                                                     "bar": cs[i]})}
    hits_gated = {i for i in range(len(cs)) if gated(i, {"i": i, "candles": cs,
                                                         "bar": cs[i]})}
    assert hits_gated == hits_raw, "全部放行时结果必须与不过滤一致"

    none_reg = [None] * len(cs)
    gated_none = RF.with_regime_filter(raw, none_reg)
    assert not any(gated_none(i, {"i": i, "candles": cs, "bar": cs[i]})
                   for i in range(len(cs))), "状态为 None 时必须全部拦掉"