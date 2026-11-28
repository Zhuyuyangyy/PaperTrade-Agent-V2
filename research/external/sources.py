"""外部策略资源的来源登记（Sources Registry）。

**这个文件的第一职责不是收集，是拒绝。**

用户给的清单来自另一个模型的调研摘要，里面混着三类东西：
  1. 我核实过的、确实存在且可用的
  2. 名字对但描述有偏的（比如策略语言、许可证、功能边界）
  3. 看起来合理但我无法验证的

对第 2、3 类，填进去就等于制造了一个「看起来很权威但实际不成立」的
研究起点。所以每条记录必须带 `verified` 字段，且 `verified` 只能是
下面四个值之一：

    VERIFIED      我实际打开过页面/仓库，下面的字段来自页面正文
    PARTIAL       页面可访问，但关键字段（许可证/规模）未能在页面上确认
    UNVERIFIED    仅存在推荐/转述，我没有直接访问
    NOT_FOUND    访问返回 404 或内容不存在

**`UNVERIFIED` 和 `NOT_FOUND` 的条目保留在表里，但不得被下游当作
可复用来源。** 保留是为了记录「我们查过、结论是这样」，而不是为了凑数。

关于许可证：这一列决定我们能不能复制代码、还是只能读方法自己重写。
`NO_LICENSE` 意味着默认保留所有权利——**只能读，不能复制**。
这条比任何「开源」的说法都硬。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VERIFIED = "VERIFIED"
PARTIAL = "PARTIAL"
UNVERIFIED = "UNVERIFIED"
NOT_FOUND = "NOT_FOUND"

Status = Literal["VERIFIED", "PARTIAL", "UNVERIFIED", "NOT_FOUND"]

# 许可证 → 我们允许做什么
LICENSE_POLICY = {
    "MIT": ("可复制、可修改、可再分发，需保留版权声明",
            "COPY_OK"),
    "Apache-2.0": ("可复制、可修改、需保留声明与变更说明，含专利授权",
                   "COPY_OK"),
    "BSD-3-Clause": ("可复制，需保留版权与免责声明", "COPY_OK"),
    "NO_LICENSE": ("默认保留所有权利。**只能读方法，不能复制代码**",
                   "READ_ONLY"),
    "UNKNOWN": ("未确认。按最严格处理，只能读", "READ_ONLY"),
    "DOCS_ONLY": ("公开文档站，非代码仓库。方法可读，无代码可复制",
                  "READ_ONLY"),
}


@dataclass
class Source:
    key: str
    name: str
    url: str
    kind: str                 # repo | docs | collection
    license: str
    verified: Status
    what_it_actually_is: str  # 我核实到的真实内容，不是宣传语
    reusable_for: list[str] = field(default_factory=list)
    not_reusable_for: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def can_copy_code(self) -> bool:
        return LICENSE_POLICY.get(self.license, ("", "READ_ONLY"))[1] == "COPY_OK"

    def to_dict(self) -> dict:
        pol, mode = LICENSE_POLICY.get(self.license, ("未确认", "READ_ONLY"))
        return {
            "key": self.key, "name": self.name, "url": self.url,
            "kind": self.kind, "license": self.license,
            "license_policy": pol, "copy_mode": mode,
            "can_copy_code": self.can_copy_code,
            "verified": self.verified,
            "what_it_actually_is": self.what_it_actually_is,
            "reusable_for": self.reusable_for,
            "not_reusable_for": self.not_reusable_for,
            "caveats": self.caveats,
        }


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------

SOURCES: list[Source] = [

    # ===== Minara 官方 =====

    Source(
        key="minara.docs",
        name="Minara Agent 技术文档",
        url="https://agent.minara.ai/docs/",
        kind="docs",
        license="DOCS_ONLY",
        verified=VERIFIED,
        what_it_actually_is=(
            "真实可访问的技术文档站，公开程度高于预期。核心页"
            "`system-design/memory/learning-system` 给出了完整实现细节："
            "三类被学习产物（tool_sequences / guidance_notes / methodologies）、"
            "SQLite 表结构（learnings / methodologies / methodology_lifecycle_events "
            "/ methodology_cases / llm_usage 等）、去重键定义、"
            "Wilson 下界置信度、quarantine 隔离区、毕业双门槛、"
            "反思阶梯（1d/7d/30d/90d/180d/365d/lazy）、"
            "预算断路器（四个类别各自日+月硬上限）、"
            "六维健康审计（含字节级 SHA-256 幂等不变量）。"
            "`features/strategy-studio` 与 `features/factor-research` 也公开。"
        ),
        reusable_for=[
            "方法论晋级/隔离的状态机设计（这是本轮最有价值的单份材料）",
            "用 Wilson 下界代替 times_correct/times_used 的理由：不让 1 次全中压过 30 次中 15 次",
            "结构化去重键：hash(direction + primary_signal + timeframe + 排序后 indicators + asset_class)",
            "反思阶梯的窗口设计（1d 问触发条件是否成立，7d 问基准情形是否兑现，30d 问期限估计）",
            "预算断路器的实现形态：调用发出前投影成本并抛 BudgetExceededError",
            "审计子系统不写学习状态——纯函数评分边界 + 表哈希幂等断言",
        ],
        not_reusable_for=[
            "不能据此复制任何代码——文档站是说明，不是实现",
            "文档未公开 DMind 模型权重、训练数据、服务端私有提示词",
        ],
        caveats=[
            "文档自述与实现可能不一致。已发现一处内部矛盾：方法论章节写"
            "`confidence >= INJECTION_THRESHOLD`（符号常量），Institution Mode "
            "章节写「Wilson ≥ 0.55 才浮现」，但文档没有显式写 "
            "INJECTION_THRESHOLD = 0.55。按保守取 0.55。",
            "「无降级操作」这句话只适用于 learnings 表；methodologies 表明确"
            "有 demoted / requantized_by_judge / synthesis_auto_demote。两处不可混读。",
        ],
    ),

    Source(
        key="minara.skills",
        name="Minara-AI/minara-skills",
        url="https://github.com/Minara-AI/minara-skills",
        kind="repo",
        license="MIT",
        verified=VERIFIED,
        what_it_actually_is=(
            "真实存在，MIT。但**内容比宣传的窄得多**：`skills/` 目录下只有"
            "一个包 `minara`，是 Minara CLI 的封装（登录、下单、钱包、限价单、"
            "永续、x402 支付）。**不含策略 DSL、不含因子研究、不含回测实现。**"
            "那些是 builtin skill，编译进二进制，不在这个仓库里。"
            "README 自称在 crypto-skill-bench 上 88/100（76 场景，"
            "66 passed / 10 partial / 0 failed）。"
        ),
        reusable_for=[
            "SKILL.md 的组织形式与 CLI 集成方式（作为我们的 skill 封装参考）",
            "金融工具调用的常用参数与错误处理形态",
        ],
        not_reusable_for=[
            "**不能从这里拿到任何策略或因子实现——它没有**",
            "benchmark 分数是自报，未独立复现，不得当作能力证据",
        ],
        caveats=[
            "用户摘要说它「公开 Agent 技能定义、金融工具调用方式」——这句是对的，"
            "但容易误读成「策略也开源了」。实际没有。",
        ],
    ),

    # ===== 策略/因子实现类 =====

    Source(
        key="hkuds.vibe-trading",
        name="HKUDS/Vibe-Trading",
        url="https://github.com/HKUDS/Vibe-Trading",
        kind="repo",
        license="MIT",
        verified=VERIFIED,
        what_it_actually_is=(
            "真实存在，MIT，活跃（2026-11-28 仍有提交），有 arXiv 论文 "
            "arXiv:2611.24845。技能在 `agent/src/skills/<name>/SKILL.md`，"
            "清单在 `agent/SKILL.md`，自述 92 skills / 31 swarm teams。"
            "README 自述：27 行情源、12 市场引擎、478 alpha 因子、"
            "309 量化库函数、81 MCP 工具、19 券商连接器。"
        ),
        reusable_for=[
            "SKILL.md 的目录组织方式（我们的 30 卡可以对齐这套结构）",
            "多 Agent 协作面板与 Swarm 研究团队的编排思路",
            "OS 级沙箱隔离（macOS Seatbelt / Linux bwrap + Landlock）——"
            "跑陌生策略时的必要防护",
        ],
        not_reusable_for=[
            "自述的 92 skills / 478 因子等规模数字未经我方核验，"
            "**不得写入我们的任何结论**",
            "不能因为它是 MIT 就把 478 个因子当成已验证策略——那是数量不是质量",
        ],
        caveats=[
            "2026-11-28 刚做 kernel 重写，升级需备份 `~/.vibe-trading/sessions`，"
            "说明接口尚不稳定。",
            "用户摘要说「19 种属于策略类包括 SMC、裸K、波浪理论」——"
            "这一类细分计数我未能逐条核实。",
        ],
    ),

    Source(
        key="robotic.freqtrade-strategies",
        name="RoboticAutomations/freqtradestrategies",
        url="https://github.com/RoboticAutomations/freqtradestrategies",
        kind="collection",
        license="NO_LICENSE",
        verified=VERIFIED,
        what_it_actually_is=(
            "1,286 个 .py 文件，909 个 Freqtrade 策略/FreqAI 模型，"
            "**137 个被标记为前视偏差**，其中 `dirty LA/` 目录专门存放。"
            "**许可证不是常见的开源协议**：原文写「collected from public sources; "
            "individual authors retain their rights」，即各文件版权归原作者，"
            "仓库本身没有统一授权。**这意味着不能整包复制。**"
            "最有价值的其实不是策略，是 `docs/LOOKAHEAD.md`——"
            "一份可复现的 AST 静态前视检测方法说明。"
        ),
        reusable_for=[
            "**`docs/LOOKAHEAD.md` 的检测方法**：12 条规则、AST 而非正则、"
            "高危/中危分级、误报治理（310→137）、复现伪代码。"
            "我们自己的 `lookahead_scan.py` 就是基于它写的。",
            "「自称已修复」不可信的实证：多个文件注释写 `# FIXED`、"
            "`LOOKAHEAD-BIAS FIX`，静态模式仍被触发。",
            "偏差形态高度集中在四条：rolling_center(59) > extrema_scan(52) > "
            "negative_shift(34) > backfill(23)。",
        ],
        not_reusable_for=[
            "**不能整包复制策略代码**——无统一许可证，各文件版权归原作者",
            "**绝不能把这里的策略当成候选盈利策略**。仓库自己第一句就是"
            "「Nothing here has been verified as profitable」",
            "137 个只是「被标记」，还有大量未被标记的也可能有偏差"
            "（如通过变量间接构造的 shift、自定义函数封装的未来引用）",
        ],
        caveats=[
            "「909 个策略」里 515 个是 SHA-256 去重后的结果，"
            "剩下的很多是「只改了参数的近重复 fork」——"
            "**独立假设数量远小于 909**。这一点直接影响我们能否把它当样本池。",
            "语料按家族聚集（battle-tank / hurst-cycle / nostalgia-for-infinity / "
            "peekaboo / FreqAI），同一份代码改名微调反复上传。"
            "当成 909 个独立策略做多重检验校正会严重高估有效样本量。",
        ],
    ),

    Source(
        key="ms.rd-agent",
        name="microsoft/RD-Agent",
        url="https://github.com/microsoft/RD-Agent",
        kind="repo",
        license="UNKNOWN",
        verified=PARTIAL,
        what_it_actually_is=(
            "真实存在，微软开源，定位是「AI 驱动数据驱动的 AI 研发」，"
            "即自动化提出假设→写代码→跑实验→分析→下一轮的闭环。"
            "**注意：它是通用 R&D 自动化框架，不是专用量化框架。**"
            "技术报告在 aka.ms/RD-Agent-Tech-Report。"
        ),
        reusable_for=[
            "假设→实现→回测→诊断→下一轮的闭环编排（我们 Phase 5 的参考）",
            "失败诊断如何回灌到下一轮假设（这是最容易做成空壳的一环）",
        ],
        not_reusable_for=[
            "许可证我未在页面上确认，按 UNKNOWN 处理，只能读不能复制",
            "不能假设它自带金融数据接入——通用框架通常需要自己接",
        ],
        caveats=[
            "用户摘要说它「能够自主提出假设、构建因子、实现代码、回测」，"
            "这个描述方向没错，但它是把量化当作 R&D 的一个应用场景，"
            "不是为量化专门设计的。差异在于金融特有的东西（成本口径、"
            "幸存者偏差、前视）它不会替你做。",
        ],
    ),

    Source(
        key="tauric.tradingagents",
        name="TauricResearch/TradingAgents",
        url="https://github.com/TauricResearch/TradingAgents",
        kind="repo",
        license="Apache-2.0",
        verified=VERIFIED,
        what_it_actually_is=(
            "真实存在，Apache-2.0。但**已不是我原先以为的形态**："
            "2026-11 发布 v1.0.0，用 **TypeScript 重写**（package.json / "
            "tsconfig / biome.json），通过 npm 分发（@tauric/tradingagents），"
            "需要 Tauric API key 或自带模型 key。"
            "README 明确写：**「TradingAgents researches and answers; "
            "it does not place trades.」——它不做交易执行。**"
            "架构是主 agent + 四个专家 agent（Equity Research / Macro Strategy / "
            "Quant Research / Risk Analysis）+ 按 skill 走的研究流程。"
        ),
        reusable_for=[
            "角色分工：把「研究」拆成四个独立上下文的专家，而不是一个 prompt 里塞四种视角",
            "输出必须标注来源（每个数字带引用、末尾编号来源列表）——"
            "这条对我们是硬要求",
            "skill 以 Markdown 文件定义、用户可自行添加",
        ],
        not_reusable_for=[
            "**不能当作交易框架参考——它明确不下单**",
            "v1.0.0 是商业产品形态（要 API key），旧版 Python 多 Agent 实现"
            "已经不是主线，引用旧架构描述会过时",
        ],
        caveats=[
            "用户摘要说它「技术分析师、情绪分析师、研究员、交易员、风控团队"
            "分别拥有独立角色……重点研究它的角色提示词、辩论机制」。"
            "**v1.0.0 的角色已变成 Equity/Macro/Quant/Risk 四类研究角色，"
            "没有交易员角色。** 这个差异必须写清楚，否则我们会照着一个"
            "已经不存在的架构做设计。",
            "论文 arXiv:2412.20138 是 2024 年的 v0.x 架构，与当前 v1.0.0 不同。",
        ],
    ),

    Source(
        key="virattt.ai-hedge-fund",
        name="virattt/ai-hedge-fund",
        url="https://github.com/virattt/ai-hedge-fund",
        kind="repo",
        license="MIT",
        verified=VERIFIED,
        what_it_actually_is=(
            "真实存在，MIT，v2.7.2（2026-11）。**自称 proof of concept，"
            "仅教育用途，「the system does not actually make any trades」。**"
            "有 paper trading（hash-chained ledger）与 backtesting 两种模式。"
            "fund 定义存 `~/.hedge-fund/mandates/`，不带 ticker，"
            "每个回测单独选 universe。需要 Financial Datasets API key"
            "（或 Base 钱包 USDC 走 x402 按次付费）。"
        ),
        reusable_for=[
            "**hash-chained ledger 做 paper trading 的记账**——"
            "这直接解决我们「模拟盘怎么证明没被事后改过」的问题",
            "mandate 与 universe 分离：策略定义不带标的，回测时再选池子。"
            "这个解耦对我们的 30 卡有直接价值（现在卡里写死了币种假设）",
            "多投资者 agent（不同方法论代表）并行出决策再聚合",
        ],
        not_reusable_for=[
            "**不能当作可盈利系统参考**，作者自己说是 educational",
            "它依赖 Financial Datasets 的付费数据，我们没有；价格/财报接口不能直接复用",
        ],
        caveats=[
            "每次 fill 收 spread + commission（2026-10-24 提交），"
            "说明成本口径是被认真对待的——这一点值得我们对照检查自己的三分拆。",
        ],
    ),
]


# ---------------------------------------------------------------------------
# 修正记录：用户摘要与核实结果不一致的地方
# ---------------------------------------------------------------------------

CORRECTIONS = [
    {
        "claim": "Strategy Codegen 通过 TypeScript DSL 编写策略",
        "verdict": "CORRECT",
        "detail": (
            "核实通过。`minara-strategy-codegen` 的官方描述原文是"
            "「author, port, backtest, validate and deploy trading strategies "
            "in the pine-runtime TypeScript DSL」。所以 pine-runtime 是运行时名，"
            "TypeScript 是 DSL 形态。"
        ),
        "note": (
            "但要注意 `features/strategy-studio` 页面对用户只说「读代码用 Code 标签」，"
            "没说语言。TypeScript DSL 这个细节来自 builtin skills 参考页。"
        ),
    },
    {
        "claim": "Minara 官方 Skills 仓库（MIT）公开 Agent 技能定义、金融工具调用方式",
        "verdict": "PARTIALLY_WRONG",
        "detail": (
            "仓库真实、MIT 真实。但 `skills/` 下只有一个 `minara` 包，"
            "内容是 CLI 封装。**策略 DSL、因子研究、回测引擎都不在这个仓库**，"
            "它们是编译进二进制的 builtin skill。"
        ),
        "note": "所以「公开了部分 Skill」是对的，「策略语言公开了」是错的。",
    },
    {
        "claim": "TradingAgents：技术分析师、情绪分析师、研究员、交易员、风控团队分别独立角色，重点研究角色提示词与辩论机制",
        "verdict": "OUTDATED",
        "detail": (
            "描述对应 2024 年的 v0.x Python 版（arXiv:2412.20138）。"
            "当前主线是 2026-11 的 v1.0.0，TypeScript 重写，"
            "角色变为 Equity Research / Macro Strategy / Quant Research / "
            "Risk Analysis 四类，**明确不做交易执行**。"
        ),
        "note": "照旧架构做设计会扑空。要研究就直接读 v1.0.0 的 specs/ 与 src/。",
    },
    {
        "claim": "freqtradestrategies：社区整理了 909 个策略及 FreqAI 模型，标记 137 个可能前视偏差，可作为候选策略池",
        "verdict": "MISLEADING",
        "detail": (
            "数字都对（909 / 137），但两处会害人：①**无统一许可证**，"
            "各文件版权归原作者，「公共频道收集」不等于授权使用；"
            "②**909 是去重后的文件数，不是独立假设数**——语料按家族聚集，"
            "大量是只改参数的近重复 fork。"
        ),
        "note": (
            "真正值钱的是 `docs/LOOKAHEAD.md`（检测方法），不是那 909 个策略。"
            "把它当策略池会严重高估有效样本量。"
        ),
    },
    {
        "claim": "Minara 的自我学习不涉及模型微调，而是 SQLite 存储经过验证的方法论",
        "verdict": "CORRECT_AND_UNDERSTATED",
        "detail": (
            "核实通过，且实际公开的内容比这句话多得多："
            "完整表结构、Wilson 下界、quarantine 隔离、双门槛毕业、"
            "反思阶梯窗口、预算断路器、六维审计、"
            "以及四条安全不变量（learning 不能绕过权限层级 / 不能绕过 L3 风险闸门 / "
            "不能存密钥 / 失败回合永不成为 learning）。"
        ),
        "note": "这是整轮调研里唯一「比预期更详细」的部分。",
    },
]


def registry() -> dict:
    by_status: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for s in SOURCES:
        by_status[s.verified] = by_status.get(s.verified, 0) + 1
        _, mode = LICENSE_POLICY.get(s.license, ("", "READ_ONLY"))
        by_mode[mode] = by_mode.get(mode, 0) + 1
    return {
        "n_sources": len(SOURCES),
        "by_verified": by_status,
        "by_copy_mode": by_mode,
        "n_corrections": len(CORRECTIONS),
        "sources": [s.to_dict() for s in SOURCES],
        "corrections": CORRECTIONS,
    }


if __name__ == "__main__":
    r = registry()
    print(f"来源 {r['n_sources']} 个：{r['by_verified']}")
    print(f"可复制模式：{r['by_copy_mode']}")
    print()
    for s in SOURCES:
        flag = {"VERIFIED": "✅", "PARTIAL": "🟡",
                "UNVERIFIED": "⚪", "NOT_FOUND": "❌"}[s.verified]
        mode = LICENSE_POLICY.get(s.license, ("", "READ_ONLY"))[1]
        print(f"{flag} {s.key:<32}{s.license:<12}{mode:<10}{s.name}")
    print()
    print(f"修正 {len(CORRECTIONS)} 条：")
    for c in CORRECTIONS:
        print(f"  [{c['verdict']}] {c['claim'][:60]}")