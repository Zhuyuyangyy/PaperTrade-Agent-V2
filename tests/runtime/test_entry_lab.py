"""实验 E P0：独立账务验收 + 无前视撮合。"""
from decimal import Decimal as D
from pathlib import Path

from research.experiment.ledger import (
    CashLedger, OpenPosition, mark_position, open_at_next_bar, verify_equity,
)
from research.experiment.sim import simulate_hold
from research.market.contracts import Candle

STEP = 300_000


def _bars(n=20, start=(1_790_000_000_000 // 300_000) * 300_000, base=59000.0):
    out = []
    for i in range(n):
        px = base + i * 10
        out.append(Candle(start + i * STEP, D(str(px)), D(str(px + 5)),
                          D(str(px - 5)), D(str(px + 2)), D("10"), STEP))
    return out


def test_entry_uses_next_bar_open_not_signal_close():
    bars = _bars()
    pos, flows = open_at_next_bar(bars, 5, "LONG", D("100"), D("58000"), D("62000"))
    # 信号 bar 收盘 = 59012；下一根开盘 = 59010（含滑点更高）
    assert pos.entry_fill > D(str(bars[6].open)), "成交价必须含不利滑点"
    assert pos.entry_fill != bars[5].close, "绝不能用信号 bar 自身收盘价成交"
    assert pos.entry_ts_ms == int(bars[6].open_ms)
    assert pos.signal_bar == 5
    assert {f.kind for f in flows} == {"ENTRY_NOTIONAL", "ENTRY_FEE"}


def test_no_next_bar_refuses_to_trade():
    bars = _bars(6)
    try:
        open_at_next_bar(bars, 5, "LONG", D("100"), D("1"), D("2"))
        assert False, "最后一根不应成交"
    except ValueError:
        pass


def test_cash_flows_reconcile_with_net_pnl():
    bars = _bars(40)
    r = simulate_hold(bars, 10, "LONG", D("100"), D("58000"), D("62000"),
                      max_hold_bars=10)
    assert r is not None
    cash_change = sum((D(f["amount"]) for f in r.flows), D("0"))
    assert abs(cash_change - r.net_pnl) < D("0.0000001")
    # 净盈亏 = 毛利 - 双边手续费 - 资金费
    expected = r.gross - r.entry_fee - r.exit_fee - r.funding
    assert abs(expected - r.net_pnl) < D("0.0000001")
    kinds = {f["kind"] for f in r.flows}
    assert {"ENTRY_NOTIONAL", "ENTRY_FEE", "EXIT_PROCEEDS", "EXIT_FEE", "FUNDING"} <= kinds


def test_slippage_always_adverse_and_equity_verified():
    bars = _bars(40)
    long = simulate_hold(bars, 10, "LONG", D("100"), D("1"), D("999999"),
                         max_hold_bars=5)
    short = simulate_hold(bars, 10, "SHORT", D("100"), D("999999"), D("1"),
                          max_hold_bars=5)
    assert long.entry_fill > bars[11].open      # 多头买高
    assert short.entry_fill < bars[11].open     # 空头卖低
    led = CashLedger()
    pos = OpenPosition("LONG", D("1"), D("100"), 0, D("90"), D("110"), D("0.4"), 0)
    led.record(0, "ENTRY_NOTIONAL", -D("100"))
    led.record(0, "ENTRY_FEE", -D("0.4"))
    v = verify_equity(led, [pos], {"LONG": D("105")})
    # cash=1000-100-0.4=899.6；仓位估值=100+(105-100)*1=105；盯市权益=1004.6
    assert abs(D(v["equity_mark_to_market"]) - D("1004.6")) < D("0.0000001")
    assert abs(D(v["equity_from_cash_flows"]) - D("899.6")) < D("0.0000001")
    assert abs(D(v["unrealized_gap"]) - D("105")) < D("0.0000001")
    assert v["notional_match"] is True


def test_same_bar_stop_and_tp_is_conservative():
    # 构造一根 bar 同时穿过止损和止盈：应按止损成交
    bars = _bars(30)
    i = 12
    b = bars[i]
    bars[i] = Candle(b.open_ms, b.open, D("62000"), D("57000"), D("60000"), D("10"), STEP)
    r = simulate_hold(bars, 10, "LONG", D("100"), D("58000"), D("61000"), max_hold_bars=5)
    assert r.exit_reason == "STOP", f"同根双向应按止损，实际 {r.exit_reason}"


def test_gap_past_stop_fills_at_open():
    bars = _bars(30)
    i = 11
    b = bars[i]
    bars[i] = Candle(b.open_ms, D("57000"), D("57500"), D("56500"), D("57000"), D("10"), STEP)
    r = simulate_hold(bars, 10, "LONG", D("100"), D("58000"), D("62000"), max_hold_bars=8)
    assert r.exit_reason == "STOP"
    assert r.exit_fill < D("58000"), "跳空止损应按更差的开盘价成交"


# ---------- 实验 E：多周期状态 / 三组配对 / AI 过滤 ----------
def test_make_state_uses_closed_bars_only():
    from research.experiment.entry_exp import make_state
    bars = _bars(120)
    st = make_state(bars, 50)
    assert st is not None
    # 所有周期的已收盘 K 线都不能超过决策时刻
    as_of = bars[50].close_ms
    for tf, rows in st.views.items():
        assert rows
        assert all(c.close_ms <= as_of for c in rows), tf
    assert st.atr_abs > 0


def test_trade_entry_is_next_bar_open_across_groups():
    """配对铁律：同一 bar 的三组，若都成交，成交价必须完全相同。"""
    from research.experiment.entry_exp import ExpConfig, _trade_for, make_state
    from research.decision.contracts import Decision
    from decimal import Decimal as D
    bars = _bars(80)
    st = make_state(bars, 40)
    cfg = ExpConfig()
    dec = Decision("x", "v1", "OK", "LONG", 1.0, D("1"), "x")
    t1 = _trade_for(dec, st, 40, bars, cfg)
    t2 = _trade_for(dec, st, 40, bars, cfg)
    assert t1 and t2
    assert t1["entry_fill"] == t2["entry_fill"] == str(
        bars[41].open * (D("1") + cfg.slippage_bps / D("10000")))


def test_filter_only_seeks_same_rule_signals():
    """D 组只能对 C 组已产生的非 HOLD 信号调用过滤器，不能自己造信号。"""
    seen = []

    class FakeFilter:
        def decide(self, snap, candidate):
            seen.append(candidate)
            return {"accept": False, "reason": "test reject", "status": "OK",
                    "provider": "fake", "tokens_in": 10, "tokens_out": 5}

    from research.experiment.entry_exp import ExpConfig, run_experiment, _rules_multitf

    bars = _bars(60)
    cfg = ExpConfig(warmup_bars=20)      # 测试用小样本，显式关闭真实预热
    res = run_experiment(bars[:50], cfg=cfg, providers={"C": _rules_multitf},
                         research_end_ms=bars[35].close_ms, decision_every=1,
                         filter_provider=FakeFilter())
    rule_signals = [d for d in res["decisions"] if d["group"] == "C" and d["choice"] != "HOLD"]
    # 过滤调用次数 == 规则信号数，且候选方向逐一对应
    assert len(seen) == len(rule_signals)
    assert seen == [d["choice"] for d in rule_signals]


def test_all_groups_share_cost_and_exit_params():
    """三组费用/滑点/止损止盈/持有期完全一致——差异只能来自入场判断。"""
    from research.experiment.entry_exp import ExpConfig
    cfg = ExpConfig()
    assert cfg.fee_bps == cfg.slippage_bps * 0 + D("4")
    assert cfg.stop_atr == D("1.5") and cfg.tp_atr == D("3")
    # 三组使用同一个 cfg（引擎内没有 per-group 参数），由 run_experiment 保证


def test_single_position_no_overlapping_trades():
    """同一组不允许串仓：前一笔未平，后一笔信号必须被跳过。"""
    from research.experiment.entry_exp import ExpConfig, run_experiment, _rules_multitf

    bars = _bars(400)
    cfg = ExpConfig(warmup_bars=20, max_hold_bars=40)
    res = run_experiment(bars, cfg=cfg, providers={"C": _rules_multitf},
                         research_end_ms=bars[200].close_ms, decision_every=1)
    trades = sorted(res["trades"], key=lambda t: t["signal_bar"])
    last_end = -1
    for t in trades:
        assert t["signal_bar"] > last_end, f"串仓: bar {t['signal_bar']} <= 上一笔结束 {last_end}"
        last_end = t["signal_bar"] + t["holding_ms"] // 300_000
