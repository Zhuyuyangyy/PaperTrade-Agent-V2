"""标签引擎（P1）——从未来窗口生成 ResearchLabel（规格 §7 的 Label 侧）。

铁律：标签只能从 origin_ts + H 之后的数据生成；生成时禁读 origin 之前的特征。
"""
from __future__ import annotations

from decimal import Decimal

from .schemas import ResearchLabel


def make_label(origin_ts_ms: int, horizon: str, future_price: Decimal,
               max_adverse: Decimal | None = None) -> ResearchLabel:
    """用未来价格构造标签（调用方负责只传 t+H 的数据）。"""
    return ResearchLabel(
        origin_ts_ms=origin_ts_ms,
        horizon=horizon,
        future_return=future_price,
        realized_volatility=None,
        max_adverse_excursion=max_adverse,
        tail_event=False,
    )


def label_from_store(store, symbol: str, origin_ts_ms: int, horizon_ms: int) -> ResearchLabel | None:
    """从 EventStore 取 origin+horizon 之后的最近快照价格作标签；无则 None。"""
    rows = store.query("binance", origin_ts_ms + horizon_ms, origin_ts_ms + horizon_ms + 600_000,
                       event_type="snapshot", key=symbol.upper())
    if not rows:
        return None
    price = Decimal(rows[0]["payload"].get("price", "0"))
    return make_label(origin_ts_ms, f"{horizon_ms // 1000}s", price)
