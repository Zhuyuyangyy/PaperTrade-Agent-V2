"""标签引擎（P1/P1.1）——从未来窗口生成 ResearchLabel（规格 §7 的 Label 侧）。

铁律：
  1. 标签只能从 origin+H **之后**的数据生成；生成时禁读 origin 之前的特征；
  2. `future_return` 是**相对收益**（future/origin − 1），不是价格。
     Sprint 0 之前的实现把价格直接写进 future_return，`hit_rate` 再用它的符号
     判方向——价格恒为正，等于所有信号永远"命中"。这是 S0-01；
  3. 标签只能取 **可用时间 >= origin+H** 的快照；早于 horizon 的价格不算标签（S0-02）。
"""
from __future__ import annotations

from decimal import Decimal

from .schemas import ResearchLabel


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def make_label(origin_ts_ms: int, horizon: str, origin_price, future_price,
               max_adverse: Decimal | None = None,
               label_ts_ms: int | None = None) -> ResearchLabel:
    """用 origin 价格与 t+H 价格构造相对收益标签（调用方负责只传 t+H 的数据）。"""
    op = _d(origin_price)
    fp = _d(future_price)
    if op <= 0:
        raise ValueError("origin_price 必须 > 0")
    return ResearchLabel(
        origin_ts_ms=origin_ts_ms,
        horizon=horizon,
        future_return=(fp / op) - 1,
        realized_volatility=None,
        max_adverse_excursion=max_adverse,
        tail_event=False,
        origin_price=op,
        label_ts_ms=label_ts_ms,
    )


def label_from_store(store, symbol: str, origin_ts_ms: int, horizon_ms: int,
                     band_ms: int = 600_000, with_mae: bool = False) -> ResearchLabel | None:
    """从 EventStore 取 origin 时刻价格与 origin+H 之后最近价格，算相对收益。

    - origin 价格：可用时间 <= origin 的最后一条快照；
    - 未来价格：可用时间 >= origin+H 的第一条快照，且不得晚于 origin+H+band；
    - 任一侧缺失 → None（不猜、不用更早的价格冒充）。
    """
    sym = symbol.upper()
    origin_ev = store.last_available("binance", sym, origin_ts_ms, event_type="snapshot")
    if origin_ev is None:
        return None
    target = origin_ts_ms + horizon_ms
    future_ev = store.first_available_after(
        "binance", sym, target, event_type="snapshot", until_ms=target + band_ms,
    )
    if future_ev is None:
        return None
    origin_price = origin_ev["payload"].get("price")
    future_price = future_ev["payload"].get("price")
    if origin_price is None or future_price is None:
        return None

    mae: Decimal | None = None
    if with_mae:
        rows = store.query_available(
            "binance", origin_ts_ms, future_ev["event_ts_ms"] + 1,
            event_type="snapshot", key=sym,
            as_of_ms=max(future_ev["event_ts_ms"], future_ev["ingest_ts_ms"]),
        )
        prices = [Decimal(str(r["payload"]["price"])) for r in rows if r["payload"].get("price")]
        if prices:
            mae = min(p / Decimal(str(origin_price)) - 1 for p in prices)

    return make_label(
        origin_ts_ms, f"{horizon_ms // 1000}s", origin_price, future_price,
        max_adverse=mae, label_ts_ms=future_ev["ingest_ts_ms"],
    )