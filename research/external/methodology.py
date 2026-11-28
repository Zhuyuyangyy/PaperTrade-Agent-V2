"""方法论晋级状态机——移植自 Minara Learning System 的公开设计。

**为什么值得移植**：我们 Phase 6 原计划里的
`DRAFT → IMPLEMENTED → BACKTESTED → VALIDATED → PAPER_TRADING`
是一条**单向**线性流程，没有隔离区、没有降级、没有「用多少次才算数」的
统计门槛。结果是：任何跑出正期望的卡都能一路晋级，而我们已经知道
「跑出正期望」在 132 配置里是纯随机也会发生 6.6 次的事。

Minara 的公开文档给出了这套机制的具体参数，而且理由说得比我们清楚：

    「Wilson lower-bound beats raw times_correct / times_used because it
      doesn't let a 1-in-1 lucky hit outweigh a 15-in-30 consistent winner.」

这句话直接对应我们的处境：S20 在三币上 944 笔 +0.53%，和某张只跑了
11 笔就 +3% 的卡，用原始胜率看后者更漂亮。

**三条核心机制（全部来自公开文档，非逆向）**：

1. **隔离区（quarantine）**：新方法论默认 `confidence=0.1, quarantine=1`，
   不进入可用集合。必须攒够证据才毕业。
2. **Wilson 下界**：置信度不是点估计，是二项分布的下界。小样本被惩罚。
3. **双门槛毕业**：`confidence >= 阈值` **且** `times_used >= 最小次数`，
   两个条件同时满足才翻转 quarantine。

**我们做的三处改动（不是照抄）**：

- **不用 0.55**。文档里 0.55 出现在 Institution Mode 单资产场景，
  我们这里用的是 6 年跨三币的通用策略，先验应该更严。
  参数化并显式标注 `MIN_USES` 的来源与理由。
- **新增 `DATA_BLOCKED` 与 `REJECTED` 不可毕业**：数据缺失或已被证伪的
  方法论，攒多少次成功都不能晋级。Minara 没有这个概念，因为它的方法论
  都是自己产出的、数据自足。
- **新增 `demote_reason`**：降级必须留原因，否则半年后没人知道为什么。

**刻意不实现的部分**：文档描述的「合成（synthesis）」与「每 6h 反思节奏」
属于多角色 Institution Mode 的产物，我们没有多角色，硬做只会得到一个
空壳。这一层留空并标注，而不是假装实现。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# 状态与阈值
# ---------------------------------------------------------------------------

class State(str, Enum):
    DRAFT = "DRAFT"                  # 只有文字描述
    IMPLEMENTED = "IMPLEMENTED"      # 有可执行代码
    BACKTESTED = "BACKTESTED"        # 跑过回测，但未验证
    QUARANTINE = "QUARANTINE"        # 有证据但不够，隔离观察
    VALIDATED = "VALIDATED"          # 毕业，可进入下游
    PAPER = "PAPER"                  # 模拟盘
    DEMOTED = "DEMOTED"              # 曾经毕业，被降回
    REJECTED = "REJECTED"            # 已证伪
    DATA_BLOCKED = "DATA_BLOCKED"    # 缺数据，冻结


# 不可从这些状态向上晋级
TERMINAL = {State.REJECTED, State.DATA_BLOCKED}

# 毕业门槛。**这两个数必须有理由，不能拍脑袋。**
#
# MIN_USES = 30：来自 Wilson 下界的基本性质。n=30 时即使 100% 成功，
#   95% 下界也只有约 0.886；n<10 时下界掉到 0.7 以下，一段运气就能过。
#   我们的 30 卡里样本量的中位数是 1000+ 笔，但那是**交易笔数**；
#   方法论层的 `times_used` 是「独立验证次数」（跨币 × 跨年），
#   3 币 × 7 年 = 21 次独立机会，30 是略高于此的保守值。
MIN_USES = 30

# CONF_THRESHOLD = 0.62：比 Minara 文档的 0.55 严。
#   0.55 是他们单资产 Institution Mode 的口径；我们是 6 年跨三币通用策略，
#   先验上应该要求更多证据。0.62 对应「n=30 时约 70% 成功率」的下界。
CONF_THRESHOLD = 0.62

# 降级触发：连续失败多少次退回隔离
DEMOTE_AFTER_FAILURES = 5


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    """二项分布的 Wilson 下界置信区间。

    为什么不用 `successes / n`：n=1 成功一次得 1.0，n=30 成功 15 次得 0.5，
    点估计会把「一次运气」排在「稳定半胜」前面。Wilson 下界对前者给
    0.207，对后者给 0.333——顺序被纠正了。

    z=1.96 是 95% 双侧。n=0 时返回 0（无证据即无置信度，
    不是 0.5——那会人为给一个新方法论「一半的可信度」）。
    """
    if n <= 0:
        return 0.0
    if successes < 0 or successes > n:
        raise ValueError(f"successes={successes} 超出 [0, {n}]")
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


# ---------------------------------------------------------------------------
# 方法论条目
# ---------------------------------------------------------------------------

@dataclass
class Methodology:
    """一条「可复用的交易方法」。

    **与策略卡的区别**：卡是「一个形态怎么识别」，方法论是「什么条件下
    这个形态值得交易」。前者是代码，后者是被证据管理的知识。
    一张卡可以挂多条方法论（不同市场状态下的不同用法）。
    """
    id: str
    name: str
    card_id: str                     # 关联的策略卡
    asset_class: str                 # major_crypto | layer_1 | ...
    direction: str                   # bullish | bearish | neutral
    primary_signal: str              # momentum | mean_reversion | technical | ...
    timeframe: str                   # intraday | short | medium | long
    indicators: tuple[str, ...] = ()

    state: State = State.DRAFT
    times_used: int = 0
    times_correct: int = 0
    confidence: float = 0.1          # 与 Minara 一致的初始值
    quarantine: bool = True

    # 连续失败计数。**必须单独记，不能用 times_used - times_correct。**
    # 后者是累计失败数，在长历史上几乎不会触发降级：一条毕业后
    # 30 次全对的记录，再来 5 次连续失败，累计失败才 5/35，
    # Wilson 下界从 0.886 只掉到 0.706，仍在门槛之上。
    # 于是「降级」这个机制看起来实现了，实际上永远不生效——
    # 这是最糟的一类 bug：有代码、有测试、没有行为。
    consec_failures: int = 0

    # 当前隔离期起始的 times_used。毕业门槛要求「本期新增证据」够数，
    # 不是「历史累计」够数。
    #
    # 不加这个会有一个尴尬的 bug：降级时 times_used 已经 >= 30、
    # confidence 也还在 0.62 之上，于是下一次 try_graduate() 立刻
    # 重新毕业，降级等于没做。降级必须**重新挣证据**。
    evidence_floor: int = 0

    demote_reason: str = ""
    lifecycle: list[tuple[str, str, str]] = field(default_factory=list)
    # (from_state, to_state, reason)

    # -- 去重 ------------------------------------------------------------

    def dedup_key(self) -> str:
        """结构化去重键。

        抄的是 Minara 的定义：hash(direction + primary_signal + timeframe
        + 排序后 indicators + asset_class)。**自由文本不参与去重**，
        只用于人读与 prompt 注入。

        为什么必须结构化：`"BTC 突破入场"` 和 `"突破后买入 BTC"` 语义相同，
        字符串相似度算不准；但 `(bullish, technical, short, (atr,), major_crypto)`
        一致就是同一条。
        """
        ind = "+".join(sorted(i.lower() for i in self.indicators))
        return "|".join([self.direction, self.primary_signal, self.timeframe,
                         ind, self.asset_class])

    # -- 状态迁移 --------------------------------------------------------

    def _log(self, frm: State, to: State, reason: str):
        self.lifecycle.append((frm.value, to.value, reason))

    def to_implemented(self, note: str = "") -> None:
        if self.state != State.DRAFT:
            raise ValueError(f"{self.id}: 只有 DRAFT 能进 IMPLEMENTED，"
                             f"当前 {self.state.value}")
        self._log(self.state, State.IMPLEMENTED, note or "代码就绪")
        self.state = State.IMPLEMENTED

    def to_backtested(self, n_trades: int, note: str = "") -> None:
        if self.state not in (State.IMPLEMENTED, State.DEMOTED):
            raise ValueError(f"{self.id}: 不能从 {self.state.value} 进 BACKTESTED")
        if n_trades <= 0:
            # 跑出零成交不是「回测完成」，是「没有证据」。不能拿来晋级。
            raise ValueError(f"{self.id}: 零成交不能标 BACKTESTED，"
                             f"应标 DATA_BLOCKED 或 NO_TRADES")
        self._log(self.state, State.BACKTESTED, note or f"{n_trades} 笔")
        self.state = State.BACKTESTED

    def record_outcome(self, correct: bool, note: str = "") -> None:
        """记录一次独立验证的结果。

        **「独立」是关键**：同一币同一年的 100 笔交易只算 1 次，
        因为它们是同一个市场环境下的相关观测。跨币跨年才算独立。
        这条与 15m 那轮的结论一致——笔数不等于样本量。
        """
        if self.state in TERMINAL:
            raise ValueError(f"{self.id}: {self.state.value} 状态不接受新证据")
        self.times_used += 1
        if correct:
            self.times_correct += 1
            self.consec_failures = 0
        else:
            self.consec_failures += 1
        self.confidence = wilson_lower(self.times_correct, self.times_used)

    def try_graduate(self) -> tuple[bool, str]:
        """双门槛毕业判定。

        **检查顺序有讲究**：先查 `times_used`，再查 `confidence`。

        反过来写（先查置信度）会让所有小样本的拒绝理由变成
        「置信度 0.566 < 0.62」，而不是「只验证了 5 次」。
        两者都拒绝，但**告诉人的信息完全不同**：前者暗示「再多成功几次
        就行了」，后者才是实情——「你根本没验证够」。一个只看
        failure message 的人会被前者骗着去刷成功率。
        """
        if self.state in TERMINAL:
            return False, f"{self.state.value} 不可毕业"
        if not self.quarantine and self.state == State.VALIDATED:
            return True, "已在 VALIDATED"
        since = self.times_used - self.evidence_floor
        if since < MIN_USES:
            return False, (f"本期新增独立验证 {since} 次 < {MIN_USES}，"
                           f"再高的胜率也不够（Wilson 下界 "
                           f"{self.confidence:.3f}，本期 n={since}）")
        if self.confidence < CONF_THRESHOLD:
            return False, (f"置信度 {self.confidence:.3f} < "
                           f"{CONF_THRESHOLD}（Wilson 下界，n={self.times_used}）")
        self.quarantine = False
        self.consec_failures = 0
        self._log(self.state, State.VALIDATED,
                  f"毕业：conf={self.confidence:.3f}, n={self.times_used}")
        self.state = State.VALIDATED
        return True, "毕业"

    def check_demote(self) -> tuple[bool, str]:
        """毕业后连续失败过多则降级。

        Minara 的 `graduation_fp_rate` 维度就是在量这件事：
        「毕业后 30 天内是否反转」。我们简化为**连续**失败计数。

        **为什么必须是「连续」而不是「累计」**：累计失败在长历史上
        几乎永不触发（30 次全对后连续 5 次失败，累计才 5/35，
        下界仍有 0.706 > 0.62）。那样写出来的降级机制是装饰品。
        连续失败才对应「这个方法最近不灵了」这个真实信号。

        另外：**降级不再看 confidence**。降级是「近期行为」的判断，
        置信度是「长期历史」的累积，用后者去 gate 前者等于让历史
        一致性掩盖近期失效——而这正是我们要抓的东西。
        """
        if self.state != State.VALIDATED:
            return False, ""
        if self.consec_failures < DEMOTE_AFTER_FAILURES:
            return False, ""
        self.demote_reason = (f"连续失败 {self.consec_failures} 次"
                              f"（阈值 {DEMOTE_AFTER_FAILURES}），"
                              f"长期置信度仍有 {self.confidence:.3f}——"
                              f"正是历史掩盖失效的典型情形")
        self._log(self.state, State.DEMOTED, self.demote_reason)
        self.state = State.DEMOTED
        self.quarantine = True
        self.consec_failures = 0
        self.evidence_floor = self.times_used     # 重新挣证据，不能吃老本
        return True, self.demote_reason

    def mark_rejected(self, reason: str) -> None:
        self._log(self.state, State.REJECTED, reason)
        self.state = State.REJECTED
        self.quarantine = True

    def mark_data_blocked(self, reason: str) -> None:
        self._log(self.state, State.DATA_BLOCKED, reason)
        self.state = State.DATA_BLOCKED
        self.quarantine = True

    # -- 输出 ------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "card_id": self.card_id,
            "asset_class": self.asset_class, "direction": self.direction,
            "primary_signal": self.primary_signal, "timeframe": self.timeframe,
            "indicators": list(self.indicators),
            "dedup_key": self.dedup_key(),
            "state": self.state.value,
            "times_used": self.times_used,
            "times_correct": self.times_correct,
            "consec_failures": self.consec_failures,
            "evidence_floor": self.evidence_floor,
            "confidence": round(self.confidence, 4),
            "quarantine": self.quarantine,
            "demote_reason": self.demote_reason,
            "lifecycle": self.lifecycle,
        }


# ---------------------------------------------------------------------------
# 库：去重 + 查询
# ---------------------------------------------------------------------------

class MethodologyStore:
    """方法论库。**只暴露已毕业的条目给下游。**

    这一层是「隔离区」的执行者：`injectable()` 是唯一对外出口，
    任何没毕业的方法论都拿不到。
    """

    def __init__(self):
        self._items: dict[str, Methodology] = {}
        self._by_key: dict[str, str] = {}     # dedup_key → id

    def add(self, m: Methodology) -> tuple[bool, str]:
        """加入库。重复键不新建，返回既有条目供更新计数器。

        与 Minara 一致：近重复（同 dedup_key）应更新既有行，
        而不是插一条新的——否则同一个方法被写 10 次就有 10 份证据。
        """
        k = m.dedup_key()
        if k in self._by_key:
            existing = self._items[self._by_key[k]]
            return False, f"重复：与 {existing.id} 同键 {k}"
        self._items[m.id] = m
        self._by_key[k] = m.id
        return True, "已加入"

    def get(self, mid: str) -> Methodology | None:
        return self._items.get(mid)

    def injectable(self) -> list[Methodology]:
        """可注入下游的方法论。**这是唯一的对外出口。**"""
        return [m for m in self._items.values()
                if m.state == State.VALIDATED and not m.quarantine]

    def by_state(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for m in self._items.values():
            out.setdefault(m.state.value, []).append(m.id)
        return out

    def summary(self) -> dict:
        n = len(self._items)
        grad = len(self.injectable())
        return {
            "n_methodologies": n,
            "n_injectable": grad,
            "graduation_rate": round(grad / n, 4) if n else 0.0,
            "by_state": {k: len(v) for k, v in self.by_state().items()},
            "min_uses": MIN_USES,
            "conf_threshold": CONF_THRESHOLD,
        }


if __name__ == "__main__":
    print("Wilson 下界：为什么不能看点估计")
    print(f"{'n':>5}{'成功':>6}{'点估计':>10}{'Wilson下界':>12}")
    print("-" * 34)
    for n, k in [(1, 1), (5, 5), (10, 8), (30, 15), (30, 21),
                 (100, 55), (944, 494)]:
        p = k / n
        print(f"{n:>5}{k:>6}{p:>10.3f}{wilson_lower(k, n):>12.3f}")
    print()
    print("注意第一行：1 次全中点估计 1.000，Wilson 下界只有 "
          f"{wilson_lower(1, 1):.3f}。")
    print(f"而 30 次中 15 次（点估计 0.500）下界是 "
          f"{wilson_lower(15, 30):.3f}——顺序被纠正。")
    print()
    print(f"毕业门槛：confidence >= {CONF_THRESHOLD} 且 n >= {MIN_USES}")