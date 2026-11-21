"""市场状态机（Sprint 1）——把已收盘多周期 K 线变成可解释的 MarketState。

判定顺序：
  1. 数据质量先于观点：缺口、陈旧、K 线不足都进入 data_quality，并直接影响 risk；
  2. 趋势看 1h 的 EMA 排列 + ADX（4h 只做一致性校验，不单独定方向）；
  3. 波动率用 1h ATR% 的历史分位，而不是绝对阈值（不同币种/时期不可比）；
  4. 动量看 MACD 柱 + RSI + 成交量 z；
  5. 冲突显式记录（1h vs 4h、趋势 vs MACD、价格新高 vs 量能萎缩），并扣 confidence；
  6. 数据不足 → UNKNOWN，并写明缺什么；绝不硬猜。
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
    TF_4H_MS,
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

MIN_TREND_BARS = 205      # EMA200 + 缓冲
VOL_LOOKBACK = 200
DEFAULT_SETTLE_MS = 30_000


def _last(seq: list[Decimal | None]) -> Decimal | None:
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _at(seq: list[Decimal | None], i: int) -> Decimal | None:
    return seq[i] if 0 <= i < len(seq) else None


def _view(tf: str, candles: list[Candle]) -> tuple[TimeframeView, dict[str, list[Decimal | None]]]:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    i = len(candles) - 1
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
    view = TimeframeView(
        timeframe=tf,
        last_closed_ms=candles[i].open_ms,
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
    )
    return view, {"atr_pct": atr_pct_series}


def _trend(view: TimeframeView, evidence: list[Evidence], contradictions: list[Contradiction]) -> tuple[str, str]:
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


def _volatility(view: TimeframeView, atr_series: list[Decimal | None],
                evidence: list[Evidence]) -> tuple[str, Decimal | None]:
    cur = view.atr_pct
    if cur is None:
        return "UNKNOWN", None
    history = [v for v in atr_series[-VOL_LOOKBACK:] if v is not None]
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


def _risk(vol: str, dq: DataQuality, pct: Decimal | None) -> str:
    if dq.stale or not dq.complete:
        if dq.stale or len(dq.gaps) >= 3:
            return "RISK_EXTREME"
        return "RISK_HIGH"
    if vol == "HIGH_VOL" or (pct is not None and pct >= Decimal("0.95")):
        return "RISK_HIGH"
    if vol == "LOW_VOL":
        return "RISK_LOW"
    if vol == "UNKNOWN":
        return "UNKNOWN"
    return "RISK_NORMAL"


def _invalidations(trend: str, vol: str) -> tuple[str, ...]:
    if trend == "TREND_UP":
        return (
            "1h ADX 跌破 20（趋势强度消失）",
            "1h EMA20 下穿 EMA60（排列被破坏）",
            "出现 >=2 根 1h 缺口或数据陈旧 > 2 小时",
        )
    if trend == "TREND_DOWN":
        return (
            "1h ADX 跌破 20",
            "1h EMA20 上穿 EMA60",
            "出现 >=2 根 1h 缺口或数据陈旧 > 2 小时",
        )
    if trend == "RANGE":
        return (
            "1h ADX 升破 25 且 EMA20/60/200 同向排列（转为趋势）",
            "区间边界被收盘价有效突破（需成交量确认）",
        )
    return ("补齐 >=205 根已收盘 1h K 线且无缺口",)


def analyze(symbol: str, candles_by_tf: Mapping[str, list[Candle]], as_of_ms: int,
            *, settle_ms: int = DEFAULT_SETTLE_MS, max_gaps: int = 10) -> MarketState:
    """装配 MarketState。``candles_by_tf`` 的键是 '5m'/'15m'/'1h'/'4h'。"""
    evidence: list[Evidence] = []
    contradictions: list[Contradiction] = []
    views: dict[str, TimeframeView] = {}
    atr_hist: dict[str, list[Decimal | None]] = {}
    all_gaps: list[dict[str, int]] = []
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
        views[tf], atr_hist[tf] = _view(tf, candles)
        for g in gaps(candles):
            all_gaps.append({"timeframe": tf, **g})

    dq_gaps = tuple(all_gaps[:max_gaps])
    last_closed = max((v.last_closed_ms for v in views.values()), default=0)
    age = (as_of_ms - last_closed) if last_closed else 0
    stale = bool(last_closed) and age > 2 * TF_1H_MS
    complete = bool(seen) and not all_gaps and not stale
    dq = DataQuality(
        timeframes_seen=tuple(seen),
        gaps=dq_gaps,
        last_closed_ms=last_closed,
        age_ms=max(0, age),
        stale=stale,
        complete=complete,
        notes=tuple(notes),
    )

    main = views.get("1h")
    if main is None:
        return MarketState(
            symbol=symbol, as_of_ms=as_of_ms, trend="UNKNOWN", volatility="UNKNOWN",
            momentum="UNKNOWN", risk="UNKNOWN" if not seen else "RISK_HIGH",
            confidence=Decimal("0.05"), reason="缺少 1h 已收盘 K 线，无法判断",
            evidence=tuple(evidence), contradictions=tuple(contradictions),
            invalidations=_invalidations("UNKNOWN", "UNKNOWN"), data_quality=dq,
            timeframes=views,
        )

    trend, trend_reason = _trend(main, evidence, contradictions)
    vol, pct = _volatility(main, atr_hist["1h"]["atr_pct"], evidence)
    momentum = _momentum(main, evidence)

    # 4h 一致性（只做校验，不单独定方向）
    tf4 = views.get("4h")
    if tf4 is not None and tf4.ema20 is not None and tf4.ema60 is not None:
        if trend == "TREND_UP" and tf4.ema20 < tf4.ema60:
            contradictions.append(Contradiction("1h_up_vs_4h_down", "1h 多头排列", "4h EMA20<EMA60"))
        if trend == "TREND_DOWN" and tf4.ema20 > tf4.ema60:
            contradictions.append(Contradiction("1h_down_vs_4h_up", "1h 空头排列", "4h EMA20>EMA60"))

    # 趋势与动量冲突
    if trend == "TREND_UP" and momentum == "MOMENTUM_DOWN":
        contradictions.append(Contradiction("trend_up_macd_down", "1h 趋势向上", "MACD 柱为负"))
    if trend == "TREND_DOWN" and momentum == "MOMENTUM_UP":
        contradictions.append(Contradiction("trend_down_macd_up", "1h 趋势向下", "MACD 柱为正"))

    risk = _risk(vol, dq, pct)
    conf = Decimal("0.50")
    if trend in ("TREND_UP", "TREND_DOWN"):
        conf += Decimal("0.15")
    if tf4 is not None and tf4.ema20 is not None and tf4.ema60 is not None:
        if not any(c.code.startswith("1h_") for c in contradictions):
            conf += Decimal("0.10")
    if main.adx is not None and main.adx >= Decimal("30"):
        conf += Decimal("0.10")
    if (trend == "TREND_UP" and momentum == "MOMENTUM_UP") or (
            trend == "TREND_DOWN" and momentum == "MOMENTUM_DOWN"):
        conf += Decimal("0.05")
    conf -= Decimal("0.10") * len(contradictions)
    if dq_gaps:
        conf -= Decimal("0.10")
    if stale:
        conf -= Decimal("0.20")
    if trend == "UNKNOWN":
        conf = Decimal("0.15")
    if stale:
        conf = min(conf, Decimal("0.40"))
    conf = max(Decimal("0.05"), min(Decimal("0.95"), conf))

    reason = f"{trend_reason}；波动 {vol}，动量 {momentum}，风险 {risk}"
    if contradictions:
        reason += f"；{len(contradictions)} 处冲突"
    if dq_gaps:
        reason += f"；{len(dq_gaps)} 处数据缺口"

    return MarketState(
        symbol=symbol, as_of_ms=as_of_ms, trend=trend, volatility=vol,
        momentum=momentum, risk=risk, confidence=conf, reason=reason,
        evidence=tuple(evidence), contradictions=tuple(contradictions),
        invalidations=_invalidations(trend, vol), data_quality=dq, timeframes=views,
    )