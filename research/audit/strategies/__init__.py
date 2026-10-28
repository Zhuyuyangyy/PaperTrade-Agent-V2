"""Ported candidate strategies. Each names the repo it came from."""
from .base import Strategy
from .cascade_fader import CascadeFader
from .funding_rate import FundingRateEnhanced
from .trendrider import TrendRider

__all__ = ["Strategy", "CascadeFader", "FundingRateEnhanced", "TrendRider"]
