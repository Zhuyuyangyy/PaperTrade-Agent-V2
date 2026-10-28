"""FundingRateEnhanced — carry with a directional filter, on USDT-perps.

Ported from realm520/FreqBot (user_data/strategies/FundingRateEnhancedStrategy.py,
defaults as shipped). The original trades 15m futures with the funding rate as
the primary alpha: short while funding is high and persistent (collect it),
long while it is deeply negative, gated by RSI/ADX/ATR and an explicit
cost-versus-expected-return check.

Port fidelity notes:
  * Funding is the 8h series from Binance futures, held flat onto the 15m bars
    it governs — the same "value in force" rule the original uses.
  * expected_funding_return = |rate| x 3 periods, verbatim; the entry gate
    requires it to exceed max_trading_cost x min_profit_ratio.
  * The exit stack is ported in the original's precedence: hard stop -1.5%,
    then profit-ratio stop, then 48h time stop, then trailing take-profit
    (armed at +0.5%, distance 0.2%); funding-reversal and technical-reversal
    exits fill at the bar close.
  * No LICENSE file ships with the repo; the port is for research measurement
    only and says so.

Their claim to test: funding is a structural cash flow rather than a zero-sum
spread — the one candidate here whose edge is not "predict the price". The
README's "年化 15-35%" is marketing until this audit reproduces it.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, adx, atr, rsi
from ..pathsim import Exit

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
         "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "ETCUSDT"]


class FundingRateEnhanced(Strategy):
    def __init__(self, pairs: list[str] | None = None,
                 short_threshold: float = 0.0003, long_threshold: float = -0.0005,
                 persistence: int = 3, hard_stop: float = -0.015,
                 max_holding_hours: int = 48,
                 trailing_start: float = 0.005, trailing_distance: float = 0.002,
                 reversal_threshold: float = 0.00005,
                 min_profit_ratio: float = 2.0, max_cost: float = 0.001):
        super().__init__(
            name="FundingRateEnhanced",
            source="realm520/FreqBot user_data/strategies/FundingRateEnhancedStrategy.py",
            timeframe="15m",
            pairs=pairs or PAIRS,
            exit_rule=Exit(take_profit=None, stop_loss=hard_stop,
                           trailing=trailing_distance, trailing_offset=trailing_start,
                           timeout_bars=max_holding_hours * 4),  # 15m bars
            hypothesis="资金费率是结构性现金流而非零和价差：高正费率做空收取、深度负费率做多收取，"
                       "技术面只做过滤不做方向。只要费率持续，方向错了也只是浮亏。",
            falsify="在本仪器的 path-aware 回放下，若 12 个币合计（含资金费收支）净期望 <= 0，"
                    "则费差 edge 在扣除成本后不存在，淘汰。",
        )
        self.short_threshold = short_threshold
        self.long_threshold = long_threshold
        self.persistence = persistence
        self.hard_stop = hard_stop
        self.reversal_threshold = reversal_threshold
        self.min_profit_ratio = min_profit_ratio
        self.max_cost = max_cost
        self.trailing_distance = trailing_distance
        self.trailing_start = trailing_start
        self.rsi_period = 14
        self.rsi_overbought, self.rsi_oversold = 70, 30
        self.adx_max, self.atr_max = 40, 0.05

    # ---------- funding mapped onto bars ----------
    def _funding_on_bars(self, candles: list[dict], funding: list[dict]) -> tuple:
        times = pd.Series([c["t"] for c in candles])
        rate = pd.Series([float("nan")] * len(candles))
        if funding:
            f_series = pd.Series([f["rate"] for f in funding],
                                 index=[f["t"] for f in funding])
            # value in force: last funding print at or before each bar
            joined = pd.merge_asof(times.sort_values(), f_series.sort_index(),
                                   left_index=True, right_index=True,
                                   direction="backward")
            rate = joined.set_index(times.sort_values().values)[0.0].reindex(times.values)
        return times, rate.reset_index(drop=True)

    def signals(self, candles: list[dict], funding: list[dict] | None = None,
                **kwargs) -> dict[int, str]:
        if len(candles) < 220:
            return {}
        df = pd.DataFrame(candles)
        _, rate = self._funding_on_bars(candles, funding or [])
        df["funding_rate"] = rate.values
        df["funding_ma"] = df["funding_rate"].rolling(16, min_periods=4).mean()
        # persistence: consecutive same-sign prints (8h cadence on 15m bars)
        sign = df["funding_rate"].apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        run = sign.groupby((sign != sign.shift()).cumsum()).cumcount() + 1
        df["funding_pos_pers"] = run.where(sign > 0, 0)
        df["funding_neg_pers"] = run.where(sign < 0, 0)
        df["rsi"] = rsi(df["c"], self.rsi_period)
        df["adx"], df["plus_di"], df["minus_di"] = adx(df, 14)
        df["atr"] = atr(df, 14)
        df["atr_percent"] = df["atr"] / df["c"]
        df["volume_ma"] = df["v"].rolling(96, min_periods=24).mean()
        # expected return = |rate| x 3 periods, verbatim from the original
        df["expected_funding_return"] = df["funding_rate"].abs() * 3
        cost_ok = df["expected_funding_return"] > self.max_cost * self.min_profit_ratio

        short_cond = (
            (df["funding_rate"] > self.short_threshold)
            & (df["funding_pos_pers"] >= self.persistence)
            & (df["funding_rate"] > df["funding_ma"])
            & (df["rsi"] > self.rsi_oversold) & (df["rsi"] < self.rsi_overbought)
            & (df["adx"] < self.adx_max) & (df["atr_percent"] < self.atr_max)
            & cost_ok & (df["v"] > 0) & (df["v"] > df["volume_ma"] * 0.5)
        )
        long_cond = (
            (df["funding_rate"] < self.long_threshold)
            & (df["funding_neg_pers"] >= self.persistence)
            & (df["funding_rate"] < df["funding_ma"])
            & (df["rsi"] < self.rsi_overbought) & (df["rsi"] > self.rsi_oversold)
            & (df["adx"] < self.adx_max) & (df["atr_percent"] < self.atr_max)
            & cost_ok & (df["v"] > 0) & (df["v"] > df["volume_ma"] * 0.5)
        )
        out: dict[int, str] = {}
        for i in df.index[short_cond.fillna(False)]:
            if i + 1 < len(df):
                out[int(i) + 1] = "short"
        for i in df.index[long_cond.fillna(False)]:
            if i + 1 < len(df):
                out[int(i) + 1] = "long"
        return out

    def exit_signals(self, candles: list[dict], funding: list[dict] | None = None,
                     **kwargs) -> dict[int, str]:
        """Funding reversal and technical reversal, filling at the close."""
        if len(candles) < 220:
            return {}
        df = pd.DataFrame(candles)
        _, rate = self._funding_on_bars(candles, funding or [])
        df["funding_rate"] = rate.values
        df["funding_ma"] = df["funding_rate"].rolling(16, min_periods=4).mean()
        df["rsi"] = rsi(df["c"], self.rsi_period)
        df["adx"], df["plus_di"], df["minus_di"] = adx(df, 14)
        long_exit = (
            (df["funding_rate"] > self.reversal_threshold)
            | (df["funding_rate"] > df["funding_ma"] * 0.5)
            | ((df["rsi"] > self.rsi_overbought) & (df["adx"] > 30) & (df["minus_di"] > df["plus_di"]))
        )
        short_exit = (
            (df["funding_rate"] < self.reversal_threshold)
            | (df["funding_rate"] < df["funding_ma"] * 0.5)
            | ((df["rsi"] < self.rsi_oversold) & (df["adx"] > 30) & (df["plus_di"] > df["minus_di"]))
        )
        out: dict[int, str] = {}
        for i in df.index[(long_exit | short_exit).fillna(False)]:
            out[int(i)] = "funding_or_ta_reversal"
        return out
