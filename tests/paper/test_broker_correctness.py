"""Sprint 2.1 反例：模拟账本正确性。

对应复审的 6 项：
  1. expire 用开仓价、且 run_round 不调用；
  2. realized_pnl 不含开仓手续费、day_start 默认 0；
  3. submit 不拒绝 shadow；
  4. ATR 代理被叫作 EV（在 tests/risk 里覆盖）；
  5. 止损只按收盘价检查，跳空仍按止损价成交；
  6. VERIFIED 可能被影子策略的优先级盖过（在 tests/strategy 里覆盖）。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.paper.broker import PaperBroker
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000


def _intent(iid: str = "i1", side: str = "LONG", entry: str = "100",
            stop: str = "95", tp: str = "110", holding: int = 3_600_000,
            shadow: bool = False) -> TradeIntent:
    return TradeIntent(
        intent_id=iid, decision_ts_ms=BASE, symbol="BTC/USDT", side=side,
        strategy_id="trend_follow", strategy_version="v1",
        entry_ref_price=D(entry), stop_price=D(stop), take_profit_price=D(tp),
        expected_holding_ms=holding, confidence=D("0.7"), reason="test", shadow=shadow,
    )


@pytest.fixture()
def broker(tmp_path: Path) -> PaperBroker:
    return PaperBroker(tmp_path / "l.sqlite", starting_cash=D("1000"))


def test_expire_uses_actual_price_not_entry(broker: PaperBroker) -> None:
    broker.submit(_intent(), D("100"))
    closed = broker.expire(BASE + 3_600_001, {"BTC/USDT": D("95")})
    assert len(closed) == 1
    assert closed[0].exit_price < D("95") * D("1.001"), "必须按当前可用价平仓，不能用开仓价"
    assert closed[0].exit_price > D("94"), "平仓价不应偏离当前价太远"


def test_expire_skips_when_no_price(broker: PaperBroker) -> None:
    broker.submit(_intent(), D("100"))
    assert broker.expire(BASE + 3_600_001, {}) == []
    assert len(broker.open_positions()) == 1


def test_expire_respects_position_holding(broker: PaperBroker) -> None:
    broker.submit(_intent(holding=600_000), D("100"))
    assert broker.expire(BASE + 599_999, {"BTC/USDT": D("99")}) == []
    assert len(broker.expire(BASE + 600_001, {"BTC/USDT": D("99")})) == 1


def test_realized_pnl_includes_both_fees(broker: PaperBroker) -> None:
    broker.submit(_intent(), D("100"))
    closed = broker.expire(BASE + 3_600_001, {"BTC/USDT": D("100")})
    # 价格没动：净亏只能是双边手续费（开 5bps + 平 5bps），不能是 0
    assert len(closed) == 1 and closed[0].pnl < 0
    assert closed[0].pnl > D("-0.35"), f"费用不该超过双边 10bps 太多，实得 {closed[0].pnl}"
    realized = broker.realized_pnl_since(BASE - 1)
    assert realized == closed[0].pnl, "realized 必须使用已扣双边费的净 pnl"


def test_submit_rejects_shadow_intent(broker: PaperBroker) -> None:
    assert broker.submit(_intent(shadow=True), D("100")) is None
    assert broker.open_positions() == []
    assert broker.restore()["orders"] == 0


def test_stop_gap_through_uses_worse_open(broker: PaperBroker) -> None:
    broker.submit(_intent(stop="95"), D("100"))
    # bar 开盘就跌到 90（跳空），不能假装在 95 成交
    closed = broker.mark_bar("BTC/USDT", D("90"), D("91"), D("89"), BASE + 1000)
    assert len(closed) == 1 and closed[0].reason == "STOP_LOSS"
    assert closed[0].exit_price < D("91"), f"跳空应按开盘价成交，实得 {closed[0].exit_price}"


def test_stop_trigger_inside_bar_uses_stop(broker: PaperBroker) -> None:
    broker.submit(_intent(stop="95"), D("100"))
    closed = broker.mark_bar("BTC/USDT", D("99"), D("99.5"), D("94"), BASE + 1000)
    assert len(closed) == 1
    assert closed[0].exit_price < D("96") and closed[0].exit_price > D("94")


def test_same_bar_stop_and_tp_prefers_stop(broker: PaperBroker) -> None:
    broker.submit(_intent(stop="95", tp="105"), D("100"))
    closed = broker.mark_bar("BTC/USDT", D("100"), D("106"), D("94"), BASE + 1000)
    assert len(closed) == 1 and closed[0].reason == "STOP_LOSS", "同 bar 双触发必须按最坏情形"


def test_take_profit_gap_through_does_not_overfill(broker: PaperBroker) -> None:
    broker.submit(_intent(stop="95", tp="105"), D("100"))
    closed = broker.mark_bar("BTC/USDT", D("110"), D("111"), D("109"), BASE + 1000)
    assert len(closed) == 1 and closed[0].reason == "TAKE_PROFIT"
    assert closed[0].exit_price <= D("106"), "止盈跳空不应按比止盈价更优的价格成交"