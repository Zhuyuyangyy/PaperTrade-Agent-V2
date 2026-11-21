"""特征引擎（P1/P1.1）——把两侧原始事件按可用时间对齐成 FeatureSnapshot。

Sprint 0 修掉的三处（审计 S0-06/S0-07/S0-08）：
  1. **每个决策点只用该时刻可用的数据**。旧实现 `decision = decision_now_ms or t`
     在实时模式下把同一个决策时刻套到窗口里每个采样点上——等于用窗口末尾的
     最新数据回填所有历史点（经典前视）；
  2. **YES/NO 归一化**。旧实现"任一 token 的最新一本"当概率：NO token 的报价
     被当成 YES 概率，方向系统性反了，而且两 token 的一价关系从未校验；
  3. **变化量**。方向信号需要 t 与 t−lookback 的差（价格动量、概率动量），
     旧特征只给水平值，信号层只能写"价格 > 0"这种恒真条件。

铁律：
  - 只用"在决策时刻已可用"的事件（可用性门禁，ingest 迟于决策即挡）；
  - 特征里禁出现 ingest_ts（它只判可用性，不是特征）；
  - 对齐输出 (FeatureSnapshot, 决策时刻)，标签由 label_engine 从未来生成。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .replay import usable_at
from .schemas import FeatureSnapshot
from .storage import EventStore

DEFAULT_LOOKBACK_MS = 300_000     # 方向动量的回看窗口（5m）
DEFAULT_MAX_AGE_MS = 600_000      # 特征可接受的最大陈旧度（超过则显式标 stale）
PROB_CONSISTENCY_TOL = Decimal("0.05")  # |yes_mid + no_mid − 1| 容差
BPS = Decimal("10000")


@dataclass
class AlignedPoint:
    decision_ts_ms: int
    feature: FeatureSnapshot


class _PointInTime:
    """已按可用时间排序的事件流上的 point-in-time 游标。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._i = 0
        self._last: dict[str, dict] = {}

    def advance_to(self, ts_ms: int) -> None:
        while self._i < len(self._rows):
            ev = self._rows[self._i]
            if usable_at(ev["event_ts_ms"], ev["ingest_ts_ms"]) > ts_ms:
                break
            self._last[ev["key"]] = ev
            self._i += 1

    def get(self, key: str) -> dict | None:
        return self._last.get(key)


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except ArithmeticError:
        return None


