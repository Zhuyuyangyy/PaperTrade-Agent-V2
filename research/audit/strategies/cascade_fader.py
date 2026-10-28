"""CascadeFaderV1 — liquidation-cascade rebound fader.

Ported 1:1 from IvPalmer/Master-Trader
(ft_userdata/user_data/strategies/CascadeFaderV1.py, generated 2026-05-09).
Only the freqtrade plumbing is dropped; every threshold and the documented
lab metrics below are theirs.

Signal (1h bar, enter on the NEXT bar's open):
  (open - low) / open >= 8%      deep intra-bar drop
  (close - low) / (open - low) > 40%   wick recovered, not a falling knife
  volume > 2.0x rolling 30-day mean    panic signature
Exit: TP +3%, SL -8%, 48h unconditional timeout.

Their published lab numbers (3.3yr, 13 pairs, 0.20% round-trip taker,
path-aware bar-by-bar precedence): N=162, WR 77.8%, PF 1.62, +0.820%/trade,
annualised ~3.3% per pair. Weakness years 2024-H2 (-15%) and 2025 (-13.7%)
are published too — which is exactly what makes this a good first candidate:
reproducing their numbers validates our instrument before we trust it on
anything new.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy
from ..pathsim import Exit

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
         "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT", "LTCUSDT",
         "ATOMUSDT", "NEARUSDT"]


class CascadeFader(Strategy):
    def __init__(self, drop_pct: float = 0.08, recovery_min: float = 0.40,
                 vol_mult: float = 2.0, take_profit: float = 0.03,
                 stop_loss: float = -0.08, timeout_bars: int = 48,
                 pairs: list[str] | None = None):
        super().__init__(
            name="CascadeFaderV1",
            source="IvPalmer/Master-Trader ft_userdata/user_data/strategies/CascadeFaderV1.py",
            timeframe="1h",
            pairs=pairs or PAIRS,
            exit_rule=Exit(take_profit=take_profit, stop_loss=stop_loss,
                           timeout_bars=timeout_bars),
            hypothesis="强制流（多头连环爆仓）把价格短暂压到公允价之下；下影线收回过滤接飞刀，"
                       "放量确认恐慌，>=8% 过滤日常波动。反弹常在 1h K 线内就发生。",
            falsify="在本仪器的 path-aware 回放下，若 13 个币的合计 PF < 1.0 或年化 <= 0，"
                    "则该 edge 在我们的执行假设下不成立，淘汰。",
        )
        self.drop_pct = drop_pct
        self.recovery_min = recovery_min
        self.vol_mult = vol_mult

    def signals(self, candles: list[dict], **kwargs) -> dict[int, str]:
        # startup_candle_count is 720 in the original: the 30-day volume SMA.
        if len(candles) < 730:
            return {}
        df = pd.DataFrame(candles)
        wick = df["o"] - df["l"]
        open_to_low = wick / df["o"]
        recovery = ((df["c"] - df["l"]) / wick.replace(0.0, float("nan"))).fillna(0.0)
        vol_sma = df["v"].rolling(720, min_periods=360).mean()
        vol_ratio = df["v"] / vol_sma.replace(0.0, float("nan"))

        mask = (
            (open_to_low >= self.drop_pct)
            & (recovery > self.recovery_min)
            & (vol_ratio > self.vol_mult)
            & (vol_sma > 0)
        )
        # enter on the next bar: the signal is only known at bar close
        return {int(i) + 1: "long" for i in df.index[mask.fillna(False)] if i + 1 < len(df)}
