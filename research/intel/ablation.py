"""消融实验（Alpha Discovery V1）：逐类加入情报，看净收益是否真的改善。

纪律：
  - 同一批决策点、同一标签，逐点配对；
  - 基线是纯技术（T），每个变体只加一类情报；
  - 输出净 bps、命中率、Profit Factor、最大回撤、相对基线的 Block Bootstrap CI；
  - 样本 < 30 → INCONCLUSIVE；结论只对这批决策点成立。
"""
from __future__ import annotations

from decimal import Decimal

from research.cross_market.experiment import block_bootstrap_ci

MIN_SAMPLES = 30
BASELINE = "T"


def signal(score: float, threshold: float = 0.2) -> int:
    if score > threshold:
        return 1
    if score < -threshold:
        return -1
    return 0


def _metrics(payoffs: list[float]) -> dict:
    n = len(payoffs)
    if not n:
        return {"n": 0, "net_bps": None, "hit": None, "pf": None, "max_dd_bps": None}
    wins = [p for p in payoffs if p > 0]
    losses = [p for p in payoffs if p < 0]
    equity, peak, worst = 0.0, 0.0, 0.0
    for p in payoffs:
        equity += p
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return {
        "n": n,
        "net_bps": round(sum(payoffs) / n, 3),
        "hit": round(len(wins) / n, 3),
        "pf": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "max_dd_bps": round(worst, 3),
    }


def evaluate(points: list[dict], labels_bps: list[float],
             weights: dict[str, dict[str, float]],
             *, min_samples: int = MIN_SAMPLES, block: int = 1) -> dict:
    """points: [{"technical": 0.5, "news": 0.1, ...}]；labels_bps: 同一决策点的未来收益。"""
    if len(points) != len(labels_bps):
        raise ValueError("points 与 labels 必须逐点对齐")
    base_weights = weights.get(BASELINE)
    if base_weights is None:
        raise ValueError("weights 必须包含基线 T")
    base_pay = [signal(w["technical"] * base_weights.get("technical", 0.0)) * l
                for w, l in zip(points, labels_bps)]
    out = {"baseline": BASELINE, "n": len(points), "variants": {}}
    for name, ws in weights.items():
        pay = []
        for point, label in zip(points, labels_bps):
            score = (ws.get("technical", 0.0) * point.get("technical", 0.0)
                     + ws.get("news", 0.0) * point.get("news", 0.0)
                     + ws.get("prediction", 0.0) * point.get("prediction", 0.0)
                     + ws.get("wallet", 0.0) * point.get("wallet", 0.0)
                     + ws.get("microstructure", 0.0) * point.get("microstructure", 0.0))
            pay.append(signal(score) * label)
        m = _metrics(pay)
        if len(pay) >= min_samples:
            point, lo, hi = block_bootstrap_ci(pay, base_pay, block=block, iters=1000)
            m["ci_vs_baseline"] = [round(point, 3), round(lo, 3), round(hi, 3)]
            m["verdict"] = "SUPPORTED" if lo > 0 else "FALSIFIED" if hi < 0 else "INCONCLUSIVE"
        else:
            m["ci_vs_baseline"] = None
            m["verdict"] = "INCONCLUSIVE"
        out["variants"][name] = m
    return out