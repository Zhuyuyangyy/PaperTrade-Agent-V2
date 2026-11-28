"""30 张策略卡平台的回归测试。

守四件事，每件都对应本轮真实踩到的一个 bug：

1. **结构层因果性**：截断数据后，前半段结果必须逐字节不变。
2. **时间戳/下标不能混用**：`by_t.get(i)` 这种写法不报错，只会静默产出零信号。
3. **Cache 索引不能跨实例复用**：模块级 `id(cache)` 缓存会因对象回收而串味。
4. **执行口径**：入场必须延后一根、同根 K 线先判止损。

这些测试都不联网。数据从 `research/cards/data/` 读，缺文件就 skip——
CI 里没有本地行情是正常的，但绝不能因为缺数据就假装通过。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "research" / "cards"
sys.path.insert(0, str(CARDS))

import engine as E                     # noqa: E402
import registry as R                   # noqa: E402
import signals_cards as C              # noqa: E402
import signals_more as M               # noqa: E402
import structure as S                  # noqa: E402

DATA = CARDS / "data"


def load(name: str) -> list[dict]:
    p = DATA / name
    if not p.is_file():
        pytest.skip(f"缺数据 {name}")
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        has_taker = "taker_buy_v" in (rd.fieldnames or [])
        for r in rd:
            d = {"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                 "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])}
            if has_taker and r.get("taker_buy_v"):
                d["taker_buy_v"] = float(r["taker_buy_v"])
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# 1. 注册表完整性
# ---------------------------------------------------------------------------

def test_registry_has_30_cards_in_six_families():
    r = R.registry()
    assert r["n_cards"] == 30
    assert r["families"] == {"SMC/ICT": 9, "Wyckoff": 4, "PriceAction": 6,
                             "Trend": 4, "OrderFlow": 4, "Other": 3}
    assert len({c.id for c in R.CARDS}) == 30


def test_data_blocked_cards_are_declared_not_hidden():
    """缺 OI/清算/新闻的卡必须被识别出来，不能悄悄当普通卡跑。"""
    blocked = {c.id for c in R.data_blocked({"ohlcv", "taker_buy_volume"})}
    assert blocked == {"S25", "S26", "S30"}


def test_every_card_has_params_fixed_before_backtest():
    """参数空间必须先于回测固定。空网格意味着「边跑边调」，不可接受。"""
    for c in R.CARDS:
        if c.id in {"S25", "S26", "S30"}:
            continue
        assert c.params, f"{c.id} 缺参数网格"
        for k, v in c.params.items():
            assert isinstance(v, list) and v, f"{c.id}.{k} 必须是预先固定的列表"


# ---------------------------------------------------------------------------
# 2. 结构层因果性（截断测试）
# ---------------------------------------------------------------------------

def test_structure_is_causal_under_truncation():
    """截断到 2/3 后，前半段的 pivot / FVG / 结构事件必须完全一致。

    这是证明「没有未来函数」的唯一机械方法。任何函数只要读了 bar t 之后
    的数据，截断就会改变 t 之前的结果。
    """
    cs = load("BTCUSDT_4h.csv")
    cut = len(cs) * 2 // 3
    full, half = cs, cs[:cut]

    pf, ph = S.pivots(full, 5, 5), S.pivots(half, 5, 5)
    assert [(p.i, p.kind, p.confirmed_at) for p in pf if p.confirmed_at < cut] \
        == [(p.i, p.kind, p.confirmed_at) for p in ph]

    ff = S.find_fvgs(full, min_gap_atr=0.5)
    fh = S.find_fvgs(half, min_gap_atr=0.5)
    assert [(f.i, f.direction, round(f.lo, 8)) for f in ff if f.t <= half[-1]["t"]] \
        == [(f.i, f.direction, round(f.lo, 8)) for f in fh]

    ef = S.structure_events(full, pf)
    eh = S.structure_events(half, ph)
    assert [(e.t, e.kind, e.direction, round(e.level, 8))
            for e in ef if e.t < cut] \
        == [(e.t, e.kind, e.direction, round(e.level, 8)) for e in eh]


def test_pivot_confirmation_delay_is_positive():
    """pivot 只能在右侧 K 线收盘后确认，确认时间必须严格晚于自身位置。"""
    cs = load("BTCUSDT_4h.csv")[:2000]
    for p in S.pivots(cs, 5, 5):
        assert p.confirmed_at == p.i + 5 or p.confirmed_at > p.i


def test_fvg_is_confirmed_on_third_bar_not_first():
    """FVG 成立时间是第 3 根收盘，不能提前。"""
    cs = load("BTCUSDT_4h.csv")[:3000]
    for f in S.find_fvgs(cs, min_gap_atr=0.0)[:200]:
        # f.t 必须是 bar i+1 的时间，不是 bar i-1
        assert cs[f.i + 1]["t"] == f.t


def test_order_block_confirmed_after_impulse_completes():
    """OB 要等位移走完才可用，不能反向回指。"""
    cs = load("BTCUSDT_4h.csv")[:3000]
    for o in S.find_order_blocks(cs)[:200]:
        assert o.confirmed_at > o.i, f"OB@{o.i} 确认时间必须晚于其所在 bar"
        assert o.confirmed_at > 0


# ---------------------------------------------------------------------------
# 3. 时间戳 / 下标混用（本轮最贵的 bug）
# ---------------------------------------------------------------------------

def test_fvg_signals_are_not_silently_zero():
    """S03/S04 必须有信号。

    第一版 `by_t.get(i)` 用时间戳做键、下标做查询，两者永远不相等，
    于是所有 FVG 卡静默返回零信号——不报错、不警告，只在结果里显示
    「NO_TRADES」，极容易被误读成「这个策略没机会」。
    """
    cs = load("BTCUSDT_4h.csv")
    cache = C.Cache(cs)
    for name, fn in (("S03-edge", C.s03_fvg_retest(cache, entry_at="edge")),
                     ("S03-mid", C.s03_fvg_retest(cache, entry_at="mid")),
                     ("S04-ifvg", M.s04_ifvg(cache))):
        n = sum(1 for i in range(len(cs)) if fn(i, {"i": i, "candles": cs,
                                                    "bar": cs[i]}))
        assert n > 0, f"{name} 零信号——大概率是下标/时间戳混用"


def test_cache_index_does_not_leak_across_instances():
    """两个 Cache 实例的索引必须是不同对象，且各自指向自己的 K 线。

    模块级 `dict[id(cache)]` 在对象回收后会被新对象复用同一个 id，
    导致第二个币命中第一个币的索引。症状是「单跑正常、批量全零」。

    注意：BTC 与 ETH 的**时间戳完全一样**（都从 2020-09-01 开始），
    所以不能靠「查不到对方的时间」来判断泄漏——那会误报。正确的判据是
    索引必须落在自己的 candles 上。
    """
    cs1 = load("BTCUSDT_4h.csv")[:500]
    cs2 = load("ETHUSDT_4h.csv")[:500]
    c1 = C.Cache(cs1)
    i1 = c1.idx(cs1[100]["t"])
    assert i1 == 100
    assert c1.candles is cs1

    c2 = C.Cache(cs2)
    i2 = c2.idx(cs2[200]["t"])
    assert i2 == 200
    assert c2.candles is cs2, "第二个 Cache 的 candles 被串改"

    # 索引必须能反查回自己那份数据里的同一根 K 线
    assert c2.candles[i2]["t"] == cs2[200]["t"]
    assert c2._t2i is not c1._t2i, "两个实例共享了同一个索引 dict"

    # BTC 与 ETH 同一时刻价格不同，用这个区分索引指向
    t = cs1[100]["t"]
    assert c1.candles[c1.idx(t)]["c"] == cs1[100]["c"]
    assert c2.candles[c2.idx(t)]["c"] == cs2[100]["c"]
    assert cs1[100]["c"] != cs2[100]["c"], "测试前提：两币价格应不同"


# ---------------------------------------------------------------------------
# 4. 执行口径
# ---------------------------------------------------------------------------

def _synthetic_candles(n=60, price=100.0):
    """构造一段平盘合成数据，用于精确校验成交价与顺序。

    长度必须 >= 引擎的 50 根门槛。这个门槛是刻意的安全限制（数据太短时
    ATR 与结构识别都不可靠），不该为了测试方便而放宽。
    """
    out = []
    for k in range(n):
        out.append({"t": k * 3600_000, "o": price, "h": price + 1,
                    "l": price - 1, "c": price + 0.5, "v": 1000.0})
    return out


def test_entry_is_delayed_one_bar_never_same_bar():
    """信号在 bar i 收盘产生，成交必须在 bar i+1 开盘，绝不可是当根收盘。"""
    cs = _synthetic_candles()
    fired = {"n": 0}

    def fn(i, ctx):
        if i == 3 and fired["n"] == 0:
            fired["n"] += 1
            return {"side": "long", "stop": 50.0, "target": None,
                    "limit": None, "atr": 1.0}
        return None

    r = E.run(cs, card="T", symbol="X", timeframe="1h", params={},
              signal_fn=fn, bar_hours=1.0, max_hold_bars=3)
    assert r.n_trades == 1
    tr = r.trades[0]
    assert tr.entry_i == 4, "入场必须延后到信号后一根"
    assert tr.entry_price == cs[4]["o"], "入场价必须是下一根开盘价"


def test_same_bar_stop_wins_over_target():
    """同一根 K 线同时触及止损与止盈时，必须按止损算（保守口径）。

    bar 只给 OHLC，不知道盘中先后。按止盈先到算会让每一笔都变好，
    是回测虚高最常见的来源之一。
    """
    cs = _synthetic_candles()
    # 在第 2 根制造一根同时穿越止损（95）与止盈（110）的 K 线
    cs[2] = {"t": 2 * 3600_000, "o": 100, "h": 111, "l": 94, "c": 100, "v": 1}
    calls = {"n": 0}

    def fn(i, ctx):
        if i == 0 and calls["n"] == 0:
            calls["n"] += 1
            return {"side": "long", "stop": 95.0, "target": 110.0,
                    "limit": None, "atr": 1.0}
        return None

    r = E.run(cs, card="T", symbol="X", timeframe="1h", params={},
              signal_fn=fn, bar_hours=1.0, max_hold_bars=10)
    assert r.n_trades == 1
    assert r.trades[0].exit_reason == "sl"
    assert r.trades[0].exit_price == 95.0


def test_costs_are_separated_not_bundled():
    """手续费、滑点、资金费必须分开统计，混在一起就没法做敏感性分析。"""
    cs = _synthetic_candles()
    calls = {"n": 0}

    def fn(i, ctx):
        if i == 2 and calls["n"] == 0:
            calls["n"] += 1
            return {"side": "long", "stop": 90.0, "target": None,
                    "limit": None, "atr": 1.0}
        return None

    r = E.run(cs, card="T", symbol="X", timeframe="1h", params={},
              signal_fn=fn, bar_hours=1.0, max_hold_bars=2,
              funding_per_bar=0.0001)
    assert r.n_trades == 1
    tr = r.trades[0]
    assert tr.fee_pct > 0
    assert tr.slip_pct > 0
    assert tr.funding_pct > 0
    # 净收益必须等于毛收益减三项成本
    assert abs(tr.net_pct - (tr.gross_pct - tr.fee_pct - tr.slip_pct
                             - tr.funding_pct)) < 1e-9


def test_sharpe_uses_calendar_days_not_per_trade():
    """Sharpe 必须按日历日聚合成净值曲线，不能「每笔收益除以笔数」。

    后者把挤在同一天的 100 笔和分散在 5 年的 100 笔当作同样的风险，
    会把高频策略的 Sharpe 算得虚高。
    """
    from engine import Trade, stats
    day = 86_400_000
    trades = []
    for k in range(20):
        t = Trade(card="T", symbol="X", side="long", entry_t=k * day,
                  entry_price=100.0, entry_i=k, exit_i=k + 1)
        t.exit_t = (k + 1) * day
        t.gross_pct = 1.0
        t.net_pct = 1.0
        trades.append(t)
    s = stats(trades, 24.0)
    # 20 天涨 20%，年化应该在 252% 附近，而不是 20%×252
    assert 100 < s["ann_return_pct"] < 400


# ---------------------------------------------------------------------------
# 5. 全部卡的信号函数都能被导入且不抛异常
# ---------------------------------------------------------------------------

def test_all_registered_cards_have_a_signal_factory():
    """除 DATA_BLOCKED 的三张外，每张卡都必须有可执行的工厂函数。"""
    import runner
    for c in R.CARDS:
        factory, _, _ = runner.CARDS[c.id]
        if c.id in {"S25", "S26", "S30"}:
            assert factory is None, f"{c.id} 应该是 DATA_BLOCKED"
        else:
            assert factory is not None, f"{c.id} 缺信号实现"


def test_runner_declares_tf_incompatibility_explicitly():
    """周期不兼容必须显式声明，不能靠「跑出零信号」暗示。"""
    import runner
    assert ("S09", "4h") in runner.TF_INCOMPATIBLE
    assert "小时窗口" in runner.TF_INCOMPATIBLE[("S09", "4h")]


# ---------------------------------------------------------------------------
# 6. 实现重复检测（S15 与 S29 曾经一模一样）
# ---------------------------------------------------------------------------

def test_s15_and_s29_are_not_the_same_implementation():
    """S15（两段式假突破）与 S29（单根刺穿收回）必须是两张不同的卡。

    第一版两者都写成 `bar["h"] > hi and bar["c"] < hi`，导致 2588 笔成交、
    每一个统计量完全相同（netExp −0.34 / ann −32.8% / PF 0.81 / 一致度 0%）。
    数字完全一致是「实现重复」的铁证——真正的独立假设不可能对得这么齐。
    """
    cs = load("BTCUSDT_4h.csv")
    cache = C.Cache(cs)
    f15 = M.s15_false_breakout(cache)
    f29 = M.s29_turtle_soup(cache)
    s15 = {i for i in range(len(cs)) if f15(i, {"i": i, "candles": cs, "bar": cs[i]})}
    s29 = {i for i in range(len(cs)) if f29(i, {"i": i, "candles": cs, "bar": cs[i]})}

    assert s15, "S15 零信号"
    assert s29, "S29 零信号"
    assert s15 != s29, "S15 与 S29 产生了完全相同的信号集"
    # 背离比例必须显著：两者只应在「同一根既收盘突破又收回」时重合
    overlap = len(s15 & s29) / len(s15 | s29)
    assert overlap < 0.5, f"Jaccard {overlap:.2f} 过高，两个假设几乎重合"


def test_no_two_cards_produce_identical_trade_counts():
    """任何两张卡在同一币同一周期上都不该给出完全相同的成交笔数。

    这条是「实现重复」的粗筛。不同假设在 4h × 6 年上撞出同一个笔数
    概率极低，一旦发生基本可以断定是复制粘贴。
    """
    import runner
    cs = load("BTCUSDT_4h.csv")
    counts: dict[int, list[str]] = {}
    for card, (factory, _, mh) in runner.CARDS.items():
        if factory is None:
            continue
        combos = runner.resolve_grid(card, "4h")
        if not combos:
            continue
        prm = combos[0]
        cache = C.Cache(cs)
        try:
            fn = factory(cache, prm)
        except TypeError:
            continue
        if isinstance(fn, tuple):
            continue
        r = E.run(cs, card=card, symbol="BTCUSDT", timeframe="4h",
                  params=prm, signal_fn=fn, bar_hours=4.0, max_hold_bars=mh)
        if r.n_trades > 100:
            counts.setdefault(r.n_trades, []).append(card)
    dup = {n: cs_ for n, cs_ in counts.items() if len(cs_) > 1}
    assert not dup, f"笔数完全相同的卡：{dup}"


# ---------------------------------------------------------------------------
# 7. 信号函数是有状态的，不能跨回测复用
# ---------------------------------------------------------------------------

def test_signal_function_must_be_rebuilt_per_run():
    """复用同一个信号函数跑两轮，结果必须不同——所以每轮都要重建。

    S03 内部维护 `live` / `seen`，跑到末尾时状态已经走完。直接拿它再跑
    一轮，信号流彻底不同。第一版成本敏感性脚本没重建，得到「成本越高
    收益越好」（S03 零成本 0.240 → 真实成本 0.657，衰减 −174%）这种
    不可能的结果。荒谬数字往往是状态污染，不是策略特性。

    这个测试把「必须重建」这件事钉死：如果有人想优化掉 `Cache(cs)` 的
    重建，这里会炸。
    """
    cs = load("BTCUSDT_4h.csv")
    cache = C.Cache(cs)
    fn = C.s03_fvg_retest(cache, entry_at="edge")

    r1 = E.run(cs, card="S03", symbol="BTCUSDT", timeframe="4h", params={},
               signal_fn=fn, bar_hours=4.0, max_hold_bars=60)
    r2 = E.run(cs, card="S03", symbol="BTCUSDT", timeframe="4h", params={},
               signal_fn=fn, bar_hours=4.0, max_hold_bars=60)

    # 复用会得到不同结果，证明状态确实残留
    assert (r1.n_trades, round(r1.net_expectancy, 4)) != \
           (r2.n_trades, round(r2.net_expectancy, 4)), \
        "信号函数似乎无状态——若真改成无状态，可放宽本测试"

    # 重建后必须与第一次完全一致
    fn2 = C.s03_fvg_retest(C.Cache(cs), entry_at="edge")
    r3 = E.run(cs, card="S03", symbol="BTCUSDT", timeframe="4h", params={},
               signal_fn=fn2, bar_hours=4.0, max_hold_bars=60)
    assert r3.n_trades == r1.n_trades
    assert abs(r3.net_expectancy - r1.net_expectancy) < 1e-9


def test_cost_sensitivity_script_rebuilds_per_tier():
    """成本敏感性脚本不能复用信号函数，否则结论方向会错。"""
    src = (CARDS / "cost_sensitivity.py").read_text(encoding="utf-8")
    # 必须在成本循环内部重建
    assert "for name, fee, slip in COST_TIERS:" in src
    body = src.split("for name, fee, slip in COST_TIERS:")[1]
    inner = body.split("if counts is None:")[0]
    assert "Cache(cs)" in inner, "成本循环内必须新建 Cache"
    assert "factory(cache)" in inner, "成本循环内必须重建信号函数"

# ---------------------------------------------------------------------------
# 8. Cache 只装与参数无关的结构索引，可跨参数复用
# ---------------------------------------------------------------------------

def test_cache_reuse_across_params_is_identical_to_fresh_cache():
    """复用 Cache 跑不同参数，结果必须与每次新建 Cache 完全一致。

    15m 上单次 Cache 构建要 16 秒（21.8 万根），132 个配置各建一次就是
    107 分钟，其中 106 分钟在重复算同一件事。`Cache` 装的是 pivot / FVG /
    OB / sweep / range，构造参数 `left=5, right=5, min_gap_atr=0.5` 全固定，
    与卡的参数网格无关，所以理应能复用。

    **但复用的安全前提必须被守住，不能靠「看起来没问题」**：
      · 信号函数不可写 cache（`signals_*.py` 无 `cache.x = ...` 赋值）
      · 每个配置仍必须重建信号函数（见上一个测试）

    4h 上实测 132 配置 × 全字段差异 0 条。这里用两个参数组合做等价断言，
    防止未来有人把某个可变状态塞进 Cache，让批量结果悄悄错位。
    """
    cs = load("BTCUSDT_4h.csv")
    shared = C.Cache(cs)

    for prm in ({"channel": 20, "atr_mult": 2.0},
                {"channel": 55, "atr_mult": 2.0}):
        r_shared = E.run(cs, card="S20", symbol="BTCUSDT", timeframe="4h",
                         params=prm,
                         signal_fn=C.s20_donchian(shared, **prm),
                         bar_hours=4.0, max_hold_bars=200)
        r_fresh = E.run(cs, card="S20", symbol="BTCUSDT", timeframe="4h",
                        params=prm,
                        signal_fn=C.s20_donchian(C.Cache(cs), **prm),
                        bar_hours=4.0, max_hold_bars=200)
        assert r_shared.n_trades == r_fresh.n_trades, prm
        assert abs(r_shared.net_expectancy - r_fresh.net_expectancy) < 1e-9
        assert abs(r_shared.sharpe - r_fresh.sharpe) < 1e-9

    # 反证：复用 Cache 不等于复用信号函数。
    # 注意这里必须用**有状态**的卡（S03 维护 live/seen）。S20 是无状态的
    # ——每次调用从 cache.candles 现算 Donchian 通道，没有残留状态，所以
    # 拿它做反证会假失败（两轮本来就该一致）。
    s03 = C.s03_fvg_retest(shared, entry_at="edge")
    a = E.run(cs, card="S03", symbol="BTCUSDT", timeframe="4h",
              params={}, signal_fn=s03, bar_hours=4.0, max_hold_bars=60)
    b = E.run(cs, card="S03", symbol="BTCUSDT", timeframe="4h",
              params={}, signal_fn=s03, bar_hours=4.0, max_hold_bars=60)
    assert (a.n_trades, round(a.net_expectancy, 6)) != \
           (b.n_trades, round(b.net_expectancy, 6)), \
        "S03 信号函数似乎已无状态——若真改成无状态，可放宽本测试"
