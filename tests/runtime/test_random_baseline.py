"""R0 随机基线：口径一致性、可复现性、对照语义。

这些测试保护的是**方法论**而不是某个数字：
  - 随机基线用的撮合必须与 E5 完全同口径（否则对照无意义）；
  - 同种子必须复现（否则 p 值不可审计）；
  - matched 口径必须真的守住多空比（否则"控制方向分布"是假的）；
  - R2 必须是真的全反向（不能偷偷保留原方向）。
"""
from decimal import Decimal as D

import pytest

from research.experiment.baseline import (Prepared, R0Spec, aggregate_bars,
                                          atr_series, cost_sensitivity,
                                          e5_params, fourh_params, mc_r0,
                                          mc_r1, multiple_testing,
                                          overlap_stats, quantile,
                                          reverse_test, risk_adjusted, run_book,
                                          scale_costs, spec_4h, summarize,
                                          wilson_ci)
from research.experiment.funding_exp import (_atr14, book_stats,
                                              run_funding_variant)
from research.market.contracts import Candle

STEP = 3_600_000
START = (1_790_000_000_000 // STEP) * STEP


def _bars(n=400, base=59000.0):
    out = []
    px = base
    for i in range(n):
        nxt = px * (1.0012 if i % 3 else 0.9988)
        out.append(Candle(START + i * STEP, D(str(px)),
                          D(str(max(px, nxt) + 8)),
                          D(str(min(px, nxt) - 8)), D(str(nxt)), D("10"), STEP))
        px = nxt
    return out


def _funding(n=300, rate="0.0001", spikes=()):
    out = []
    base = float(rate)
    for i in range(n):
        r = "0.004" if i in spikes else str(base * (1.0 + 0.25 * ((i % 5) - 2)))
        out.append({"funding_ms": START + i * 8 * STEP, "rate": r,
                    "mark_price": "59000"})
    return out


@pytest.fixture(scope="module")
def prep():
    bars = _bars(1200)
    fd = _funding(150, spikes=(60, 90, 120))
    spec = R0Spec(n_mc=12, seed=7)
    return Prepared(bars, fd, spec), spec


# ---------------------------------------------------------------- 口径一致性
def test_atr_series_matches_rolling_atr():
    bars = _bars(120)
    series = atr_series(bars)
    for i in range(14, len(bars)):
        assert series[i] == _atr14(bars, i), f"ATR 在第 {i} 根不一致"


def test_atr_series_zero_before_period():
    bars = _bars(40)
    series = atr_series(bars)
    assert all(series[i] == D("0") for i in range(14))


def test_run_book_matches_run_funding_variant(prep):
    """随机基线的组合回测必须与 E5 回测给出完全相同的净值。"""
    prepared, spec = prep
    p = e5_params(spec, z_threshold=D("1.5"))
    ctxs = prepared.contexts("delta", D("1.5"))
    sides = [c["default_side"] for c in ctxs]
    book = run_book(prepared, p, ctxs, sides)
    ref = run_funding_variant(prepared.bars, _funding(150, spikes=(60, 90, 120)),
                              p)
    assert book["all"]["net"] == ref["all"]["net"]
    assert book["filled"] == ref["all"]["n"]


def test_run_book_rejects_length_mismatch(prep):
    prepared, spec = prep
    ctxs = prepared.contexts(spec.e5_mode, spec.e5_z)
    if not ctxs:
        pytest.skip("合成数据未产生信号")
    with pytest.raises(ValueError):
        run_book(prepared, e5_params(spec), ctxs, ["LONG"])


def test_single_position_constraint_can_skip(prep):
    """单仓约束必须真的会挡单，且 attempted/filled 要如实分开。"""
    prepared, spec = prep
    ctxs = prepared.contexts(spec.e5_mode, spec.e5_z)
    if len(ctxs) < 2:
        pytest.skip("样本不足")
    sides = ["LONG"] * len(ctxs)
    on = run_book(prepared, e5_params(spec), ctxs, sides, single_position=True)
    off = run_book(prepared, e5_params(spec), ctxs, sides, single_position=False)
    assert on["attempted"] == off["attempted"] == len(ctxs)
    assert on["filled"] == off["filled"] + on["skipped_overlap"]
    assert off["filled"] == len(ctxs)


# ---------------------------------------------------------------- 可复现性
def test_mc_r0_is_reproducible(prep):
    prepared, spec = prep
    a = mc_r0(prepared, spec, n_mc=8, seed=99)
    b = mc_r0(prepared, spec, n_mc=8, seed=99)
    assert a["research_net"] == b["research_net"]
    assert [r["research"] for r in a["records"]] == [r["research"] for r in b["records"]]


def test_mc_r0_different_seed_differs(prep):
    prepared, spec = prep
    a = mc_r0(prepared, spec, n_mc=8, seed=1)
    b = mc_r0(prepared, spec, n_mc=8, seed=2)
    assert ([r["research"] for r in a["records"]]
            != [r["research"] for r in b["records"]])


# ---------------------------------------------------------------- 对照语义
def test_mc_r0_matched_preserves_side_ratio(prep):
    """matched 口径只换"谁多谁空"，多空总数必须与真实一致。"""
    prepared, spec = prep
    ctxs = prepared.contexts(spec.e5_mode, spec.e5_z)
    if len(ctxs) < 2:
        pytest.skip("样本不足")
    n_long = sum(1 for c in ctxs if c["default_side"] == "LONG")
    out = mc_r0(prepared, spec, n_mc=6, matched=True)
    assert out["real"]["signal_long"] == n_long
    assert out["real"]["signal_short"] == len(ctxs) - n_long


def test_mc_r0_indep_changes_sides(prep):
    """独立随机必须真的会翻方向，不能退化成原样。"""
    prepared, spec = prep
    ctxs = prepared.contexts(spec.e5_mode, spec.e5_z)
    if len(ctxs) < 3:
        pytest.skip("样本不足")
    out = mc_r0(prepared, spec, n_mc=12)
    nets = {r["all"] for r in out["records"]}
    assert len(nets) > 1, "12 次随机方向全同 => 方向没有随机化"


def test_r1_matches_trade_count(prep):
    prepared, spec = prep
    out = mc_r1(prepared, spec, n_mc=4, target_n=5, n_long=3, n_short=2, seed=3)
    assert out["target_n"] == 5
    assert all(r["n"] == 5 for r in out["records"]), \
        "R1 必须匹配交易笔数，否则对照不成立"


def test_r2_is_exact_reverse(prep):
    """R2 每一笔的方向都必须是原文的反面。"""
    prepared, spec = prep
    ctxs = prepared.contexts(spec.e5_mode, spec.e5_z)
    if not ctxs:
        pytest.skip("样本不足")
    p = e5_params(spec)
    fwd = run_book(prepared, p, ctxs, [c["default_side"] for c in ctxs])
    rev = reverse_test(prepared, spec)
    assert rev["filled"] <= fwd["filled"] + len(ctxs)
    flip = {"LONG": "SHORT", "SHORT": "LONG"}
    expect = [flip[c["default_side"]] for c in ctxs]
    got = run_book(prepared, p, ctxs, expect)["trades"]
    assert [t["side"] for t in got] == [t["side"] for t in rev["trades"]]


# ---------------------------------------------------------------- 成本
def test_cost_scaling_identity_at_frozen_round_trip():
    spec = R0Spec()
    fee, slip = scale_costs(spec, spec.round_trip_bps)
    assert fee == spec.fee_bps and slip == spec.slippage_bps


def test_cost_scaling_zero():
    spec = R0Spec()
    fee, slip = scale_costs(spec, D("0"))
    assert fee == 0 and slip == 0


def test_cost_sensitivity_monotonic(prep):
    """成本越高净值必须越低——否则成本没接进撮合。"""
    prepared, spec = prep
    out = cost_sensitivity(prepared, spec, (D("0"), D("18"), D("30")))
    nets = [D(r["net"]) for r in out["scenarios"]]
    assert nets[0] >= nets[1] >= nets[2]
    assert out["scenarios"][0]["is_assumption"] is True      # 0bps 是假设
    assert out["scenarios"][1]["is_assumption"] is False     # 18bps 是实测


# ---------------------------------------------------------------- 统计
def test_wilson_ci_bounds():
    lo, hi = wilson_ci(0, 100)
    assert 0.0 <= lo <= hi <= 1.0
    assert hi < 0.05        # 0/100 的上界应小于 5%
    lo2, hi2 = wilson_ci(50, 100)
    assert lo2 < 0.5 < hi2


def test_quantile_linear():
    vals = [0.0, 10.0]
    assert quantile(vals, 0.0) == 0.0
    assert quantile(vals, 1.0) == 10.0
    assert quantile(vals, 0.5) == 5.0


def test_summarize_counts_ties_as_better():
    """p 用 >= 口径：与真实打平也算"随机不输"，偏保守。"""
    out = summarize([1.0, 2.0, 3.0, 4.0], 3.0)
    assert out["n_better_or_equal"] == 2
    assert out["p_value"] == 0.5
    assert out["real_percentile"] == 50.0


def test_summarize_empty():
    assert summarize([], 1.0) == {"n": 0}


# ---------------------------------------------------------------- 重叠与风险
def test_overlap_detects_concurrency():
    trades = [
        {"entry_ts_ms": 0, "exit_ts_ms": 100, "holding_ms": 100},
        {"entry_ts_ms": 50, "exit_ts_ms": 150, "holding_ms": 100},
        {"entry_ts_ms": 500, "exit_ts_ms": 600, "holding_ms": 100},
    ]
    out = overlap_stats(trades)
    assert out["n"] == 3
    assert out["overlap_pairs"] == 1
    assert out["max_concurrent"] == 2


def test_overlap_no_trades():
    assert overlap_stats([]) == {"n": 0}


def test_risk_adjusted_needs_two_trades():
    assert risk_adjusted([{"net_pnl": "1"}])["n"] == 1
    out = risk_adjusted([{"net_pnl": "1"}, {"net_pnl": "-0.5"}])
    assert out["n"] == 2
    assert out["max_dd_pct"] > 0


# ---------------------------------------------------------------- 多重检验
def test_multiple_testing_bonferroni():
    out = multiple_testing({"a": 0.01, "b": 0.02, "c": 0.03})
    assert out["families"] == 3
    assert out["detail"]["a"]["bonferroni"] == 0.03
    assert out["detail"]["c"]["bonferroni"] == 0.09


def test_multiple_testing_fdr_monotone():
    out = multiple_testing({"a": 0.001, "b": 0.01, "c": 0.04, "d": 0.2})
    vals = [out["detail"][k]["bh_fdr"] for k in ("a", "b", "c", "d")]
    assert vals == sorted(vals), "BH-FDR 必须随原始 p 单调不减"


def test_book_stats_empty():
    assert book_stats([]) == {"n": 0}


# ---------------------------------------------------------------- 4h 聚合
def test_aggregate_bars_ohlc_is_correct():
    # 起点必须对齐 4h 边界；不对齐时 aggregate_bars 会丢弃开头（见下一个用例）
    ms4 = STEP * 4
    start = START + (ms4 - START % ms4) % ms4
    bars, px = [], 59000.0
    for i in range(16):
        nxt = px * (1.0012 if i % 3 else 0.9988)
        bars.append(Candle(start + i * STEP, D(str(px)),
                           D(str(max(px, nxt) + 8)), D(str(min(px, nxt) - 8)),
                           D(str(nxt)), D("10"), STEP))
        px = nxt
    big = aggregate_bars(bars, 4)
    assert len(big) == 4
    for i, b in enumerate(big):
        chunk = bars[i * 4:(i + 1) * 4]
        assert b.open == chunk[0].open
        assert b.close == chunk[-1].close
        assert b.high == max(c.high for c in chunk)
        assert b.low == min(c.low for c in chunk)
        assert b.open_ms == chunk[0].open_ms
        assert b.timeframe_ms == STEP * 4


def test_aggregate_bars_factor_one_is_identity():
    bars = _bars(10)
    assert aggregate_bars(bars, 1) == bars


def test_aggregate_bars_aligns_to_boundary():
    """起点不对齐时必须丢弃开头不足一个周期的部分，而不是造出非法 Candle。"""
    bars = _bars(12)
    # 从第 1 根开始，open_ms 不再对齐 4×STEP
    shifted = bars[1:]
    big = aggregate_bars(shifted, 4)
    assert big, "应至少产出一根合法 K 线"
    for b in big:
        assert int(b.open_ms) % (STEP * 4) == 0


def test_spec_4h_defaults():
    s = spec_4h()
    assert s.interval == "4h" and s.step_ms == 14_400_000
    assert s.version.startswith("R0-4h")


def test_fourh_params_are_prior_not_optimized():
    """4h 主口径必须是先验中心参数，不能偷偷用 1h 的最优参数。"""
    spec = spec_4h()
    p = fourh_params(spec)
    assert (p.stop_atr, p.trail_atr, p.trail_start_atr, p.hold_bars) == (
        D("2.0"), D("1.0"), D("1.0"), 24)
    e5 = e5_params(spec)
    assert (e5.stop_atr, e5.trail_atr) == (D("1.5"), D("0.75")), \
        "1h 的最优参数不应被搬到 4h"


def test_fourh_hold_covers_multiple_days():
    """hold 24 根 4h = 4 天；这是跨周期可比的前提。"""
    p = fourh_params(spec_4h())
    assert p.hold_bars * 4 * 3600_000 == 4 * 86_400_000
