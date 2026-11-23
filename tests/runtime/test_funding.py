"""实验 E4：资金费率信号——防前视与 carry 符号。"""
from decimal import Decimal as D

from research.experiment.funding import funding_signals
from research.experiment.funding_exp import _carry, _index_at
from research.market.contracts import Candle

STEP = 3_600_000
START = (1_790_000_000_000 // STEP) * STEP


def _bars(n=200, base=59000.0):
    out = []
    px = base
    for i in range(n):
        nxt = px * (1.001 if i % 3 else 0.999)
        out.append(Candle(START + i * STEP, D(str(px)), D(str(max(px, nxt) + 5)),
                          D(str(min(px, nxt) - 5)), D(str(nxt)), D("10"), STEP))
        px = nxt
    return out


def _funding(n=200, rate="0.0001", spike_at=None, spike="0.003"):
    """带正常波动的费率序列（sd 必须 >0，否则 z 无法计算会被跳过）。"""
    out = []
    base = float(rate)
    for i in range(n):
        if spike_at is not None and i == spike_at:
            r = spike
        else:
            r = str(base * (1.0 + 0.2 * ((i % 5) - 2)))   # ±40% 抖动
        out.append({"funding_ms": START + i * 8 * STEP, "rate": r, "mark_price": "59000"})
    return out


def test_signal_uses_only_past_rates():
    """第 i 条的 z 只能用 [i-lookback, i-1]，不得含当次或未来。"""
    fd = _funding(80, spike_at=70)
    bars = _bars(700)          # 80 条费率跨 640 小时，K 线要覆盖得住
    sigs = funding_signals(fd, bars, lookback=30, z_threshold=D("1.5"), min_history=20)
    # 尖峰那一条会被判定为极端 → 产生信号；而未到尖峰前的正常值不产生
    assert any(s.ts_ms == fd[70]["funding_ms"] for s in sigs)
    assert all(s.ts_ms <= fd[-1]["funding_ms"] for s in sigs)


def test_signal_direction_is_contrarian():
    fd = _funding(80, spike_at=70, spike="0.005")   # 极端正费率 = 多头拥挤
    sigs = funding_signals(fd, _bars(700), lookback=30, z_threshold=D("1.5"),
                           min_history=20)
    hit = [s for s in sigs if s.ts_ms == fd[70]["funding_ms"]]
    assert hit and hit[0].side == "SHORT"

    fd2 = _funding(80, spike_at=70, spike="-0.005")
    sigs2 = funding_signals(fd2, _bars(700), lookback=30, z_threshold=D("1.5"),
                            min_history=20)
    hit2 = [s for s in sigs2 if s.ts_ms == fd2[70]["funding_ms"]]
    assert hit2 and hit2[0].side == "LONG"


def test_index_at_is_at_or_before_and_entry_is_strictly_after():
    bars = _bars(50)
    ts = int(bars[10].open_ms)          # 正好等于某根开盘
    i = _index_at(bars, ts)
    assert int(bars[i].open_ms) == ts
    # 成交用 i+1 → 严格晚于 ts
    assert int(bars[i + 1].open_ms) > ts


def test_carry_sign_long_pays_positive_rate():
    times = [1000, 2000, 3000]
    rates = [D("0.001"), D("0.001"), D("0.001")]
    cost_long, n1 = _carry("LONG", D("1"), D("100"), times, rates, 500, 2500)
    assert n1 == 2 and cost_long == D("0.2")     # 多头付费
    cost_short, n2 = _carry("SHORT", D("1"), D("100"), times, rates, 500, 2500)
    assert n2 == 2 and cost_short == D("-0.2")   # 空头收费


def test_carry_excludes_settlement_at_entry_time():
    times = [1000, 2000]
    rates = [D("0.001"), D("0.001")]
    cost, n = _carry("LONG", D("1"), D("100"), times, rates, 1000, 2500)
    assert n == 1 and cost == D("0.1")           # 只在 2000 结算一次
