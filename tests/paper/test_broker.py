"""模拟撮合测试（Sprint 2）：幂等、费用/滑点、止损止盈、超时、权益、恢复。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.paper.broker import PaperBroker
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000


def _intent(side: str = "LONG", entry: str = "100", stop: str = "98",
            tp: str = "104", iid: str = "i1") -> TradeIntent:
    return TradeIntent(
        intent_id=iid, decision_ts_ms=BASE, symbol="BTC/USDT", side=side,
        strategy_id="trend_follow", strategy_version="v1",
        entry_ref_price=D(entry), stop_price=D(stop), take_profit_price=D(tp),
        expected_holding_ms=3_600_000, confidence=D("0.7"), reason="test",
    )


@pytest.fixture()
def broker(tmp_path: Path) -> PaperBroker:
    return PaperBroker(tmp_path / "ledger.sqlite", starting_cash=D("1000"),
                       fee_bps=D("5"), slippage_bps=D("5"))


def test_fill_applies_slippage_and_fee(broker: PaperBroker) -> None:
    fill = broker.submit(_intent(), D("100"))
    assert fill is not None
    assert fill.price == D("100") * D("1.0005")
    assert fill.fee == D("100") * D("5") / D("10000")
    assert broker.cash() == D("1000") - D("100") - fill.fee


def test_submit_is_idempotent(broker: PaperBroker) -> None:
    assert broker.submit(_intent(), D("100")) is not None
    assert broker.submit(_intent(), D("100")) is None
    assert len(broker.open_positions()) == 1
    assert broker.restore()["orders"] == 1


def test_short_fill_direction(broker: PaperBroker) -> None:
    fill = broker.submit(_intent("SHORT", stop="104", tp="96"), D("100"))
    assert fill is not None and fill.price == D("100") * D("0.9995")
    assert broker.open_positions()[0]["side"] == "SHORT"


def test_stop_loss_and_take_profit(broker: PaperBroker) -> None:
    broker.submit(_intent(iid="a"), D("100"))
    closed = broker.mark("BTC/USDT", D("97"), BASE + 1000)
    assert len(closed) == 1 and closed[0].reason == "STOP_LOSS" and closed[0].pnl < 0
    assert broker.open_positions() == []

    broker.submit(_intent(iid="b"), D("100"))
    closed = broker.mark("BTC/USDT", D("105"), BASE + 2000)
    assert len(closed) == 1 and closed[0].reason == "TAKE_PROFIT" and closed[0].pnl > 0


def test_short_stop_triggers_on_price_up(broker: PaperBroker) -> None:
    broker.submit(_intent("SHORT", stop="104", tp="96", iid="s"), D("100"))
    closed = broker.mark("BTC/USDT", D("105"), BASE + 1000)
    assert len(closed) == 1 and closed[0].reason == "STOP_LOSS"


def test_timeout_expiry(broker: PaperBroker) -> None:
    broker.submit(_intent(), D("100"))
    assert broker.expire(BASE + 1000, 3_600_000) == []
    closed = broker.expire(BASE + 3_600_001, 3_600_000)
    assert len(closed) == 1 and closed[0].reason == "TIMEOUT"


def test_equity_marks_open_positions(broker: PaperBroker) -> None:
    broker.submit(_intent(), D("100"))
    e_up = broker.equity(BASE + 1, {"BTC/USDT": D("105")})
    e_down = broker.equity(BASE + 2, {"BTC/USDT": D("95")})
    assert e_up > e_down


def test_insufficient_cash_rejects(broker: PaperBroker) -> None:
    tiny = PaperBroker(broker.path.parent / "tiny.sqlite", starting_cash=D("50"))
    assert tiny.submit(_intent(), D("100")) is None
    assert tiny.open_positions() == []


def test_restore_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite"
    b = PaperBroker(path, starting_cash=D("1000"))
    b.submit(_intent(), D("100"))
    cash = b.cash()
    b.close()
    b2 = PaperBroker(path, starting_cash=D("1000"))
    assert b2.cash() == cash
    assert len(b2.open_positions()) == 1
    assert b2.restore()["open_positions"] == 1