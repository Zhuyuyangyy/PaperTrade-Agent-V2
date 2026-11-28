"""可复用角色卡库——把「多角色研究」从提示词工程变成可检查的结构。

**为什么需要这一层，以及为什么它不是「抄提示词」。**

用户任务书列了要重点研究的东西：角色提示词、SKILL.md、方法论、
市场结构识别、策略路由、信号确认、止盈止损、风险过滤……
其中「角色提示词」是最容易被误解成「去把别人的 prompt 抄过来」的一项。

我们不这么做，三个理由：

1. **许可证**。TradingAgents 是 Apache-2.0（可复制但需保留声明），
   ai-hedge-fund 是 MIT。理论上可以抄。但——
2. **我们没读过原文**。本轮核实的是仓库存在性、许可证、架构描述、
   角色划分，**没有逐字读过任何一份提示词**。没读过就写「移植自
   TradingAgents 的 XX 提示词」是编造。
3. **抄提示词是错的抽象层**。角色真正的价值不在措辞，在
   **输入契约、输出契约、以及它被允许做什么/不被允许做什么**。
   措辞可以随时重写；契约错了整个系统就是在自欺。

所以本模块定义的是**角色的结构**：每个角色有哪些输入槽、必须输出
什么结构、有哪些硬禁止。提示词是这些结构的自然语言投影，
而不是反过来。

**每个角色带 `basis` 字段，说明它从哪来、可信度如何。**
这比「参考了业界最佳实践」这种话有用得多——后者等于没说话。

**与 Minara 的对照**：Minara 的 Institution Mode 是多角色（含 synthesis
与每 6h 反思）。我们刻意不做 synthesis：没有真实多角色并发时，
synthesis 只会得到一个把所有观点平均掉的空壳。这一点在
`methodology.py` 里已经标注为「刻意不实现」，这里保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Basis(Enum):
    """角色设计的来源可信度。**这个字段是防自欺的核心。**"""
    READ_PRIMARY = "READ_PRIMARY"        # 我读过该来源的原文（本轮：无）
    READ_ARCH_DESC = "READ_ARCH_DESC"    # 只读到架构/角色划分的描述，未读提示词
    PROJECT_HISTORY = "PROJECT_HISTORY"  # 来自本项目自己的踩坑记录（最可信）
    GENERAL_PRACTICE = "GENERAL_PRACTICE"  # 通行做法，无特定来源


class Slot(Enum):
    """角色可消费的输入槽。

    **输入槽是硬白名单**，不是建议。角色只能看到它声明过的槽——
    这条约束的价值在于：一个「风控角色」如果拿不到未来数据，
    它就不可能用未来数据做判断。这比在提示词里写「请不要使用
    未来数据」可靠得多。
    """
    OHLCV = "ohlcv"
    STRUCTURE = "structure"          # pivot / BOS / FVG 等结构层
    INDICATORS = "indicators"
    POSITION = "position"            # 当前持仓与风险敞口
    COSTS = "costs"                  # 费率、滑点模型
    BACKTEST_RUN = "backtest_run"    # 一次回测的完整记录
    MARKET_REGIME = "market_regime"
    NEWS = "news"                    # 本轮无可靠源，标 DATA_BLOCKED
    EXTERNAL_METHOD = "external_method"  # 外部策略资源库条目


class Verdict(Enum):
    """角色输出必须是闭集。自由文本结论无法被下游消费。"""
    PASS = "PASS"
    FAIL = "FAIL"
    ABSTAIN = "ABSTAIN"      # 我判断不了（**必须允许，否则角色会硬编一个答案**）
    BLOCKED = "BLOCKED"      # 缺输入，无法判断


@dataclass(frozen=True)
class RoleCard:
    """一个研究角色的完整契约。"""

    id: str
    name: str
    question: str
    """这个角色**唯一**要回答的问题。一句话。多问一句就会失焦。"""

    slots_in: tuple[Slot, ...]
    verdict_options: tuple[Verdict, ...]
    must_quote: tuple[str, ...] = ()
    """输出里必须带出处证据的字段。空表示不需要。"""

    hard_bans: tuple[str, ...] = ()
    """硬禁止。**这些不是提示词里的礼貌请求，是代码层的检查项。**"""

    basis: Basis = Basis.GENERAL_PRACTICE
    basis_note: str = ""
    anti_pattern: str = ""
    """这个角色最容易退化成的样子。写出来是为了能检查它有没有退化。"""

    def check_output(self, verdict: Verdict, payload: dict) -> tuple[bool, str]:
        """输出契约校验。

        **为什么要有这一步**：角色输出如果只靠 LLM 自觉，
        迟早会出现「结论是 FAIL 但证据字段为空」或者
        「PASS 但一个数字都没引用」。那不是研究，是表态。
        """
        if verdict not in self.verdict_options:
            return False, f"{self.id} 不接受裁决 {verdict.value}"

        if verdict in (Verdict.PASS, Verdict.FAIL):
            missing = [k for k in self.must_quote if not payload.get(k)]
            if missing:
                return False, (f"{self.id} 给出 {verdict.value} 但缺少证据字段 "
                               f"{missing}——没有证据的裁决不算裁决")

        if verdict == Verdict.ABSTAIN and not payload.get("reason"):
            return False, f"{self.id} ABSTAIN 必须写明 reason"

        if verdict == Verdict.BLOCKED and not payload.get("missing_slot"):
            return False, f"{self.id} BLOCKED 必须写明缺哪个槽"
        return True, "ok"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "question": self.question,
            "slots_in": [s.value for s in self.slots_in],
            "verdict_options": [v.value for v in self.verdict_options],
            "must_quote": list(self.must_quote),
            "hard_bans": list(self.hard_bans),
            "basis": self.basis.value, "basis_note": self.basis_note,
            "anti_pattern": self.anti_pattern,
        }


# ---------------------------------------------------------------------------
# 角色卡
# ---------------------------------------------------------------------------

ROLES: list[RoleCard] = [

    RoleCard(
        id="R-COST",
        name="成本裁判",
        question="这笔交易在诚实的手续费与滑点下，毛优势还剩多少？",
        slots_in=(Slot.BACKTEST_RUN, Slot.COSTS),
        verdict_options=(Verdict.PASS, Verdict.FAIL, Verdict.ABSTAIN),
        must_quote=("gross_expectancy", "net_expectancy", "cost_per_trade",
                    "n_trades"),
        hard_bans=(
            "不得用零成本结果作为结论",
            "不得把『笔数多』当成『样本量大』——15m 那轮 93,766 笔仍未增加有效样本",
            "不得在成本/毛 > 50% 时给 PASS",
        ),
        basis=Basis.PROJECT_HISTORY,
        basis_note=(
            "来自本项目 30 卡回测的实测教训：S03 成本/毛从 4h 的 45% "
            "升到 15m 的 366%；S20 在 1h 上毛期望本身归零（−0.071），"
            "不是被摩擦吃掉的。**「成本解释了一半，另一半更重要」**"
            "这句话是这轮最重要的发现。"
        ),
        anti_pattern=(
            "退化成『成本占比不高，可以接受』——不给具体数字、"
            "不区分毛优势退化与摩擦放大。"
        ),
    ),

    RoleCard(
        id="R-REGIME",
        name="市场状态裁判",
        question="这个策略的结论是在哪种市场状态下得出的，换个状态还成立吗？",
        slots_in=(Slot.MARKET_REGIME, Slot.BACKTEST_RUN),
        verdict_options=(Verdict.PASS, Verdict.FAIL, Verdict.ABSTAIN,
                         Verdict.BLOCKED),
        must_quote=("regime_label", "n_trades_per_regime"),
        hard_bans=(
            "不得用全样本平均掩盖分状态反转",
            "不得在某一状态样本 < 30 笔时对该状态下结论",
            "不得把『趋势/震荡』当唯一维度——波动率与流动性同样要分",
        ),
        basis=Basis.PROJECT_HISTORY,
        basis_note=(
            "来自 regime_split 的实测：多数卡在趋势/震荡两态下的期望符号相反。"
            "regime 过滤本身能降抖动，但也让样本腰斩（min_hold=6 时 "
            "CI 跨 0）。**这是一个真实的权衡，不是免费的改进。**"
        ),
        anti_pattern=(
            "退化成给每张卡都写『在趋势市表现较好』——"
            "这句话对所有动量类策略都成立，等于没信息。"
        ),
    ),

    RoleCard(
        id="R-LOOKAHEAD",
        name="前视审查员",
        question="这段实现里，有没有任何一处用到了当时还不可能知道的信息？",
        slots_in=(Slot.STRUCTURE, Slot.BACKTEST_RUN),
        verdict_options=(Verdict.PASS, Verdict.FAIL, Verdict.ABSTAIN,
                         Verdict.BLOCKED),
        must_quote=("scan_result", "causality_test"),
        hard_bans=(
            "不得以『静态扫描没命中』作为 PASS 的唯一依据——"
            "**静态检测发现模式，不提供证明**",
            "不得以『注释里写了 FIXED』作为已修复的证据",
            "不得跳过截断因果性测试",
        ),
        basis=Basis.PROJECT_HISTORY,
        basis_note=(
            "本项目最贵的 bug 形态就是中心窗口 pivot。"
            "freqtradestrategies 的实测（137/1286 被标记，其中大量注释"
            "自称已修复）证明『自称修复』不可信。"
            "我们的判据是双重的：静态扫描 + 截断因果性测试。"
        ),
        anti_pattern=(
            "退化成『代码看起来没问题』——没有 scan_ref、没有 causality_test。"
        ),
    ),

    RoleCard(
        id="R-MULTIPLICITY",
        name="多重检验审查员",
        question="这个『显著』结果，在跑过的所有配置里，是第几个被挑出来的？",
        slots_in=(Slot.BACKTEST_RUN,),
        verdict_options=(Verdict.PASS, Verdict.FAIL, Verdict.ABSTAIN),
        must_quote=("n_configs_tried", "n_positive", "expected_by_chance"),
        hard_bans=(
            "不得只报告最好的那个配置",
            "不得在 n_configs_tried 未记录时给 PASS",
            "不得把 132 配置里 39 个正期望当成『32.5% 成功率』——"
            "那是随机也会出现的量级",
        ),
        basis=Basis.PROJECT_HISTORY,
        basis_note=(
            "来自 30 卡 × 132 配置的实测：4h 上 39 个正期望，"
            "纯随机也应有约 6.6 个。这个数字是本项目所有『显著』"
            "结论的背景噪音底。"
        ),
        anti_pattern=(
            "退化成『我们做了样本外验证』——没说验了多少个、"
            "挑之前看过多少个。"
        ),
    ),

    RoleCard(
        id="R-EXTERNAL",
        name="外部策略审查员",
        question="这条外部策略，我们是复现了它、独立实现了同族方法、还是只读了描述？",
        slots_in=(Slot.EXTERNAL_METHOD,),
        verdict_options=(Verdict.PASS, Verdict.FAIL, Verdict.ABSTAIN,
                         Verdict.BLOCKED),
        must_quote=("reproduction", "license"),
        hard_bans=(
            "不得把外部宣传收益写进复现结果——**这是用户明令禁止的**",
            "不得在许可证为 NO_LICENSE/UNKNOWN 时声称复制了对方代码",
            "不得把『独立实现同族方法』说成『复现』",
        ),
        basis=Basis.PROJECT_HISTORY,
        basis_note=(
            "来自本轮 entry.py 初版的真实错误：FVG 那条同时标了 "
            "RAN_MATCHED 和『我们没有复现对方』。两句话都是好话，"
            "拼在一起就是过度声称。为此新增了 INDEPENDENT_IMPL 枚举值。"
        ),
        anti_pattern=(
            "退化成『参考了业界开源实现』——不说是哪一条、"
            "什么许可证、复现到什么程度。"
        ),
    ),

    RoleCard(
        id="R-SURVIVOR",
        name="选择偏差审查员",
        question="这个结论用的样本，是不是已经排除了死掉的/退市的标的？",
        slots_in=(Slot.OHLCV, Slot.BACKTEST_RUN),
        verdict_options=(Verdict.PASS, Verdict.FAIL, Verdict.ABSTAIN,
                         Verdict.BLOCKED),
        must_quote=("universe_definition", "n_symbols_at_start",
                    "n_symbols_survived"),
        hard_bans=(
            "不得用『当前仍在交易』的币种池做历史回测而不声明",
            "不得在 n_symbols_at_start == n_symbols_survived 且无说明时给 PASS",
        ),
        basis=Basis.GENERAL_PRACTICE,
        basis_note=(
            "加密市场的幸存者偏差比股票市场小（退市不像股票那样普遍），"
            "但**不是没有**：交易所下架、稳定币脱锚、项目归零都发生过。"
            "我们的 3 币样本是主流币，偏差有限但必须声明。"
        ),
        anti_pattern="退化成不提这件事。",
    ),
]


# ---------------------------------------------------------------------------
# 库：路由与自检
# ---------------------------------------------------------------------------

class RoleLibrary:
    """角色卡库。**按输入槽路由，不按名字找。**"""

    def __init__(self, roles: list[RoleCard] | None = None):
        self._roles = list(roles if roles is not None else ROLES)

    def all(self) -> list[RoleCard]:
        return list(self._roles)

    def get(self, rid: str) -> RoleCard | None:
        return next((r for r in self._roles if r.id == rid), None)

    def route(self, available: set[Slot]) -> tuple[list[RoleCard], list[RoleCard]]:
        """按**实际可用的输入槽**决定哪些角色能上。

        返回 `(可运行, 被阻塞)`。

        **为什么按槽路由而不是按名字硬编码**：如果写死「每次跑五个角色」，
        那么缺 news 数据的角色会拿到空输入然后硬编一个结论出来——
        这比不跑它更糟。**能跑什么取决于手里有什么，不取决于计划里有什么。**
        """
        can, blocked = [], []
        for r in self._roles:
            (can if set(r.slots_in).issubset(available) else blocked).append(r)
        return can, blocked

    def blocked_reason(self, role: RoleCard,
                       available: set[Slot]) -> str:
        missing = [s.value for s in role.slots_in if s not in available]
        return f"缺输入槽：{missing}" if missing else ""

    def summary(self) -> dict:
        by_basis: dict[str, int] = {}
        for r in self._roles:
            by_basis[r.basis.value] = by_basis.get(r.basis.value, 0) + 1
        return {
            "n_roles": len(self._roles),
            "by_basis": by_basis,
            "n_with_hard_bans": sum(1 for r in self._roles if r.hard_bans),
            "n_general_practice": sum(
                1 for r in self._roles if r.basis == Basis.GENERAL_PRACTICE),
        }


def audit_roles(roles: list[RoleCard] | None = None) -> list[str]:
    """角色卡自检。空列表表示通过。

    查六件事，每件都对应一种「看起来有角色，实际没有」的退化：
    """
    roles = roles if roles is not None else ROLES
    problems: list[str] = []
    for r in roles:
        # 1. 必须恰好问一个问题。多问题会导致角色失焦，也导致无法裁决。
        if r.question.count("？") + r.question.count("?") != 1:
            problems.append(f"{r.id}: question 必须恰好一个问号")

        # 2. 必须允许 ABSTAIN 或 BLOCKED。不允许「判断不了」的角色
        #    一定会硬编答案——这是 LLM 角色的头号退化形态。
        if not ({Verdict.ABSTAIN, Verdict.BLOCKED} & set(r.verdict_options)):
            problems.append(
                f"{r.id}: 不允许 ABSTAIN/BLOCKED，角色会硬编一个答案")

        # 3. PASS/FAIL 角色必须有 must_quote，否则裁决无证据。
        if {Verdict.PASS, Verdict.FAIL} & set(r.verdict_options) \
                and not r.must_quote:
            problems.append(f"{r.id}: 能判 PASS/FAIL 但 must_quote 为空")

        # 4. 必须有 hard_bans。没有硬禁止的角色等于一个建议框。
        if not r.hard_bans:
            problems.append(f"{r.id}: 没有 hard_bans")

        # 5. 必须写 anti_pattern，否则不知道它退化成什么样时该报警。
        if not r.anti_pattern:
            problems.append(f"{r.id}: 没写 anti_pattern")

        # 6. basis 是 GENERAL_PRACTICE 的必须显式说明，
        #    不能让它看起来像有出处。
        if r.basis == Basis.GENERAL_PRACTICE and not r.basis_note:
            problems.append(
                f"{r.id}: basis=GENERAL_PRACTICE 但没有说明——"
                f"这会让『通行做法』看起来像有出处")
    return problems


if __name__ == "__main__":
    lib = RoleLibrary()
    s = lib.summary()
    print("=" * 78)
    print("可复用角色卡库")
    print("=" * 78)
    print(f"角色 {s['n_roles']} 个，全部带 hard_bans：{s['n_with_hard_bans'] == s['n_roles']}")
    print(f"来源可信度分布：{s['by_basis']}")
    print()
    for r in lib.all():
        print(f"{r.id:<16}{r.name:<14}[{r.basis.value}]")
        print(f"    Q: {r.question}")
        print(f"    IN: {[x.value for x in r.slots_in]}")
        print(f"    OUT: {[v.value for v in r.verdict_options]}")
        print(f"    禁止 {len(r.hard_bans)} 条，最易退化为：{r.anti_pattern[:52]}…")
        print()

    print("-" * 78)
    print("路由演示：只有 OHLCV + 成本，没有 news / 外部条目")
    avail = {Slot.OHLCV, Slot.COSTS, Slot.BACKTEST_RUN,
             Slot.MARKET_REGIME, Slot.STRUCTURE}
    can, blocked = lib.route(avail)
    print(f"  可运行 {len(can)} 个：{[r.id for r in can]}")
    print(f"  被阻塞 {len(blocked)} 个：")
    for r in blocked:
        print(f"    {r.id} ← {lib.blocked_reason(r, avail)}")

    print()
    problems = audit_roles()
    if problems:
        print(f"❌ 自检 {len(problems)} 个问题：")
        for p in problems:
            print(f"   · {p}")
    else:
        print("✅ 自检通过")