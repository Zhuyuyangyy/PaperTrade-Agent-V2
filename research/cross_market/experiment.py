"""A/B/C 实验骨架（P1/P1.1，规格 §9-§12）。

核心问题：Performance(C) − Performance(A) > 0？（Polymarket 的增量价值）
不是 C 自己能否赚钱。

Sprint 0 修掉的两处（审计 S0-03/S0-04/S0-05）：
  1. **方向信号必须用变化量**。旧实现 A 臂是 `price > 0 and oi > 0 → +1`——
     价格是正数，恒为做多；B 臂是 `mid > 0.5 → +1`——概率水平不是方向，
     而且 BELOW 类市场（"BTC 跌破 60k"）的概率上升对应标的看跌，方向反了。
  2. **A/C 比较必须按共同决策时点对齐**。旧实现把两个臂的命中序列各自压缩成
     列表，再按 index 截断相减；两臂出手时点不同，比较的是不同时刻的收益。
     现在所有臂在同一个决策点向量上计算 payoff（弃权 = 0），配对 bootstrap；
     另报"共同点命中率差"作为诊断，并明示它在 C⊆A 的构造下恒为 0。

预注册契约：hypothesis 在跑数据前锁定（primary horizon、特征集、模型类、
事件比较符），禁跑完再挑显著的那个（多重检验钓鱼）。

统计链：配对 payoff 差 → Block Bootstrap CI → 样本量/覆盖率门槛 → 裁定。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from decimal import Decimal

BPS = Decimal("10000")


# ---------- 预注册假设 ----------

@dataclass
class Hypothesis:
    hypothesis_id: str
    market_key: str
    underlying: str
    primary_horizon_ms: int
    arms: tuple[str, ...] = ("A", "B", "C")   # A=Binance only, B=Poly only, C=both
    features: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "A": ("price_delta_bps", "oi_delta_bps", "cvd_delta", "funding_rate"),
        "B": ("yes_mid", "yes_mid_delta_bps", "spread_bps"),
        "C": ("price_delta_bps", "oi_delta_bps", "cvd_delta", "funding_rate",
              "yes_mid", "yes_mid_delta_bps", "spread_bps"),
    })
    preregistered: bool = True   # False = 未预注册（结果只配进废纸篓）
    # 事件方向：Polymarket "BTC above X" = ABOVE，"BTC dips to X" = BELOW。
    # B 臂必须据此把"概率变化"翻译成"标的方向"，否则方向系统性反了。
    event_comparator: str = "ABOVE"
    deadband_bps: Decimal = Decimal("2")   # 小于该变化视为无信息（弃权）
    min_aligned: int = 30                  # 共同决策点下限（不足 → INCONCLUSIVE）


# ---------- 信号与评分 ----------

def _d(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except ArithmeticError:
        return None


def _sign(v: Decimal | None, deadband: Decimal) -> Decimal | None:
    if v is None or abs(v) < deadband:
        return None
    return Decimal("1") if v > 0 else Decimal("-1")


def _dir_sign(v: Decimal | None, deadband: Decimal, comparator: str) -> Decimal | None:
    s = _sign(v, deadband)
    if s is None:
        return None
    if comparator.upper() == "BELOW":
        return -s
    return s


def arm_signal(arm: str, feature: dict, hypothesis: Hypothesis) -> Decimal | None:
    """把特征压缩成一个方向性信号（+1 看涨 / -1 看跌 / None 弃权 / 0 冲突）。

    A：Binance 价格动量方向（|price_delta_bps| >= deadband）。
    B：Polymarket YES 概率动量，按市场比较符映射到标的方向
       （ABOVE：概率涨=看涨；BELOW：概率涨=看跌）。
    C：A 与 B 同向才出手，否则弃权（0 = 两侧冲突）。
    返回 None 表示该时刻无信号（abstain）。原始值容许字符串（Decimal 化后比较）。
    """
    if arm == "A":
        return _sign(_d(feature.get("binance", {}).get("price_delta_bps")),
                     hypothesis.deadband_bps)
    if arm == "B":
        poly = feature.get("polymarket", {})
        if poly.get("prob_usable") is False:
            return None   # 两 token 一价关系破 → 这个"概率"不可用
        return _dir_sign(_d(poly.get("yes_mid_delta_bps")),
                         hypothesis.deadband_bps, hypothesis.event_comparator)
    if arm == "C":
        a = arm_signal("A", feature, hypothesis)
        b = arm_signal("B", feature, hypothesis)
        if a is None or b is None:
            return None
        return a if a == b else Decimal("0")
    return None


def hit_rate(signals: list[Decimal | None], labels: list[Decimal | None]) -> tuple[float, int]:
    """方向准确率（只在双方都有的时刻计算）。labels 为**未来收益**（>0 涨/<0 跌）。"""
    n = 0
    hits = 0
    for s, l in zip(signals, labels):
        if s is None or l is None or s == 0:
            continue
        n += 1
        if (s > 0 and l > 0) or (s < 0 and l < 0):
            hits += 1
    return (hits / n if n else 0.0), n


def payoff_bps(signal: Decimal | None, label: Decimal | None) -> float:
    """逐决策点收益（bps）：方向 × 未来收益；弃权/无标签 = 0。

    这是 A/C 比较的正确口径：同一决策点上，C 弃权时收益为 0，A 仍按自己的
    方向持仓——两者的差就是"B 的信息让 C 躲过了多少亏损/错过了多少盈利"。
    """
    if signal is None or signal == 0 or label is None:
        return 0.0
    return float(signal) * float(label) * float(BPS)


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
    coverage: float = 0.0
    mean_payoff_bps: float = 0.0


@dataclass
class AbcVerdict:
    hypothesis_id: str
    arms: dict[str, ArmResult]
    increment_ci: tuple[float, float, float] | None   # 全样本逐点 payoff 差 C−A
    verdict: str                                      # SUPPORTED / FALSIFIED / INCONCLUSIVE
    reason: str
    aligned_n: int = 0                                # 所有臂都出手的共同决策点数
    common_hit_ci: tuple[float, float, float] | None = None  # 共同点命中率差（诊断）
    n_decision_points: int = 0
    step_ms: int = 0                                  # 采样间隔（从 decision_ts 推断）
    block: int = 1                                    # 覆盖一个预测视界所需的采样点数
    effective_aligned_n: int = 0                      # 折算成独立窗口的共同点数
    non_overlap_increment_ci: tuple[float, float, float] | None = None
    non_overlap_aligned_n: int = 0


def _infer_step_ms(points: list[dict], h: Hypothesis) -> int:
    """从决策时刻推断采样间隔；推断不出来就退回视界（等价于 k=1）。"""
    ts = sorted({p["decision_ts_ms"] for p in points
                 if isinstance(p.get("decision_ts_ms"), int)})
    if len(ts) >= 2:
        diffs = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
        if diffs:
            return max(1, int(diffs[len(diffs) // 2]))
    return max(1, int(h.primary_horizon_ms))


def run_abc(points: list[dict], labels: list[Decimal | None], h: Hypothesis,
            block: int | None = None, iters: int = 1000) -> AbcVerdict:
    """points: [{"decision_ts_ms": int, "binance": {...}, "polymarket": {...}}]；
    labels: 与 points **逐点对齐**的未来收益（同一 length、同一 index 同一时刻）。

    重叠采样（R2-05）：若每 5 秒采样、视界 5 分钟，相邻 60 个点的标签高度重叠。
    因此：
      - block 默认 = ceil(视界 / 采样间隔)，bootstrap 按这个块长重采样；
      - 有效样本量按 aligned_n / block 折算，门槛用**有效**共同点数；
      - 另报非重叠子样本（每 block 个点取一个）的增量 CI，作为稳健性检验：
        全样本显著、非重叠样本不显著 → 降级 INCONCLUSIVE。
    """
    if len(points) != len(labels):
        raise ValueError(f"points({len(points)}) 与 labels({len(labels)}) 必须逐点对齐")
    n_points = len(points)
    step_ms = _infer_step_ms(points, h)
    k = int(block) if block is not None else max(1, math.ceil(h.primary_horizon_ms / step_ms))

    sig: dict[str, list[Decimal | None]] = {
        arm: [arm_signal(arm, p, h) for p in points] for arm in h.arms
    }

    arms: dict[str, ArmResult] = {}
    for arm in h.arms:
        hit, n = hit_rate(sig[arm], labels)
        payoff = [payoff_bps(s, l) for s, l in zip(sig[arm], labels)]
        labelled = [i for i, l in enumerate(labels) if l is not None]
        coverage = (n / len(labelled)) if labelled else 0.0
        arms[arm] = ArmResult(
            arm=arm, hit=hit, n=n,
            per_point=[1.0 if (s > 0) == (l > 0) else 0.0
                       for s, l in zip(sig[arm], labels)
                       if s is not None and l is not None and s != 0],
            coverage=coverage,
            mean_payoff_bps=sum(payoff) / n_points if n_points else 0.0,
        )

    # 共同决策点：所有臂都真正出手（方向非 0）且标签可用
    common = [
        i for i in range(n_points)
        if labels[i] is not None
        and all(sig[a][i] is not None and sig[a][i] != 0 for a in h.arms)
    ]

    if "A" not in arms or "C" not in arms or not common:
        return AbcVerdict(
            h.hypothesis_id, arms, None, "INCONCLUSIVE",
            f"A/C 无共同决策点（A 出手 {arms.get('A').n if 'A' in arms else 0} / "
            f"C 出手 {arms.get('C').n if 'C' in arms else 0}）",
            aligned_n=len(common), n_decision_points=n_points,
            step_ms=step_ms, block=k, effective_aligned_n=len(common) // k,
        )

    # 1) 主指标：全样本逐点 payoff 差（同长、同点、配对，块长 = 视界/采样）
    c_pay = [payoff_bps(sig["C"][i], labels[i]) for i in range(n_points)]
    a_pay = [payoff_bps(sig["A"][i], labels[i]) for i in range(n_points)]
    inc_ci = block_bootstrap_ci(c_pay, a_pay, block=k, iters=iters)

    # 2) 非重叠子样本（每 k 个点取一个）：独立窗口上的稳健性检验
    non_idx = list(range(0, n_points, k))
    non_ci = block_bootstrap_ci(
        [c_pay[i] for i in non_idx], [a_pay[i] for i in non_idx], block=1, iters=iters,
    )
    non_common = sum(1 for i in common if i % k == 0)

    # 3) 诊断指标：共同点命中率差（C⊆A 的构造下数学上恒为 0；显式报出来防误读）
    c_hit_common = [1.0 if (sig["C"][i] > 0) == (labels[i] > 0) else 0.0 for i in common]
    a_hit_common = [1.0 if (sig["A"][i] > 0) == (labels[i] > 0) else 0.0 for i in common]
    common_ci = block_bootstrap_ci(c_hit_common, a_hit_common, block=k, iters=iters)

    aligned_n = len(common)
    effective = aligned_n // k
    required = h.min_aligned * k
    if aligned_n < required:
        verdict = "INCONCLUSIVE"
        reason = (f"有效独立共同点 {effective}（{aligned_n} 个重叠点 / 块长 {k}）"
                  f"< 门槛 {h.min_aligned}；payoff 差 {inc_ci[0]:.1f}bps "
                  f"CI=({inc_ci[1]:.1f},{inc_ci[2]:.1f})")
    elif inc_ci[1] > 0:
        if non_common >= h.min_aligned and non_ci[1] <= 0:
            verdict = "INCONCLUSIVE"
            reason = (f"全样本 payoff 增量 {inc_ci[0]:.1f}bps 显著，但非重叠子样本"
                      f"（n={non_common}）CI=({non_ci[1]:.1f},{non_ci[2]:.1f}) 含 0，"
                      f"显著性不复现")
        else:
            verdict = "SUPPORTED"
            reason = (f"C−A payoff 增量 {inc_ci[0]:.1f}bps，CI=({inc_ci[1]:.1f},{inc_ci[2]:.1f})"
                      f" 不含 0；有效独立共同点 {effective}；非重叠 CI=({non_ci[1]:.1f},{non_ci[2]:.1f})")
    elif inc_ci[2] < 0:
        if non_common >= h.min_aligned and non_ci[2] >= 0:
            verdict = "INCONCLUSIVE"
            reason = (f"全样本 payoff 增量 {inc_ci[0]:.1f}bps 显著为负，但非重叠子样本"
                      f"（n={non_common}）CI=({non_ci[1]:.1f},{non_ci[2]:.1f}) 含 0")
        else:
            verdict = "FALSIFIED"
            reason = (f"C−A payoff 增量 {inc_ci[0]:.1f}bps，CI=({inc_ci[1]:.1f},{inc_ci[2]:.1f})"
                      f" 显著为负；有效独立共同点 {effective}")
    else:
        verdict = "INCONCLUSIVE"
        reason = (f"C−A payoff 增量 {inc_ci[0]:.1f}bps，CI=({inc_ci[1]:.1f},{inc_ci[2]:.1f}) 含 0；"
                  f"共同决策点 {aligned_n}（块长 {k}，有效独立 {effective}）；"
                  f"共同点命中率差恒为 0 是 C⊆A 的定义后果，不作为证据")
    return AbcVerdict(
        h.hypothesis_id, arms, inc_ci, verdict, reason,
        aligned_n=aligned_n, common_hit_ci=common_ci, n_decision_points=n_points,
        step_ms=step_ms, block=k, effective_aligned_n=effective,
        non_overlap_increment_ci=non_ci, non_overlap_aligned_n=non_common,
    )


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
            "n_decision_points": verdict.n_decision_points,
            "aligned_n": verdict.aligned_n,
            "step_ms": verdict.step_ms,
            "block": verdict.block,
            "effective_aligned_n": verdict.effective_aligned_n,
            "non_overlap_increment_ci": verdict.non_overlap_increment_ci,
            "non_overlap_aligned_n": verdict.non_overlap_aligned_n,
            "arms": {
                k: {"hit": v.hit, "n": v.n, "coverage": v.coverage,
                    "mean_payoff_bps": v.mean_payoff_bps}
                for k, v in verdict.arms.items()
            },
            "increment_ci": verdict.increment_ci,
            "common_hit_ci": verdict.common_hit_ci,
        }