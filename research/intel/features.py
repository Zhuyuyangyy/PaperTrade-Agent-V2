"""五类特征（Alpha Discovery V1）：T / N / P / W / L。

全部按"决策时刻可用"生成，输出归一化到 [-1, 1]（计数类用 0..1）。
特征只是模型输入，不代表任何一类情报有效；有效性由消融实验回答。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from research.market.contracts import MarketState

from .contracts import IntelDigest, WalletSignal

D = Decimal


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _trend_dir(trend: str) -> float:
    return 1.0 if trend == "TREND_UP" else -1.0 if trend == "TREND_DOWN" else 0.0


def _momentum_dir(m: str) -> float:
    return 1.0 if m == "MOMENTUM_UP" else -1.0 if m == "MOMENTUM_DOWN" else 0.0


def _dec(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class IntelFeatures:
    technical: dict[str, float] = field(default_factory=dict)
    news: dict[str, float] = field(default_factory=dict)
    prediction: dict[str, float] = field(default_factory=dict)
    wallet: dict[str, float] = field(default_factory=dict)
    microstructure: dict[str, float] = field(default_factory=dict)

    def score(self, weights: dict[str, float]) -> float:
        parts = {
            "technical": self.technical.get("bias", 0.0),
            "news": self.news.get("bias", 0.0),
            "prediction": self.prediction.get("bias", 0.0),
            "wallet": self.wallet.get("bias", 0.0),
            "microstructure": self.microstructure.get("bias", 0.0),
        }
        return sum(weights.get(k, 0.0) * v for k, v in parts.items())

    def to_dict(self) -> dict:
        return {
            "technical": self.technical,
            "news": self.news,
            "prediction": self.prediction,
            "wallet": self.wallet,
            "microstructure": self.microstructure,
        }


def compute(state: MarketState, digest: IntelDigest | None,
            wallet_signals: tuple[WalletSignal, ...] = ()) -> IntelFeatures:
    f = state.features
    adx = _dec(f.get("adx"))
    rsi = _dec(f.get("rsi"))
    atr_pct = _dec(f.get("atr_pct"))
    vz = _dec(f.get("volume_z"))
    technical = {
        "bias": _clamp(0.6 * _trend_dir(state.trend) + 0.4 * _momentum_dir(state.momentum)),
        "trend": _trend_dir(state.trend),
        "momentum": _momentum_dir(state.momentum),
        "adx": _clamp(adx / 50.0, 0.0, 1.0),
        "rsi": _clamp((rsi - 50.0) / 50.0),
        "atr_pct": _clamp(atr_pct * 10_000 / 200.0, 0.0, 1.0),
    }

    news_bias = 0.0
    news = {"bias": 0.0, "count": 0.0, "confidence": 0.0, "injection": 0.0}
    if digest and digest.news:
        signed = []
        confs = []
        for e in digest.news:
            sign = 1.0 if e.direction == "bullish" else -1.0 if e.direction == "bearish" else 0.0
            c = _dec(e.confidence)
            signed.append(sign * c)
            confs.append(c)
        news_bias = _clamp(sum(signed) / max(1, len(signed)))
        news = {
            "bias": news_bias,
            "count": min(1.0, len(digest.news) / 10.0),
            "confidence": _clamp(sum(confs) / max(1, len(confs)), 0.0, 1.0),
            "injection": 1.0 if any(e.injection_suspected for e in digest.news) else 0.0,
        }

    pred_bias = 0.0
    prediction = {"bias": 0.0, "researchable": 0.0, "spread": 0.0, "delta": 0.0}
    if digest and digest.markets:
        signed = []
        spreads = []
        for m in digest.markets:
            if not m.researchable or m.delta is None:
                continue
            sign = 1.0 if m.comparator == "ABOVE" else -1.0 if m.comparator == "BELOW" else 0.0
            signed.append(sign * _dec(m.delta))
            if m.spread_bps is not None:
                spreads.append(_dec(m.spread_bps))
        pred_bias = _clamp(sum(signed) * 5.0) if signed else 0.0
        prediction = {
            "bias": pred_bias,
            "researchable": min(1.0, len(signed) / 3.0),
            "spread": _clamp((sum(spreads) / len(spreads)) / 200.0, 0.0, 1.0) if spreads else 0.0,
            "delta": _clamp(max((abs(x) for x in signed), default=0.0) * 5.0),
        }

    wallet_bias = 0.0
    wallet = {"bias": 0.0, "copyable": 0.0, "count": 0.0}
    if wallet_signals:
        signed = []
        for w in wallet_signals:
            sign = 1.0 if w.side == "BUY" else -1.0
            size = _dec(w.size_usd)
            signed.append(sign * min(1.0, size / 10_000.0) * (1.0 if w.copyable else 0.3))
        wallet_bias = _clamp(sum(signed) / max(1, len(signed)))
        copyable = sum(1 for w in wallet_signals if w.copyable)
        wallet = {
            "bias": wallet_bias,
            "copyable": copyable / max(1, len(wallet_signals)),
            "count": min(1.0, len(wallet_signals) / 10.0),
        }

    micro = {
        "bias": _clamp(0.5 * _clamp(vz / 3.0) + 0.5 * _clamp(atr_pct * 10_000 / 200.0, 0.0, 1.0)),
        "volume_z": _clamp(vz / 3.0),
        "atr_pct": _clamp(atr_pct * 10_000 / 200.0, 0.0, 1.0),
    }
    return IntelFeatures(technical, news, prediction, wallet, micro)