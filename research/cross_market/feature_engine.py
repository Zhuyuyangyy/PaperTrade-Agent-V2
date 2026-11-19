"""特征引擎（P1）——把两侧原始事件按可用时间对齐成 FeatureSnapshot。

铁律：
  - 只用"在决策时刻已可用"的事件（可用性门禁，ingest 迟于决策即挡）；
  - 特征里禁出现 ingest_ts（它只判可用性，不是特征）；
  - 对齐输出 (FeatureSnapshot, 决策时刻)，标签由 label_engine 从未来生成。

时钟偏移实况（2026-11-19 部署节点实测）：Polymarket CLOB 服务端时间戳
比本地快 ~4s。可用性门禁因此对 poly 数据保守（本地决策时刻要等服务端
时间戳追上）——这是安全方向：宁可慢几秒，不冒前视风险。若未来需要消除
这 ~4s 保守延迟，应做 per-source 时钟偏移估计并显式记录，而不是偷偷
把 event_ts 改成本地时间。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .replay import usable_at
from .schemas import FeatureSnapshot
from .storage import EventStore


@dataclass
class AlignedPoint:
    decision_ts_ms: int
    feature: FeatureSnapshot


def align_features(store: EventStore, market_key: str, token_ids: list[str],
                   symbol: str, start_ms: int, end_ms: int,
                   decision_now_ms: int | None = None,
                   step_ms: int = 60_000) -> list[AlignedPoint]:
    """在 [start_ms, end_ms] 上按 step 采样决策时刻，装配 point-in-time 特征。

    每个决策时刻：取该时刻已可用的最新 poly book（任一 token）与最新 binance
    快照；任一侧缺失 → 该侧字段为空（禁跨时刻拼接未可用数据）。
    """
    # 预取窗口内事件（按 event_ts 排序）
    poly_rows = store.query("polymarket", start_ms, end_ms)
    bin_rows = store.query("binance", start_ms, end_ms)
    token_set = set(token_ids)

    points: list[AlignedPoint] = []
    t = start_ms
    while t < end_ms:
        decision = decision_now_ms if decision_now_ms is not None else t
        poly_state = None
        for r in poly_rows:
            avail = usable_at(r["event_ts_ms"], r["ingest_ts_ms"])
            if avail > decision:
                break  # 后续更晚（query 已排序）
            if r["key"] in token_set:
                poly_state = r["payload"]  # 已可用的最新一本
        bin_state = None
        for r in bin_rows:
            avail = usable_at(r["event_ts_ms"], r["ingest_ts_ms"])
            if avail > decision:
                break
            if r["key"] == symbol.upper():
                bin_state = r["payload"]
        if poly_state is not None or bin_state is not None:
            points.append(AlignedPoint(
                decision_ts_ms=decision,
                feature=FeatureSnapshot(
                    feature_ts_ms=decision,
                    polymarket=_poly_view(poly_state),
                    binance=_bin_view(bin_state),
                ),
            ))
        t += step_ms
    return points


def _poly_view(payload: dict | None) -> dict:
    if not payload:
        return {}
    out: dict = {}
    for k in ("bid", "ask", "spread_abs", "spread_bps"):
        v = payload.get(k)
        if v is not None:
            out[k] = Decimal(str(v))
    return out


def _bin_view(payload: dict | None) -> dict:
    if not payload:
        return {}
    out: dict = {}
    for k in ("price", "volume", "oi_snapshot", "cvd_delta", "funding_rate"):
        v = payload.get(k)
        if v is not None:
            out[k] = Decimal(str(v)) if k != "funding_rate" else v
    # 强平只透传观测口径，不透传伪造值
    out["liquidation_observed_usdt"] = Decimal(str(payload.get("liquidation_observed_usdt", "0")))
    out["liquidation_coverage"] = payload.get("liquidation_coverage", "UNKNOWN")
    return out
