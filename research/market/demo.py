"""合成行情（Sprint 1）——只用于软件测试与 demo，绝不当真实数据。

三种 regime 各生成 14880 根 1m（= 248 根 1h = 62 根 4h），确定性 seed：
  - ``trend_up``：稳定上行 + 小噪声，EMA 多头排列、ADX 高；
  - ``range``：正弦震荡，ADX 低；
  - ``high_vol``：大噪声，ATR% 抬升。

所有价格都用 Decimal 构造（先算 float，再一次性转 Decimal 并不参与后续比较之外的计算）。
"""
from __future__ import annotations

import math
import random
from decimal import Decimal

from .contracts import Candle, TF_1H_MS, TF_4H_MS

BASE_MS = 1_795_000_000_000 // TF_4H_MS * TF_4H_MS   # 4h 对齐
ONE_MIN = 60_000
BARS_1M = 240 * 62                                   # 62 根 4h
REGIMES = ("trend_up", "range", "high_vol")


def synthetic_1m(regime: str, bars: int = BARS_1M, seed: int = 7) -> list[Candle]:
    if regime not in REGIMES:
        raise ValueError(f"未知 regime: {regime}")
    rng = random.Random(seed)
    out: list[Candle] = []
    prev_close = 100.0
    for i in range(bars):
        if regime == "trend_up":
            # 加速上行：末端 MACD 柱保持为正，RSI 在 50 以上
            level = 100.0 * (1 + 0.000018 * i + 0.0000000016 * i * i)
            noise = rng.gauss(0, 0.015)
            vol = 100 + 40 * (i / bars)
            close = level + noise
        elif regime == "range":
            # 60 分钟三角波区间（30 上 / 30 下）：1h 内方向反转，ADX 低、ATR% 稳定
            tri = 1.0 - 4.0 * abs(((i / 30.0) % 1.0) - 0.5)
            noise = rng.gauss(0, 0.005)
            close = 100.0 + 0.3 * tri + noise
            vol = 100
        else:  # high_vol：前 80% 平静、末端波动放大（ATR% 分位抬升）
            calm = i < int(bars * 0.8)
            sigma = 0.15 if calm else 1.2
            level = 100.0 + 0.9 * (prev_close - 100.0)
            noise = rng.gauss(0, sigma)
            vol = 100 + abs(rng.gauss(0, 120 if not calm else 20))
            close = level + noise
        close = max(1.0, close)
        o = prev_close
        h = max(o, close) + abs(noise) * 0.6 + 0.005
        l = min(o, close) - abs(noise) * 0.6 - 0.005
        out.append(Candle(
            BASE_MS + i * ONE_MIN,
            _q(o), _q(h), _q(l), _q(close), _q(max(0.0, vol)),
            ONE_MIN,
        ))
        prev_close = close
    return out


def _q(x: float) -> Decimal:
    return Decimal(f"{x:.6f}")


def demo_as_of_ms() -> int:
    """最后一根 1m 收盘后 1 秒（保证所有 K 线已收盘）。"""
    return BASE_MS + BARS_1M * ONE_MIN + 1_000