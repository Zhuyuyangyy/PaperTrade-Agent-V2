"""轻量线性模型（逻辑回归）作为 DecisionProvider。

纯 Python 实现，不依赖 LightGBM/sklearn；训练结果可导出 JSON（权重 + 特征名），
Windows 电脑加载即可推理，满足"训练资产可迁移"的要求。

未训练 → UNAVAILABLE；绝不提供"看起来合理"的默认权重。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..contracts import Decision, MarketSnapshot, unavailable

FEATURES = (
    "trend_dir", "momentum_dir", "adx_norm", "rsi_norm", "atr_norm",
    "news_bias", "pred_bias", "wallet_bias", "micro_bias",
)


def flatten(snapshot: MarketSnapshot) -> dict[str, float]:
    s = snapshot.state
    f = s.features
    intel = snapshot.intel

    def num(v) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    trend = 1.0 if s.trend == "TREND_UP" else -1.0 if s.trend == "TREND_DOWN" else 0.0
    mom = 1.0 if s.momentum == "MOMENTUM_UP" else -1.0 if s.momentum == "MOMENTUM_DOWN" else 0.0
    news = prediction = wallet = micro = 0.0
    if intel is not None:
        news = float(intel.news.get("bias", 0.0))
        prediction = float(intel.prediction.get("bias", 0.0))
        wallet = float(intel.wallet.get("bias", 0.0))
        micro = float(intel.microstructure.get("bias", 0.0))
    return {
        "trend_dir": trend,
        "momentum_dir": mom,
        "adx_norm": min(1.0, num(f.get("adx")) / 50.0),
        "rsi_norm": max(-1.0, min(1.0, (num(f.get("rsi")) - 50.0) / 50.0)),
        "atr_norm": min(1.0, num(f.get("atr_pct")) * 10_000 / 200.0),
        "news_bias": news,
        "pred_bias": prediction,
        "wallet_bias": wallet,
        "micro_bias": micro,
    }


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass(frozen=True)
class LogisticModel:
    weights: dict[str, float]
    bias: float
    samples: int = 0
    trained_at_ms: int = 0

    def predict(self, x: dict[str, float]) -> float:
        z = self.bias + sum(self.weights.get(k, 0.0) * x.get(k, 0.0) for k in FEATURES)
        return _sigmoid(z)

    def to_dict(self) -> dict:
        return {"weights": dict(self.weights), "bias": self.bias,
                "samples": self.samples, "trained_at_ms": self.trained_at_ms,
                "features": list(FEATURES)}

    @classmethod
    def from_dict(cls, data: dict) -> "LogisticModel":
        weights = {k: float(v) for k, v in (data.get("weights") or {}).items()}
        return cls(weights, float(data.get("bias", 0.0)),
                   int(data.get("samples", 0)), int(data.get("trained_at_ms", 0)))

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "LogisticModel":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def train(samples: list[dict[str, float]], labels: list[int], *,
          l2: float = 0.01, lr: float = 0.2, epochs: int = 400) -> LogisticModel:
    """labels: 1 = 上涨/做多，0 = 下跌/做空。数据不足直接拒绝。"""
    if len(samples) != len(labels) or len(samples) < 30:
        raise ValueError("训练样本不足（需要 >=30 且逐点对齐）")
    w = {k: 0.0 for k in FEATURES}
    b = 0.0
    n = len(samples)
    for _ in range(epochs):
        gw = {k: 0.0 for k in FEATURES}
        gb = 0.0
        for x, y in zip(samples, labels):
            p = _sigmoid(b + sum(w[k] * x.get(k, 0.0) for k in FEATURES))
            err = p - y
            for k in FEATURES:
                gw[k] += err * x.get(k, 0.0)
            gb += err
        for k in FEATURES:
            w[k] -= lr * (gw[k] / n + l2 * w[k])
        b -= lr * gb / n
    return LogisticModel(w, b, n, int(time.time() * 1000))


class LinearProvider:
    name = "linear"
    version = "logit-v1"

    def __init__(self, model: LogisticModel | None = None):
        self.model = model

    def available(self) -> bool:
        return self.model is not None and bool(self.model.weights)

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        if not self.available():
            return unavailable(self.name, self.version, "LINEAR_UNAVAILABLE: model not trained")
        x = flatten(snapshot)
        p = self.model.predict(x)
        if p > 0.55:
            choice, score = "LONG", p * 2 - 1
        elif p < 0.45:
            choice, score = "SHORT", p * 2 - 1
        else:
            choice, score = "HOLD", 0.0
        conf = Decimal(str(round(abs(score), 4)))
        return Decision(self.name, self.version, "OK", choice, score, conf,
                        f"logit p={p:.3f} (n={self.model.samples})",
                        tuple(f"{k}={x[k]:.2f}" for k in FEATURES[:4]))