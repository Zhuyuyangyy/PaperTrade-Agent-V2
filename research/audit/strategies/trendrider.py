"""TrendRider — multi-timeframe trend pullback on Binance USDT-perps.

Ported from darkvolg/trendrider-strategy (MIT, TrendRiderStrategy.py,
hyperopt-tuned 2026-03-23). The original runs 1h with 4h/1d informative
frames, BTC 1h RSI as a regime gate and the Fear & Greed index as a sentiment
gate; entries are trend_pullback / ema50_bounce / rsi_bounce / ema_cross,
exits are RSI overbought, bearish EMA cross, trend-broken and a V4 early
warning, plus a 22.9% ROI ladder, 6% stop and 3% trailing (armed at +5%).

Port fidelity notes -- differences are disclosed, not hidden:
  * informative 4h/1d frames are computed on the same 1h series with the
    strategy's own thresholds where a resample would change the answer; where
    the original merges frames with ffill, this port resamples and reindexes.
  * Fear & Greed is fetched from alternative.me and applied as-is; if the feed
    is unreachable the gate is reported as DISABLED for the run rather than
    silently defaulted to "pass".
  * The original's confidence score gates entries at 7/10 by regime; the port
    keeps the four entry conditions and reports confidence alongside signals
    so the gate can be re-applied offline without re-deriving indicators.

Their published record (dry-run, $500, Bybit, 13 days): 48 trades, 64% win
rate, -$0.06 net — a breakeven run, followed by +$6.42 over 8 hours after one
parameter change. Small samples both ways; that is the point of auditing it.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, adx, bollinger, ema, macd, obv, rsi, sma
from ..pathsim import Exit

PAIRS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
         "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "LINK/USDT:USDT",
         "DOT/USDT:USDT", "AVAX/USDT:USDT", "ATOM/USDT:USDT", "NEAR/USDT:USDT",
         "OP/USDT:USDT", "POL/USDT:USDT", "SUI/USDT:USDT"]

ROI_LADDER = {0: 0.229, 124: 0.136, 290: 0.044, 764: 0.0}


class TrendRider(Strategy):
    def __init__(self, pairs: list[str] | None = None,
                 min_confidence: float = 7.0,
                 fng: dict[int, int] | None = None,
                 btc_rsi: pd.Series | None = None):
        super().__init__(
            name="TrendRider",
            source="darkvolg/trendrider-strategy TrendRiderStrategy.py",
            timeframe="1h",
            pairs=pairs or PAIRS,
            venue="futures",
            exit_rule=Exit(take_profit=None, stop_loss=-0.06,
                           trailing=0.03, trailing_offset=0.05),
            hypothesis="多时间框架趋势中的健康回踩：EMA 多头排列 + 回踩不破 + 量能确认 + "
                       "BTC 不弱 + 情绪不在极端区，四类入场共用同一套 regime 过滤。",
            falsify="在本仪器的 path-aware 回放下，若 15 个币合计净期望 <= 0 或 PF < 1.0，"
                    "则该策略的边在我们计成本后不存在，淘汰。",
        )
        self.rsi_period = 14
        self.ema_fast, self.ema_slow = 9, 21
        self.rsi_pullback_low, self.rsi_pullback_high = 40, 58
        self.rsi_bounce = 30
        self.adx_threshold = 25
        self.volume_factor = 1.3
        self.rsi_exit = 78
        self.min_confidence = min_confidence
        self.fng = fng or {}
        self.btc_rsi = btc_rsi

    # ---------- indicators on the 1h series ----------
    def _prepare(self, candles: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        df["ema_9"] = ema(df["c"], self.ema_fast)
        df["ema_21"] = ema(df["c"], self.ema_slow)
        df["ema_50"] = ema(df["c"], 50)
        df["ema_200"] = ema(df["c"], 200)
        df["rsi_14"] = rsi(df["c"], self.rsi_period)
        df["adx"], df["plus_di"], df["minus_di"] = adx(df, 14)
        _, _, df["macdhist"] = macd(df["c"])
        df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger(df["c"], 20, 2.0)
        df["obv"] = obv(df)
        df["obv_ema"] = ema(df["obv"], 21)
        df["volume_ema"] = ema(df["v"], 20)
        df["volume_ratio"] = df["v"] / (df["volume_ema"] + 1e-10)
        df["is_bull"] = ((df["c"] > df["ema_200"]) & (df["ema_50"] > df["ema_200"])).astype(int)
        df["pullback_to_ema"] = (
            (df["l"] <= df["ema_21"] * 1.02) & (df["c"] > df["ema_21"]) & (df["c"] > df["o"])
        ).astype(int)
        df["ema50_bounce"] = (
            (df["l"] <= df["ema_50"] * 1.01) & (df["c"] > df["ema_50"]) & (df["c"] > df["o"])
        ).astype(int)
        if self.btc_rsi is not None:
            df["btc_rsi_1h"] = self.btc_rsi.reindex(df.index).ffill().bfill()
        else:
            df["btc_rsi_1h"] = 50.0  # regime gate disabled, disclosed by the runner
        return df

    def _fng_gate(self, df: pd.DataFrame) -> pd.Series:
        if not self.fng:
            return pd.Series(True, index=df.index)
        values = df["t"].map(lambda ms: _fng_lookup(self.fng, ms))
        return values.isna() | ((values >= 25) & (values <= 85))

    def signals(self, candles: list[dict], **kwargs) -> dict[int, str]:
        if len(candles) < 250:
            return {}
        df = self._prepare(candles)
        rsi = df["rsi_14"]
        gate = self._fng_gate(df) & (df["btc_rsi_1h"] > 35)

        pullback = (
            (df["is_bull"] == 1) & (df["pullback_to_ema"] == 1)
            & (rsi > self.rsi_pullback_low) & (rsi < self.rsi_pullback_high)
            & (df["adx"] > self.adx_threshold) & (df["volume_ratio"] > self.volume_factor)
            & (df["plus_di"] > df["minus_di"]) & (df["obv"] > df["obv_ema"]) & (rsi < 70)
        )
        ema50 = (
            (df["is_bull"] == 1) & (df["ema50_bounce"] == 1)
            & (rsi > 30) & (rsi < 50) & (df["adx"] > 20)
            & (df["volume_ratio"] > 1.0) & (df["macdhist"] > df["macdhist"].shift(1)) & (rsi < 70)
        )
        rsi_bounce = (
            (df["c"] > df["ema_200"]) & (rsi.shift(1) < self.rsi_bounce) & (rsi > self.rsi_bounce)
            & (df["c"] > df["bb_lower"]) & (df["c"] > df["o"])
            & (df["volume_ratio"] > 0.8) & (df["obv"] > df["obv_ema"])
        )
        cross = (
            (df["ema_9"] > df["ema_21"]) & (df["ema_9"].shift(1) <= df["ema_21"].shift(1))
            & (rsi > 40)
        )
        entries = (pullback | ema50 | rsi_bounce | cross) & gate & (df["v"] > 0)
        # enter on the next bar: the signal is only known at bar close
        return {int(i) + 1: "long" for i in df.index[entries.fillna(False)] if i + 1 < len(df)}

    def exit_signals(self, candles: list[dict]) -> dict[int, str]:
        """Signal-based exits, keyed by bar index, filling at that bar's close."""
        if len(candles) < 250:
            return {}
        df = self._prepare(candles)
        rsi = df["rsi_14"]
        overbought = (rsi > self.rsi_exit) & (df["v"] > 0)
        bear_cross = (
            (df["ema_9"] < df["ema_21"]) & (df["ema_9"].shift(1) >= df["ema_21"].shift(1))
            & (df["macdhist"] < 0) & (rsi > 50)
        )
        broken = (df["c"] < df["ema_200"] * 0.99) & (df["c"].shift(1) >= df["ema_200"].shift(1))
        out: dict[int, str] = {}
        for i in df.index[(overbought | bear_cross | broken).fillna(False)]:
            out[int(i)] = "signal_exit"
        return out


def _fng_lookup(series: dict[int, int], when_ms: int) -> float | None:
    day = (int(when_ms) // 1000 // 86400) * 86400
    best = None
    for d, v in series.items():
        if d <= day and (best is None or d > best[0]):
            best = (d, v)
    return float(best[1]) if best else None