def _mid(ev: dict | None) -> Decimal | None:
    if not ev:
        return None
    bid = _dec(ev["payload"].get("bid"))
    ask = _dec(ev["payload"].get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2


def _delta_bps(cur: Decimal | None, ref: Decimal | None) -> Decimal | None:
    if cur is None or ref is None or ref == 0:
        return None
    return ((cur / ref) - 1) * BPS


def align_features(store: EventStore, market_key: str, token_ids: list[str],
                   symbol: str, start_ms: int, end_ms: int,
                   step_ms: int = 60_000,
                   decision_now_ms: int | None = None,
                   lookback_ms: int = DEFAULT_LOOKBACK_MS,
                   max_age_ms: int = DEFAULT_MAX_AGE_MS) -> list[AlignedPoint]:
    """在 [start_ms, end_ms) 上按 step 采样决策时刻，装配 point-in-time 特征。

    每个决策时刻 t：取"可用时间 <= t"的最新 YES/NO 盘口与最新 Binance 快照，
    并用"可用时间 <= t−lookback"的快照算变化量。任一侧缺失 → 该侧字段为空
    （禁跨时刻拼接未可用数据）。

    decision_now_ms：实时模式的"当前时刻上限"——只生成 t <= decision_now_ms 的点，
    且只用在该时刻已可用的事件。它**不会**改写每个点自己的决策时刻 t。
    """
    tokens = [str(t) for t in token_ids]
    yes_token = tokens[0] if tokens else None
    no_token = tokens[1] if len(tokens) > 1 else None
    sym = symbol.upper()

    lower = start_ms - lookback_ms - max_age_ms
    poly_rows = store.query_available("polymarket", lower, end_ms, as_of_ms=decision_now_ms)
    bin_rows = store.query_available("binance", lower, end_ms, event_type="snapshot",
                                     key=sym, as_of_ms=decision_now_ms)

    poly_now, poly_ref = _PointInTime(poly_rows), _PointInTime(poly_rows)
    bin_now, bin_ref = _PointInTime(bin_rows), _PointInTime(bin_rows)

    points: list[AlignedPoint] = []
    t = start_ms
    while t < end_ms:
        if decision_now_ms is not None and t > decision_now_ms:
            break
        ref_ts = t - lookback_ms
        poly_now.advance_to(t)
        poly_ref.advance_to(ref_ts)
        bin_now.advance_to(t)
        bin_ref.advance_to(ref_ts)

        yes_ev = poly_now.get(yes_token) if yes_token else None
        no_ev = poly_now.get(no_token) if no_token else None
        yes_ref_ev = poly_ref.get(yes_token) if yes_token else None
        poly_view = _poly_view(yes_ev, no_ev, yes_ref_ev, t, max_age_ms)

        bin_ev = bin_now.get(sym)
        bin_ref_ev = bin_ref.get(sym)
        bin_view = _bin_view(bin_ev, bin_ref_ev, t, max_age_ms)

        if poly_view or bin_view:
            points.append(AlignedPoint(
                decision_ts_ms=t,
                feature=FeatureSnapshot(
                    feature_ts_ms=t,
                    polymarket=poly_view,
                    binance=bin_view,
                ),
            ))
        t += step_ms
    return points


def _poly_view(yes_ev: dict | None, no_ev: dict | None, yes_ref_ev: dict | None,
               t: int, max_age_ms: int) -> dict:
    out: dict = {}
    yes_bid = _dec(yes_ev["payload"].get("bid")) if yes_ev else None
    yes_ask = _dec(yes_ev["payload"].get("ask")) if yes_ev else None
    yes_mid = _mid(yes_ev)
    no_mid = _mid(no_ev)

    if yes_mid is not None:
        out["yes_bid"] = yes_bid
        out["yes_ask"] = yes_ask
        out["yes_mid"] = yes_mid
        out["yes_ts_ms"] = yes_ev["event_ts_ms"]
        if yes_ask is not None and yes_bid is not None and yes_mid > 0:
            out["spread_bps"] = ((yes_ask - yes_bid) / yes_mid * BPS).quantize(Decimal("0.01"))
        out["yes_age_ms"] = t - usable_at(yes_ev["event_ts_ms"], yes_ev["ingest_ts_ms"])
        out["stale"] = out["yes_age_ms"] > max_age_ms
        out["yes_mid_delta_bps"] = _delta_bps(yes_mid, _mid(yes_ref_ev))

    if no_mid is not None:
        out["no_mid"] = no_mid

    if not out:
        return {}   # 两侧都没有有效报价 → 该侧不可用（不得产生"空点"）

    if yes_mid is not None and no_mid is not None:
        out["consistent"] = abs(yes_mid + no_mid - Decimal("1")) <= PROB_CONSISTENCY_TOL
    else:
        out["consistent"] = None

    out["prob_usable"] = yes_mid is not None and out["consistent"] is not False
    if out["consistent"] is False:
        out["prob_inconsistency"] = (yes_mid + no_mid - Decimal("1"))
    return out


def _bin_view(cur_ev: dict | None, ref_ev: dict | None, t: int, max_age_ms: int) -> dict:
    if not cur_ev:
        return {}
    p = cur_ev["payload"]
    out: dict = {}
    price = _dec(p.get("price"))
    if price is not None:
        out["price"] = price
        out["price_ts_ms"] = cur_ev["event_ts_ms"]
        ref_price = _dec(ref_ev["payload"].get("price")) if ref_ev else None
        out["price_delta_bps"] = _delta_bps(price, ref_price)
    oi = _dec(p.get("oi_snapshot"))
    if oi is not None:
        out["oi_snapshot"] = oi
        ref_oi = _dec(ref_ev["payload"].get("oi_snapshot")) if ref_ev else None
        out["oi_delta_bps"] = _delta_bps(oi, ref_oi)
    for k in ("volume", "cvd_delta"):
        v = _dec(p.get(k))
        if v is not None:
            out[k] = v
    if p.get("funding_rate") not in (None, ""):
        out["funding_rate"] = p["funding_rate"]
    out["liquidation_observed_usdt"] = _dec(p.get("liquidation_observed_usdt")) or Decimal("0")
    out["liquidation_coverage"] = p.get("liquidation_coverage", "UNKNOWN")
    out["age_ms"] = t - usable_at(cur_ev["event_ts_ms"], cur_ev["ingest_ts_ms"])
    out["stale"] = out["age_ms"] > max_age_ms
    return out