"""市场状态机（Sprint 2 修订版）——可解释状态 + 逐周期数据质量 + 策略特征。

Sprint 2 修掉的三处（用户复审 + 端到端需要）：

  1. **陈旧度逐周期**：旧实现用"所有周期里最新的一根"算整体 age，5m 新鲜会把
     1h 陈旧掩盖掉——而趋势判断依赖 1h。现在每个周期各自算 age/stale，
     ``critical_stale`` 只看 1h（趋势周期）。
  2. **真实收盘时间**：``last_closed_ms`` 改为 ``close_ms``，陈旧度按"数据何时
     变得完整"算，而不是按开盘时刻。
  3. **缺口进入风险**：``analyze`` 不抛错（分析层只报告），但 ``contiguous`` /
     ``gaps`` 传给风险层，由风控拒绝新开仓。
  4. **策略特征**：``features`` 提供区间上下沿、突破参考价、ATR 绝对值、
     距离边界 bps 等，策略层禁止自己重算指标。

判定顺序：数据质量先于观点（stale/gaps 直接进 risk），趋势看 1h EMA 排列 + ADX，
波动看 1h ATR% 历史分位，动量看 MACD 柱 + RSI；4h 只做一致性校验。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from .contracts import (
    Candle,
    Contradiction,
    DataQuality,
    Evidence,
    MarketState,
    TF_1H_MS,
    TimeframeView,
)
from .indicators import (
    adx as adx_series,
    atr as atr_series,
    ema,
    macd as macd_series,
    percentile_rank,
    rsi as rsi_series,
    slope_bps,
    volume_z as volume_z_series,
)
from .klines import closed_only, gaps

MIN_TREND_BARS = 205
VOL_LOOKBACK = 200
DEFAULT_SETTLE_MS = 30_000
STALE_BARS = 2          # 超过 2 根该周期的 K 线未更新 → stale
RANGE_LOOKBACK = 20     # 区间上下沿 / 突破参考的窗口
BPS = Decimal("10000")


def _at(seq: list[Decimal | None], i: int) -> Decimal | None:
    return seq[i] if 0 <= i < len(seq) else None


def _view(tf: str, candles: list[Candle], as_of_ms: int) -> tuple[TimeframeView, dict[str, list[Decimal | None]]]:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    i = len(candles) - 1
    tf_ms = candles[0].timeframe_ms
    e20, e60, e200 = ema(closes, 20), ema(closes, 60), ema(closes, 200)
    _, _, hist = macd_series(closes)
    rsi = rsi_series(closes)
    adx_s, _, _ = adx_series(highs, lows, closes)
    atr_s = atr_series(highs, lows, closes)
    vz = volume_z_series(vols)
    slope = slope_bps(closes, 20)
    atr_pct_series: list[Decimal | None] = [
        (a / c) if (a is not None and c != 0) else None for a, c in zip(atr_s, closes)
    ]
    last = candles[i]
    age = as_of_ms - last.close_ms
    view = TimeframeView(
        timeframe=tf,
        last_closed_ms=last.close_ms,
        close=closes[i],
        ema20=_at(e20, i),
        ema60=_at(e60, i),
        ema200=_at(e200, i),
        macd_hist=_at(hist, i),
        rsi=_at(rsi, i),
        adx=_at(adx_s, i),
        atr_pct=_at(atr_pct_series, i),
        volume_z=_at(vz, i),
        slope_bps=_at(slope, i),
        bars=len(candles),
        last_open_ms=last.open_ms,
        age_ms=max(0, age),
        stale=age > STALE_BARS * tf_ms,
    )
    return view, {"atr_pct": atr_pct_series, "atr": atr_s}


def _trend(view: TimeframeView, evidence: list[Evidence]) -> tuple[str, str]:
    if view.ema20 is None or view.ema60 is None or view.ema200 is None or view.adx is None:
        return "UNKNOWN", "1h 指标不足（需要 >=205 根已收盘 K 线）"
    up_stack = view.ema20 > view.ema60 > view.ema200
    down_stack = view.ema20 < view.ema60 < view.ema200
    if up_stack:
        evidence.append(Evidence("ema_stack_up", view.timeframe, "TREND_UP",
                                 threshold="ema20>ema60>ema200"))
    if down_stack:
        evidence.append(Evidence("ema_stack_down", view.timeframe, "TREND_DOWN",
                                 threshold="ema20<ema60<ema200"))
    if view.adx >= Decimal("25"):
        evidence.append(Evidence("adx_strong", view.timeframe, "trend",
                                 value=view.adx, threshold=">=25"))
    elif view.adx < Decimal("20"):
        evidence.append(Evidence("adx_weak", view.timeframe, "RANGE",
                                 value=view.adx, threshold="<20"))
    if up_stack and view.adx >= Decimal("20"):
        return "TREND_UP", f"1h EMA 多头排列且 ADX={view.adx:.1f}"
    if down_stack and view.adx >= Decimal("20"):
        return "TREND_DOWN", f"1h EMA 空头排列且 ADX={view.adx:.1f}"
    if view.adx < Decimal("20"):
        return "RANGE", f"1h ADX={view.adx:.1f} < 20，趋势强度不足"
    return "RANGE", f"1h EMA 排列不完整且 ADX={view.adx:.1f}，按震荡处理"


def _volatility(view: TimeframeView, atr_pct_history: list[Decimal | None],
                evidence: list[Evidence]) -> tuple[str, Decimal | None]:
    cur = view.atr_pct
    if cur is None:
        return "UNKNOWN", None
    history = [v for v in atr_pct_history[-VOL_LOOKBACK:] if v is not None]
    p = percentile_rank(history, cur)
    if p is None or len(history) < 30:
        return "UNKNOWN", p
    if p >= Decimal("0.8"):
        evidence.append(Evidence("atr_regime_high", view.timeframe, "HIGH_VOL",
                                 value=cur, threshold="ATR% 分位 >= 0.8"))
        return "HIGH_VOL", p
    if p <= Decimal("0.2"):
        evidence.append(Evidence("atr_regime_low", view.timeframe, "LOW_VOL",
                                 value=cur, threshold="ATR% 分位 <= 0.2"))
        return "LOW_VOL", p
    evidence.append(Evidence("atr_regime_normal", view.timeframe, "NORMAL_VOL",
                             value=cur, threshold="20h..80h 分位"))
    return "NORMAL_VOL", p


def _momentum(view: TimeframeView, evidence: list[Evidence]) -> str:
    if view.macd_hist is None or view.rsi is None:
        return "UNKNOWN"
    if view.macd_hist > 0 and view.rsi > Decimal("50"):
        evidence.append(Evidence("macd_positive", view.timeframe, "MOMENTUM_UP",
                                 value=view.macd_hist, threshold=">0 且 RSI>50"))
        return "MOMENTUM_UP"
    if view.macd_hist < 0 and view.rsi < Decimal("50"):
        evidence.append(Evidence("macd_negative", view.timeframe, "MOMENTUM_DOWN",
                                 value=view.macd_hist, threshold="<0 且 RSI<50"))
        return "MOMENTUM_DOWN"
    evidence.append(Evidence("macd_flat", view.timeframe, "FLAT", value=view.macd_hist,
                             threshold="MACD 与 RSI 不一致"))
    return "FLAT"


def _features(candles: list[Candle], view: TimeframeView,
              atr_hist: list[Decimal | None]) -> dict[str, Decimal | None]:
    """策略层直接消费的特征；窗口/口径固定在这里，策略不得重算。"""
    close = view.close
    recent = candles[-RANGE_LOOKBACK:]
    prior = candles[-(RANGE_LOOKBACK + 1):-1]
    range_high = max(c.high for c in recent)
    range_low = min(c.low for c in recent)
    ref_high = max(c.close for c in prior) if prior else None
    ref_low = min(c.close for c in prior) if prior else None
    atr_abs = _at(atr_hist, len(candles) - 1)

    def _bps(a: Decimal | None, b: Decimal | None) -> Decimal | None:
        if a is None or b is None or b == 0:
            return None
        return (a - b) / b * BPS

    return {
        "close": close,
        "range_high": range_high,
        "range_low": range_low,
        "range_mid": (range_high + range_low) / Decimal("2"),
        "range_width_bps": _bps(range_high, range_low),
        "breakout_ref_high": ref_high,
        "breakout_ref_low": ref_low,
        "atr_abs": atr_abs,
        "atr_pct": view.atr_pct,
        "ema20": view.ema20,
        "ema60": view.ema60,
        "ema200": view.ema200,
        "adx": view.adx,
        "rsi": view.rsi,
        "volume_z": view.volume_z,
        "slope_bps": view.slope_bps,
        "distance_to_range_high_bps": _bps(range_high, close),
        "distance_to_range_low_bps": _bps(close, range_low),
    }


def analyze(symbol: str, candles_by_tf: Mapping[str, list[Candle]], as_of_ms: int,
            *, settle_ms: int = DEFAULT_SETTLE_MS, max_gaps: int = 10) -> MarketState:
    evidence: list[Evidence] = []
    contradictions: list[Contradiction] = []
    views: dict[str, TimeframeView] = {}
    atr_hist: dict[str, dict[str, list[Decimal | None]]] = {}
    all_gaps: list[dict[str, int]] = []
    per_tf: dict[str, dict] = {}
    notes: list[str] = []
    seen: list[str] = []

    for tf in ("5m", "15m", "1h", "4h"):
        raw = candles_by_tf.get(tf) or []
        if not raw:
            continue
        candles = closed_only(sorted(raw, key=lambda c: c.open_ms), as_of_ms, settle_ms)
        if not candles:
            notes.append(f"{tf}: 无已收盘 K 线")
            continue
        seen.append(tf)
        views[tf], atr_hist[tf] = _view(tf, candles, as_of_ms)
        tf_gaps = gaps(candles)
        per_tf[tf] = {
            "close_ms": views[tf].last_closed_ms,
            "age_ms": views[tf].age_ms,
            "stale": views[tf].stale,
            "gaps": len(tf_gaps),
        }
        for g in tf_gaps:
            all_gaps.append({"timeframe": tf, **g})

    critical = views.get("1h")
    critical_stale = bool(critical and critical.stale)
    contiguous = bool(critical) and per_tf.get("1h", {}).get("gaps", 1) == 0
    last_closed = max((v.last_closed_ms for v in views.values()), default=0)
    age = (as_of_ms - last_closed) if last_closed else 0
    any_stale = any(v.stale for v in views.values())
    dq = DataQuality(
        timeframes_seen=tuple(seen),
        gaps=tuple(all_gaps[:max_gaps]),
        last_closed_ms=last_closed,
        age_ms=max(0, age),
        stale=critical_stale,          # 语义：趋势数据是否陈旧
        complete=bool(seen) and not all_gaps and not any_stale,
        notes=tuple(notes),
        per_timeframe=per_tf,
        critical_stale=critical_stale,
        contiguous=contiguous,
    )

    if critical is None:
        return MarketState(
            symbol=symbol, as_of_ms=as_of_ms, trend="UNKNOWN", volatility="UNKNOWN",
            momentum="UNKNOWN", risk="UNKNOWN" if not seen else "RISK_HIGH",
            confidence=Decimal("0.05"), reason="缺少 1h 已收盘 K 线，无法判断",
            evidence=tuple(evidence), contradictions=tuple(contradictions),
            invalidations=_invalidations("UNKNOWN", "UNKNOWN"),
            data_quality=dq, timeframes=views,
        )

    trend, trend_reason = _trend(critical, evidence)
    vol, pct = _volatility(critical, atr_hist["1h"]["atr_pct"], evidence)
    momentum = _momentum(critical, evidence)
    features = _features(
        closed_only(sorted(candles_by_tf.get("1h") or [], key=lambda c: c.open_ms), as_of_ms, settle_ms),
        critical, atr_hist["1h"]["atr"],
    )

    tf4 = views.get("4h")
    if tf4 is not None and tf4.ema20 is not None and tf4.ema60 is not None:
        if trend == "TREND_UP" and tf4.ema20 < tf4.ema60:
            contradictions.append(Contradiction("1h_up_vs_4h_down", "1h 多头排列", "4h EMA20<EMA60"))
        if trend == "TREND_DOWN" and tf4.ema20 > tf4.ema60:
            contradictions.append(Contradiction("1h_down_vs_4h_up", "1h 空头排列", "4h EMA20>EMA60"))
    if trend == "TREND_UP" and momentum == "MOMENTUM_DOWN":
        contradictions.append(Contradiction("trend_up_macd_down", "1h 趋势向上", "MACD 柱为负"))
    if trend == "TREND_DOWN" and momentum == "MOMENTUM_UP":
        contradictions.append(Contradiction("trend_down_macd_up", "1h 趋势向下", "MACD 柱为正"))

    risk = _risk(vol, dq, pct)
    conf = Decimal("0.50")
    if trend in ("TREND_UP", "TREND_DOWN"):
        conf += Decimal("0.15")
    if tf4 is not None and not any(c.code.startswith("1h_") for c in contradictions):
        conf += Decimal("0.10")
    if critical.adx is not None and critical.adx >= Decimal("30"):
        conf += Decimal("0.10")
    if (trend == "TREND_UP" and momentum == "MOMENTUM_UP") or (
            trend == "TREND_DOWN" and momentum == "MOMENTUM_DOWN"):
        conf += Decimal("0.05")
    conf -= Decimal("0.10") * len(contradictions)
    if per_tf.get("1h", {}).get("gaps"):
        conf -= Decimal("0.15")
    if any_stale and not critical_stale:
        conf -= Decimal("0.05")
    if critical_stale:
        conf = min(conf, Decimal("0.35"))
    if trend == "UNKNOWN":
        conf = Decimal("0.15")
    conf = max(Decimal("0.05"), min(Decimal("0.95"), conf))

    reason = f"{trend_reason}；波动 {vol}，动量 {momentum}，风险 {risk}"
    if contradictions:
        reason += f"；{len(contradictions)} 处冲突"
    if all_gaps:
        reason += f"；{len(all_gaps)} 处数据缺口"
    if critical_stale:
        reason += "；1h 数据陈旧"

    return MarketState(
        symbol=symbol, as_of_ms=as_of_ms, trend=trend, volatility=vol,
        momentum=momentum, risk=risk, confidence=conf, reason=reason,
        evidence=tuple(evidence), contradictions=tuple(contradictions),
        invalidations=_invalidations(trend, vol, dq), data_quality=dq,
        timeframes=views, features=features,
    )


def _risk(vol: str, dq: DataQuality, pct: Decimal | None) -> str:
    h1 = dq.per_timeframe.get("1h", {})
    h1_gaps = int(h1.get("gaps", 0) or 0)
    if dq.critical_stale or h1_gaps >= 3:
        return "RISK_EXTREME"
    if h1_gaps >= 1:
        return "RISK_HIGH"
    if vol == "HIGH_VOL" or (pct is not None and pct >= Decimal("0.95")):
        return "RISK_HIGH"
    if vol == "LOW_VOL":
        return "RISK_LOW"
    if vol == "UNKNOWN":
        return "UNKNOWN"
    return "RISK_NORMAL"


def _invalidations(trend: str, vol: str, dq: DataQuality | None = None) -> tuple[str, ...]:
    base: list[str]
    if trend == "TREND_UP":
        base = [
            "1h ADX 跌破 20（趋势强度消失）",
            "1h EMA20 下穿 EMA60（排列被破坏）",
            "出现 >=2 根 1h 缺口或 1h 数据陈旧 > 2 小时",
        ]
    elif trend == "TREND_DOWN":
        base = [
            "1h ADX 跌破 20",
            "1h EMA20 上穿 EMA60",
            "出现 >=2 根 1h 缺口或 1h 数据陈旧 > 2 小时",
        ]
    elif trend == "RANGE":
        base = [
            "1h ADX 升破 25 且 EMA20/60/200 同向排列（转为趋势）",
            "区间边界被收盘价有效突破（需成交量确认）",
        ]
    else:
        base = ["补齐 >=205 根已收盘 1h K 线且无缺口"]
    if dq is not None and (dq.critical_stale or not dq.contiguous):
        base = base + ["1h 数据恢复连续且收盘时间在 2 小时内（当前数据门未通过）"]
    return tuple(base)