"""A/B/C 实验骨架（P1，规格 §9-§12）。

核心问题：Performance(C) − Performance(A) > 0？（Polymarket 的增量价值）
不是 C 自己能否赚钱。

预注册契约：hypothesis 在跑数据前锁定（primary horizon、特征集、模型类），
禁跑完再挑显著的那个（多重检验钓鱼）。

统计链（最小可用实现）：CCF → 方向准确率 → Block Bootstrap CI → DM 风格比较。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal


# ---------- 预注册假设 ----------

@dataclass
class Hypothesis:
    hypothesis_id: str
    market_key: str
    underlying: str
    primary_horizon_ms: int
    arms: tuple[str, ...] = ("A", "B", "C")   # A=Binance only, B=Poly only, C=both
    features: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "A": ("price", "oi_snapshot", "cvd_delta", "funding_rate"),
        "B": ("bid", "ask", "spread_bps"),
        "C": ("price", "oi_snapshot", "cvd_delta", "funding_rate", "bid", "ask", "spread_bps"),
    })
    preregistered: bool = True   # False = 未预注册（结果只配进废纸篓）


# ---------- 信号与评分 ----------

def _d(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:
        return None


def arm_signal(arm: str, feature: dict, hypothesis: Hypothesis) -> Decimal | None:
    """把特征压缩成一个方向性信号（+1/-1/0）。最小可解释模型：

    A：价格动量为正且 OI 扩张 → 多；反之为空（否则 0）。
    B：概率（mid）上移 → 多；下移 → 空。
    C：两侧同向才出手（否则 0）——增量价值若存在，应表现为 C 比 A 更稳。
    返回 None 表示该时刻无信号（ abstain ）。原始值容许字符串（Decimal 化后比较）。
    """
    feats = hypothesis.features.get(arm, ())
    if arm == "A":
        price = _d(feature.get("binance", {}).get("price"))
        oi = _d(feature.get("binance", {}).get("oi_snapshot"))
        if price is None or oi is None:
            return None
        return Decimal("1") if price > 0 and oi > 0 else None
    if arm == "B":
        bid = _d(feature.get("polymarket", {}).get("bid"))
        ask = _d(feature.get("polymarket", {}).get("ask"))
        if bid is None or ask is None:
            return None
        mid = (bid + ask) / 2
        return Decimal("1") if mid > Decimal("0.5") else Decimal("-1")
    if arm == "C":
        a = arm_signal("A", feature, hypothesis)
        b = arm_signal("B", feature, hypothesis)
        if a is None or b is None:
            return None
        return a if a == b else Decimal("0")
    return None


def hit_rate(signals: list[Decimal | None], labels: list[Decimal | None]) -> tuple[float, int]:
    """方向准确率（只在双方都有的时刻计算）。labels 为未来收益（>0 涨/<0 跌）。"""
    n = 0
    hits = 0
    for s, l in zip(signals, labels):
        if s is None or l is None or s == 0:
            continue
        n += 1
        if (s > 0 and l > 0) or (s < 0 and l < 0):
            hits += 1
    return (hits / n if n else 0.0), n


# ---------- Block Bootstrap（时间序列，禁有放回打乱时序） ----------

def block_bootstrap_ci(x: list[float], y: list[float], block: int = 20,
                       iters: int = 1000, seed: int = 7) -> tuple[float, float, float]:
    """mean(x−y) 的 block bootstrap 95% CI。返回 (diff, lo, hi)。"""
    if len(x) != len(y) or not x:
        return (0.0, 0.0, 0.0)
    diffs = [a - b for a, b in zip(x, y)]
    n = len(diffs)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(diffs[start:start + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    point = sum(diffs) / n
    return (point, lo, hi)


# ---------- A/B/C 运行与裁定 ----------

@dataclass
class ArmResult:
    arm: str
    hit: float
    n: int
    per_point: list[float]


@dataclass
class AbcVerdict:
    hypothesis_id: str
    arms: dict[str, ArmResult]
    increment_ci: tuple[float, float, float] | None   # C − A 的 (point, lo, hi)
    verdict: str                                      # SUPPORTED / FALSIFIED / INCONCLUSIVE
    reason: str


def run_abc(points: list[dict], labels: list[Decimal | None], h: Hypothesis) -> AbcVerdict:
    """points: [{"binance": {...}, "polymarket": {...}}]；labels: 与 points 对齐。"""
    arms: dict[str, ArmResult] = {}
    per_point_by_arm: dict[str, list[float]] = {}
    for arm in h.arms:
        sigs = [arm_signal(arm, p, h) for p in points]
        hit, n = hit_rate(sigs, labels)
        per_point = []
        for s, l in zip(sigs, labels):
            if s is None or l is None or s == 0:
                continue
            per_point.append(1.0 if (s > 0) == (l > 0) else 0.0)
        arms[arm] = ArmResult(arm=arm, hit=hit, n=n, per_point=per_point)
        per_point_by_arm[arm] = per_point

    if "A" not in arms or "C" not in arms or not per_point_by_arm.get("A") or not per_point_by_arm.get("C"):
        return AbcVerdict(h.hypothesis_id, arms, None, "INCONCLUSIVE", "A/C 臂样本不足")
    # 逐点相减需要等长——取较短并对齐到公共决策点集合（此处简化：按 index 截断）
    m = min(len(per_point_by_arm["A"]), len(per_point_by_arm["C"]))
    ci = block_bootstrap_ci(per_point_by_arm["C"][:m], per_point_by_arm["A"][:m])
    if ci[1] > 0:
        verdict, reason = "SUPPORTED", f"C−A 增量 {ci[0]:.3f}，CI=({ci[1]:.3f},{ci[2]:.3f}) 不含 0"
    elif ci[2] < 0:
        verdict, reason = "FALSIFIED", f"C−A 增量 {ci[0]:.3f}，CI=({ci[1]:.3f},{ci[2]:.3f}) 显著为负"
    else:
        verdict, reason = "INCONCLUSIVE", f"C−A 增量 {ci[0]:.3f}，CI=({ci[1]:.3f},{ci[2]:.3f}) 含 0"
    return AbcVerdict(h.hypothesis_id, arms, ci, verdict, reason)


# ---------- 证伪适配器（规格 §14：Cross-Market → 既有验证体系） ----------

class FalsificationAdapter:
    """把 A/B/C 结果翻译成既有 verify 体系能吃的形式。"""

    @staticmethod
    def to_evidence(verdict: AbcVerdict) -> dict:
        return {
            "kind": "cross_market_abc",
            "hypothesis": verdict.hypothesis_id,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "arms": {k: {"hit": v.hit, "n": v.n} for k, v in verdict.arms.items()},
            "increment_ci": verdict.increment_ci,
        }
