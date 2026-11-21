"""情报增量价值评估（Sprint 3）——"有没有用"必须独立回答。

方法：同一批决策点、同一套标签，比较"仅 K 线"与"K 线 + 情报"两条策略序列的
逐点 payoff 差；用 Block Bootstrap 给 95% CI。样本不足或 CI 跨 0 → INCONCLUSIVE。

约束：
  - 输入必须**逐点对齐**（同长度、同一时间轴）；
  - 情报只能通过 ``advisory_only`` 进入研究，不得在评估前影响 Selection；
  - 结论只对"被评估的那类情报 + 那批决策点"成立，不能外推。
"""
from __future__ import annotations

from decimal import Decimal

from research.cross_market.experiment import block_bootstrap_ci

MIN_SAMPLES = 30


def evaluate_incremental(baseline_payoffs: list[float], augmented_payoffs: list[float],
                         *, block: int = 1, min_samples: int = MIN_SAMPLES,
                         iters: int = 1000) -> dict:
    if len(baseline_payoffs) != len(augmented_payoffs):
        raise ValueError("baseline 与 augmented 必须逐点对齐（同长度）")
    n = len(baseline_payoffs)
    if n < min_samples:
        return {
            "n": n, "verdict": "INCONCLUSIVE", "diff": None, "ci": None,
            "reason": f"样本 {n} < {min_samples}，不足以判断增量价值",
        }
    point, lo, hi = block_bootstrap_ci(augmented_payoffs, baseline_payoffs,
                                       block=block, iters=iters)
    if lo > 0:
        verdict = "SUPPORTED"
        reason = f"增量 {point:.2f}，CI=({lo:.2f},{hi:.2f}) 不含 0"
    elif hi < 0:
        verdict = "FALSIFIED"
        reason = f"增量 {point:.2f}，CI=({lo:.2f},{hi:.2f}) 显著为负"
    else:
        verdict = "INCONCLUSIVE"
        reason = f"增量 {point:.2f}，CI=({lo:.2f},{hi:.2f}) 含 0"
    return {"n": n, "verdict": verdict, "diff": point, "ci": (lo, hi), "reason": reason,
            "block": block}


def verdict_is_actionable(result: dict) -> bool:
    """只有 SUPPORTED 才能进入"下一步接进策略"的讨论；其余保持 advisory。"""
    return result.get("verdict") == "SUPPORTED"


def required_next_step(result: dict) -> str:
    v = result.get("verdict")
    if v == "SUPPORTED":
        return "可进入独立复现与稳健性检验；通过前仍 advisory_only"
    if v == "FALSIFIED":
        return "该类情报不参与决策；保留公示与复盘价值"
    return "扩大样本或换评估口径；在此之前不得影响 Selection"


def _d(x: Decimal) -> float:
    return float(x)