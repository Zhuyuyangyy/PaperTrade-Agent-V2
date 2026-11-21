"""Sprint 4.3 反例：账本审计字段、开仓当根、UTC 日界、资金费来源。

- orders.shadow 必须反映意图类型（实验盘影子订单写 1）；
- 开仓发生在某根 K 线中途时，不能用该根的完整 OHLC 判止损（会把开仓前波动算进去）；
- 每日亏损限制必须按 UTC 日界（每轮重算），不能用累计历史；
- 统计必须标注资金费是估算值，不能冒充真实历史费率。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.paper.broker import PaperBroker
from research.runtime.loop import utc_day_start
from research.runtime.metrics import summarize
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000 // 86_400_000 * 86_400_000   # UTC 日界
HOUR = 3_600_000


def _intent(shadow: bool, iid: str = "i1", side: str = "LONG", ts: int | None = None) -> TradeIntent:
    return TradeIntent(
        intent_id=iid, decision_ts_ms=(ts if ts is not None else BASE + 10 * HOUR), symbol="BTC/USDT", side=side,
        strategy_id="trend_follow", strategy_version="v1",
        entry_ref_price=D("100"), stop_price=D("95") if side == "LONG" else D("105"),
        take_profit_price=D("110") if side == "LONG" else D("90"),
        expected_holding_ms=HOUR, confidence=D("0.7"), reason="test", shadow=shadow,
    )


def test_orders_shadow_column_matches_intent(tmp_path: Path) -> None:
    shadow = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         shadow_mode="shadow_only")
    verified = PaperBroker(tmp_path / "v.sqlite", starting_cash=D("1000"))
    shadow.submit(_intent(True, "s1"), D("100"))
    verified.submit(_intent(False, "v1"), D("100"))
    s = shadow._db.execute("SELECT shadow FROM orders WHERE intent_id='s1'").fetchone()
    v = verified._db.execute("SELECT shadow FROM orders WHERE intent_id='v1'").fetchone()
    assert s["shadow"] == 1, "实验盘影子订单必须写 shadow=1"
    assert v["shadow"] == 0


def test_mark_skips_bar_opened_mid_candle(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         shadow_mode="shadow_only")
    bar_open = BASE + 10 * HOUR
    # 在第 10 根 1h K 线中途（+30 分钟）开仓
    broker.submit(_intent(True, ts=bar_open + 30 * 60_000), D("100"))
    # 该根 K 线 low=90（穿止损 95），但发生在开仓前后未知 → 不得判止损
    assert broker.mark_bar("BTC/USDT", D("100"), D("101"), D("90"), bar_open + HOUR,
                           bar_open_ms=bar_open) == []
    # 下一根 K 线（开仓之后）跌破止损 → 正常平仓
    closed = broker.mark_bar("BTC/USDT", D("96"), D("97"), D("94"), bar_open + 2 * HOUR,
                             bar_open_ms=bar_open + HOUR)
    assert len(closed) == 1 and closed[0].reason == "STOP_LOSS"


def test_mark_close_bar_still_checks(tmp_path: Path) -> None:
    """开仓在 K 线收盘时刻（entry_ts == bar close）时，该根不应再判；下一根判。"""
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         shadow_mode="shadow_only")
    bar_open = BASE + 10 * HOUR
    # 开仓在该根之后（+2h），该根早于开仓，本来就该跳过
    broker.submit(_intent(True, ts=bar_open + 2 * HOUR), D("100"))
    assert broker.mark_bar("BTC/USDT", D("100"), D("101"), D("90"), bar_open + HOUR,
                           bar_open_ms=bar_open) == []


def test_utc_day_start() -> None:
    assert utc_day_start(BASE + 5 * HOUR) == BASE
    assert utc_day_start(BASE + 25 * HOUR) == BASE + 86_400_000
    assert utc_day_start(BASE) == BASE


def test_summarize_labels_funding_as_estimate(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         shadow_mode="shadow_only", funding_bps_per_8h=D("1"))
    summary = summarize(broker, label="shadow")
    assert summary["funding_source"] == "estimated"
    assert "funding_paid" in summary