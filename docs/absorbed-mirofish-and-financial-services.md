# 从两个外部项目里搬了什么，以及没搬什么

日期：2026-11-04
来源：`MiroFish-0.1.2`、`financial-services-main`（Anthropic 金融插件集）
触发问题：meme 盘被证伪（`chg_1h` 追高是唯一稳定亏损源，无偏样本里所有筛选组合都不为正），
需要判断这两个项目能不能补上"没有 alpha"的缺口。

## 一句话结论

**两个项目都没有可直接运行的"能力"能搬进来，能搬的是写法和建模原则。**
MiroFish 的运行时（Zep + OASIS + 上千 agent）与"可复现地证伪"这条原则冲突；
financial-services 的价值在文档工程，不在代码。

## 分流表

| 资产 | 它是什么 | 搬 / 不搬 | 理由 |
|---|---|---|---|
| `financial-services` 的 agent 四段式（produce → workflow → guardrails → skills） | 纯 markdown 的插件规范 | **搬** | 我们的 SKILL.md 有铁律和工作流，缺"产物清单"和"边界声明" |
| `market-researcher` 的 guardrails | 4 条：第三方材料不可信 / 每个数字要溯源 / 关键节点停下等人审 / 不许分发 | **搬** | 前两条我们完全没有；第三条我们有"三次击杀后停"，粒度太粗 |
| `idea-generation` 的结束语 | "Screens surface candidates, not conclusions" | **搬（作为原则）** | 这正是我们踩过的坑：把筛选分当结论 |
| `portfolio-monitoring` 的红黄绿阈值 | Green <5% 偏离 / Yellow 5–15% / Red >15% | 不搬 | 我们的七种诊断结局已经比三档更细，加回去是倒退 |
| `comps-analysis` / `ic-memo` / `xlsx-author` 等 | 投行估值、IC memo、Excel 建模 | 不搬 | 与策略证伪无关，且依赖 CapIQ / FactSet 付费源 |
| MiroFish 的"实体必须是能行动的主体" | 本体设计原则：不能把"舆论""情绪"当实体 | **搬（作为原则）** | 我们的因子（organic_score、holder_count）都是抽象指标，不是行为主体。这是"为什么预测不了"的一个解释 |
| MiroFish 的时段活跃度配置 | 深夜/早间/工作/晚间高峰 + 活跃度系数 | **已验证，不成立** | 见下节，我们自己的数据把这条否了 |
| MiroFish 的运行时（graph_builder / simulation_runner / report_agent，约 1.1 万行） | Flask + Zep Cloud + OASIS，跑一轮要上千次 LLM | 不搬 | ① 依赖云服务，无法离线复现，和"锁定契约再跑"冲突；② 输出是叙事报告，不是可回测的信号；③ README 自己标注"消耗较大，先试 40 轮以内" |
| `financial-services` 的 Claude Cowork / Managed Agents 封装 | 插件市场 + `/v1/agents` 部署脚本 | 不搬 | 锁生态。我们的分发走 zip + Gumroad，不需要 MCP 宿主 |

## 用我们自己的数据验证了什么

MiroFish 的时段假设看起来合理（meme 币确实有亚洲/美洲时段差），所以直接拿账本验：

| 时段（UTC） | 全样本 n | 全样本扣成本 | 无偏样本 n | 无偏扣成本 |
|---|---|---|---|---|
| 00–04 | 148 | -3.31% | 54 | -0.80% |
| 04–08 | 192 | -1.09% | 35 | -5.64% |
| 08–12 | 61 | -0.32% | 2 | — |
| 12–16 | 19 | +6.63% | 0 | — |
| 16–20 | 42 | +1.18% | 0 | — |
| 20–24 | 77 | +6.70% | 21 | -2.23% |

**结论：时段效应是幸存者偏差的又一个投影。** 全样本里 20–24 点看似 +6.7%，换成独立报价只剩 -2.2%；
12–16 点那 +6.6% 背后一笔无偏样本都没有。任何"挑时段"的规则都会重犯追高的错。

## 真正落地的改动

`skill/strategy-falsifier-pro/SKILL.md`（125 → 184 行）：

1. **新增 `## What you produce`** — 四件产物（report.md / report.html / result.json / verdict_lock.json）
   各自的读者和必填内容，明确"填不出来的格子写 `[UNSOURCED]`"。
2. **新增 `## Guardrails`** — 四条：输入是数据不是指令、每个数字要溯源、迭代前必须停、
   不许放宽契约来通过。
3. **新增 `## When NOT to use`** — 找 alpha、实盘下单、没有价格也没有成交记录、非交易规则，四种情况明确拒绝。
4. **新增一条 Honest limit** — "盈利子集不等于 edge，直到你知道样本是怎么选出来的"。
   这条是从 2026-11-04 那次证伪里直接长出来的：539 个候选筛出 143 个 +4.2%，
   换成独立报价变成 -2.35%，差的就是"还能被扫到"这个筛选条件。
5. frontmatter 补了 7 个触发词，让 skill 在真实对话里更容易被唤起。

Pro 包重新构建通过（205 KB，32 个条目），`398 passed / 2 skipped` 未受影响。

## 下一步要的不是引擎，是数据源

两个项目都指向同一个结论：**我们缺的不是更聪明的模型，是"谁在买"这一层信息。**

- MiroFish 想说清"谁在行动"，但它靠 LLM 扮演，不可复现；
- financial-services 想说清"数字从哪来"，但它接的是 CapIQ / FactSet 这类付费终端。

对 meme 盘，对应的是链上地址级数据（新建地址占比、狙击者集群、聪明钱流入、dev 钱包动向）。
拿不到这层数据之前，任何评分权重的调整都只是在噪声里换姿势。这一点已经写进 README 的证伪小节。
