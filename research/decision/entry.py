"""实验 E 的多周期入场快照。

与旧的 ``MarketSnapshot.summary()`` 不同，这里把 5m/15m/1h/4h 的**已收盘**
指标全部交给模型，而不是只给 1h 聚合特征。所有字段都带 ``as_of_ms`` /
``last_closed_ms``，保证可回放、可检查是否有前视。

约束：
  - 只用决策时刻已收盘的 K 线（``state.timeframes`` 由 analyze() 裁剪）；
  - 新闻只给 occurred_at_ms <= as_of_ms 的事件，并标出相对上一轮的变化；
  - 成本（手续费 / 滑点 / 点差 / 最低净收益门槛）显式进入输入，
    模型必须面对"扣费后是否值得交易"这个问题；
  - 不制造数据：没有的字段就是 None / 空列表，不填 0。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from research.market.contracts import MarketState

D = Decimal
TFS = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class EntrySnapshot:
    as_of_ms: int
    symbol: str
    timeframes: dict[str, dict] = field(default_factory=dict)
    news: tuple[dict, ...] = ()
    news_since_last: tuple[str, ...] = ()
    costs: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)   # 冻结基线（rules / legacy model）的决策
    data_quality: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "as_of_ms": self.as_of_ms,
            "symbol": self.symbol,
            "timeframes": self.timeframes,
            "news": list(self.news),
            "news_since_last": list(self.news_since_last),
            "costs": self.costs,
            "baseline": self.baseline,
            "data_quality": self.data_quality,
        }


def _n(v) -> str | None:
    if v is None:
        return None
    return str(v)


def _tf_block(view, now_ms: int) -> dict:
    if view is None:
        return {"available": False}
    last = int(getattr(view, "last_closed_ms", 0) or 0)
    e20, e60 = getattr(view, "ema20", None), getattr(view, "ema60", None)
    if e20 is not None and e60 is not None:
        trend = "TREND_UP" if e20 > e60 else ("TREND_DOWN" if e20 < e60 else "FLAT")
    else:
        trend = "UNKNOWN"
    return {
        "available": True,
        "last_closed_ms": last,
        "age_ms": max(0, now_ms - last),
        "close": _n(getattr(view, "close", None)),
        "ema20": _n(e20), "ema60": _n(e60), "ema200": _n(getattr(view, "ema200", None)),
        "trend": trend,
        "rsi": _n(getattr(view, "rsi", None)),
        "adx": _n(getattr(view, "adx", None)),
        "macd_hist": _n(getattr(view, "macd_hist", None)),
        "atr_pct": _n(getattr(view, "atr_pct", None)),
        "volume_z": _n(getattr(view, "volume_z", None)),
        "slope_bps": _n(getattr(view, "slope_bps", None)),
    }


def _news_block(digest, as_of_ms: int, since_ms: int) -> tuple[list[dict], tuple[str, ...]]:
    items: list[dict] = []
    new: list[str] = []
    if digest is None:
        return items, ()
    for n in (getattr(digest, "news", ()) or ()):
        t = int(getattr(n, "occurred_at_ms", 0) or 0)
        if t <= 0 or t > as_of_ms:      # 前视防御
            continue
        headline = str(getattr(n, "headline", "") or "")
        if not headline:
            continue
        cred = getattr(n, "credibility", None)
        direction = getattr(n, "direction", None)
        items.append({
            "occurred_at_ms": t,
            "headline": headline[:160],
            "credibility": _n(cred),
            "direction": str(direction) if direction is not None else None,
            "is_new": t > since_ms,
            "age_ms": as_of_ms - t,
        })
        if t > since_ms:
            new.append(headline[:120])
    items.sort(key=lambda x: x["occurred_at_ms"], reverse=True)
    return items[:10], tuple(new[:5])


def build_entry_snapshot(*, state: MarketState, as_of_ms: int, digest=None,
                         fee_bps: D = D("4"), slippage_bps: D = D("5"),
                         spread_bps: D = D("5"), min_net_bps: D = D("10"),
                         last_news_ms: int = 0, baseline: dict | None = None,
                         data_quality: dict | None = None) -> EntrySnapshot:
    """从已收盘状态构造入场快照。没有任何未来数据。"""
    tfs = getattr(state, "timeframes", None) or {}
    blocks = {tf: _tf_block(tfs.get(tf), as_of_ms) for tf in TFS}
    news, delta = _news_block(digest, as_of_ms, last_news_ms)
    rt = D(fee_bps) * D("2") + D(slippage_bps) * D("2") + D(spread_bps)
    return EntrySnapshot(
        as_of_ms=as_of_ms,
        symbol=str(getattr(state, "symbol", "BTC/USDT")),
        timeframes=blocks,
        news=tuple(news),
        news_since_last=delta,
        costs={
            "fee_bps_each_side": str(fee_bps),
            "slippage_bps_each_side": str(slippage_bps),
            "spread_bps": str(spread_bps),
            "roundtrip_bps": str(rt),
            "min_net_edge_bps": str(min_net_bps),
        },
        baseline=dict(baseline or {}),
        data_quality=dict(data_quality or {}),
    )


__all__ = ["EntrySnapshot", "build_entry_snapshot", "TFS"]
