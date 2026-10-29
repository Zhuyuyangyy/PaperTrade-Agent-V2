# 市场调研：别人在这个赛道怎么赚钱（2026-10-29）

> 背景：自研六 agent 交易线已停止（E≈C，无增量）；外部 3 策略审计全部证伪；可转债折价无 edge。
> 本报告回答一个问题：**这个生态里谁在赚钱、靠什么赚钱、钱从哪来到哪去，以及对我们（个人/学生/小团队）哪些方向真实可行。**
> 调研范围：Reddit、X/Twitter、YouTube、Indie Hackers、GitHub、Discord/Telegram、知识星球、B站、Product Hunt、agent marketplace（Toku 等），中英文双语。

---

## 0. 结论先行（TL;DR）

1. **交易侧个人基本赚不到钱**，这是结构性事实而非能力问题：个人没有资本、速度、信息优势；backtest→实盘中位差 **-14.3pp 年化**（6 平台实测）；17/22 个零售 bot 本质上只是指数 beta。nof1.ai Alpha Arena 六大前沿模型 $10K 实盘 17 天，4 个亏 30%-63%，唯一赢家 Qwen 靠"43 笔低频+严止损"——**LLM 直接管账户已被双重证伪（我们自己的实验 + 这个公开实验）**。
2. **赚钱的人分四层，没有一层是"用自己的钱交易"**：平台/交易所 → 工具与框架商（卖铲子）→ 内容/IP（卖注意力）→ 带单/资管（卖信任）。外加新出现的第五层：**agent 经济（agent 替人干活收钱，不是 agent 自己交易）**。
3. **对我们最值钱的方向不是"赚钱"，是"求职杠杆"**：归档 README 里 6 条否定结论 + 完整仪器链（path-aware replay、成本校准、随机对照组、事前证伪），比任何"收益率 300%"的垃圾项目都值钱——这是 AI 应用开发实习岗的最强面试材料。
4. **如果坚持"agent 自动赚钱"叙事，唯一被验证的靶心是"替人干活收钱"**（Toku marketplace 真实 USD 结算、micro-SaaS 案例 $4.8K-$18K MRR），不是"自己交易"。
5. **所有躺赚叙事的共同隐藏成本是 distribution**：开源项目 90% 收入为 $0，500 star 以下基本无偿；内容变现要 4-18 个月持续产出。没有受众就没有被动收入。

---

## 1. 生态全景：钱从哪来，到哪去

```
                    ┌─────────────────────────────────────────────┐
                    │  终端用户/散户（净流出方：学费、订阅费、亏损）  │
                    └──────┬──────────┬──────────┬──────────┬───────┘
                           │          │          │          │
              交易手续费/清算费   订阅费     广告/商单    打赏/会费
                           │          │          │          │
        ┌──────────────────▼──┐  ┌────▼─────┐  ┌▼────────┐ ┌▼──────────┐
        │ ① 平台/交易所层      │  │ ② 工具层 │  │ ③ 内容层 │ │ ④ 信任层   │
        │ Hyperliquid/Binance │  │ 框架/SaaS│  │ YT/B站/ │ │ 带单/vault│
        │ HLP: 做市+清算      │  │ 回测工具 │  │ 星球/X  │ │ prop/信号 │
        │ 手续费+清算费 = 印钞 │  │ 返佣/赞助│  │ RPM最高 │ │ 1.5%AUC  │
        └─────────────────────┘  └──────────┘  └─────────┘ └───────────┘
                                     │              │
                                     └──────┬───────┘
                                    ⑤ agent 经济层（新）
                                    Toku 等 marketplace：
                                    agent 接单收 USD（85% 归自己）
```

**关键认知：每一层都在向终端用户收钱，只有"自己用本金交易"的散户是净付出方。**

---

## 2. 各层单位经济（实测数据）

### ① 平台/交易所层（个人做不了，但要知道钱有多大）

| 事实 | 数据 | 来源 |
|---|---|---|
| Hyperliquid HLP 协议金库（做市+清算） | 2026-10-28 持仓 $174.3M，当月 +$618K，2023-05 至今累计 +$138.8M，当前 APR 3.78% | DeFiLlama/cipher-intelligence |
| 平台年化手续费收入 | HLP 相关约 $9.94M/年 | DeFiLlama |
| 结论 | 平台是食物链顶端，个人无入场券 | — |

### ② 工具/框架层（"卖铲子"，个人可做）

| 模式 | 真实单位经济 | 案例 |
|---|---|---|
| 开源框架 + 基金会赞助 | Hummingbot：14K Discord、6K GitHub star，用户群日交易量 $20-100M，赞助方是**交易所/协议**（买 connector 导流） | hummingbot/hummingbot |
| 开源软件 + broker 返佣 | DepthSight：AGPL 自托管 28 万行平台，**Bybit Broker Partner**，用户交易交易所分佣金，软件永久免费 | Indie Hackers 实测帖 |
| Open-core / pro license | Jesse：MIT 核心 + pro 付费层；OctoBot：免费核心 + 云托管订阅 | bitcointradingbots.com |
| GitHub Sponsors | 1000+ star 项目 $300-900/月（主要来自少数企业赞助） | 2026 OSS Monetization Report |
| **star 数与收入映射** | 0-500 star：$0-50/月；500-2K：$100-800/月；2K-10K：$800-5000/月；10K+：$3000-15000+/月 | 同上 |
| **残酷现实** | **90% 开源项目收入为 $0**；多数 maintainer 3 个月后放弃 | 同上 |

### ③ 内容/IP 层（流量变现，财经是最高 RPM 赛道）

| 平台 | 单位经济 | 门槛 |
|---|---|---|
| YouTube（美区） | 财经 RPM $15-30/千次播放；100 万月播放 = $15-30K/月；赞助 $5K-50K/条（100K-500K 订阅）；affiliate 券商推荐 $50-500/人 | YPP 1000 订阅 |
| B站 | 创作激励 ~每千播放 2-3 元（50 万播放≈1000-1500 元）；花火商单 ≈ 近 30 天平均播放 × 0.05-0.15 元；充电 ~70% 分成；舰长 138 元/月 ~50% 分成 | 花火需 1 万粉 |
| 知识星球 | 平台抽 20%（个人）/ ~5%（企业）。极端案例：唐朝 1499 元/年 × 6000 人 = 899 万流水（净 ~720 万）。垂类量化星球 899 元/年、续费率 75% | 无 |
| X/Substack | fin-twit 付费通讯 + build-in-public 导流到 SaaS | 无 |
| 内容工场（agent 跑） | 40 个内容套利 agent 月入 $8200；faceless YouTube 频道 7 亿播放赚 $1M+ | 需 agent 编排 |

### ④ 信任层（带单/资管/prop，卖的是"可验证 track record"）

| 模式 | 单位经济 | 现实 |
|---|---|---|
| eToro Popular Investor | 1.5% AUC/年。Champion 档（$50K AUC + 5 跟单者）月付 $250-500 封顶；$1M AUC = $15K/年 | 52% 零售 CFD 账户亏损；需要先有 2 个月可查记录 |
| Hyperliquid 社区 vault | leader 抽 10% 利润，需 ≥5% 本金共担。2026-10-22 有 3072 个 vault、总 $94.8M，**前十大 APR -25.2% ~ +18.7%** | 多数 leader 不赚钱 |
| 信号群（TG/Discord） | $20-200/月/订户 | 执行滞后吃掉 edge，纯信号生意在 2026 年向自动执行迁移 |
| Prop firm（FTMO 等） | 中位 funded trader **$0-2000/月**；仅 5-10% 挑战者拿到过任何一次 payout；85-90% 挂 evaluation；拿到账户的 40-50% 三个月内爆仓 | FTMO 2023 全年派出 $75.17M——钱 mostly 被平台和少数人赚走 |

### ⑤ agent 经济层（2026 最新，真实但早期）

| 平台 | 模式 | 已验证数据 |
|---|---|---|
| **Toku.agency** | agent 注册服务、人/agent 发 job、agent 竞价接单，**Stripe 真实 USD 结算，85% 归 agent** | 官方口径（2026-05）：193 个注册 agent、3 个 open job / 90+ 竞价；需求集中在 research / writing / code review / **security audits**； payout 需 Stripe KYC |
| Codex 自主接单 | AI 自己接安全审计悬赏 | Sam Altman 2026-05-10 推文：赚了 $16.88 |
| OpenWork / AgentAds / ClawGig / MoltX | 代币或 USDC 结算 | 早期，多数是 agent 卖给 agent，流动性是瓶颈 |
| micro-SaaS（agent 辅助运营） | 单人 + agent 编排做垂直 SaaS | 已验证案例：BridgeMind $18,320 MRR（语言学毕业生 8 个月单人）；AI Cold Email 工具 $11,400 MRR（6 个月）；Proposal Writer $4,800 MRR |

**Solo dev SaaS 大盘（TrustMRR 3,478 家，2026）**：中位 $1K-8K MRR；AI 类平均 $1,746 MRR（1,213 家）；前 10% > $10K MRR；约 2-3% 到 $1M ARR。里程碑：M3-6 $0-1K → M6-12 $1-5K → M12-18 $5-15K → M18-24 $10-30K（单人天花板）。

---

## 3. 平台地图：人在哪、需求在哪、话术是什么

| 平台 | 谁在这 | 对我们的用处 |
|---|---|---|
| **Reddit**（r/algotrading、r/quant、r/SideHustle、r/Entrepreneur） | 最 skeptical 的社区，大量诚实 post-mortem；共识："真有印钞机你会卖？" | 找需求痛点（回测造假是月经帖）；冷启动发帖（Show HN/Story 帖一次成功 = 800-2000 star） |
| **X/Twitter** | fin-twit、build-in-public 独立开发者 | 获客主渠道；透明 MRR 文化 |
| **Indie Hackers** | 公开 MRR 的 solo founder | 案例库 + 同类产品竞价调研 |
| **YouTube** | 财经 faceless 频道、quant 教学 | RPM 最高赛道；但需英文或强制作能力 |
| **GitHub** | 框架生态（freqtrade/hummingbot/passivbot） | 开源+sponsors+broker 返佣模式 |
| **Discord/Telegram** | 做市商社区（Hummingbot 14K）、信号群 | 找真实 trader 痛点；中文信号群生态 |
| **知识星球/公众号** | 国内量化从业者（如"自由星球"从业者限定 99-199 元/年） | 国内验证服务的销售渠道；注意合规（不碰荐股喊单） |
| **B站** | 学生/年轻技术受众 | 与简历线协同的个人品牌 |
| **Product Hunt** | 出海 AI 产品冷启动 | 标准化发布流程 |
| **Toku.agency 等 agent marketplace** | agent 开发者、agent 买方 | **零成本接单实验场**（见 §5-A5） |

---

## 4. 结构性事实：为什么"用自己的钱交易"对个人是死路

1. **Backtest→实盘衰减**（Broker Tested Reviews 2024-2025 实盘对照 6 平台）：年化收益中位 35.2%→20.7%（-14.5pp）；最大回撤 8.1%→14.6%；Sharpe 1.82→0.94；胜率 68%→54%。
2. **beta 伪装成 alpha**：实测 17/22 个零售 bot 与纳指/S&P 相关性 >0.8，所谓"市场中性"是营销话术。
3. **手续费杀死高频**：Alpha Arena 上 Gemini 238 笔交易手续费 $1,331（占本金 13%）；我们的校准也显示 $1000 往返 120bps、$100 实际 60bps。
4. **有 edge 的人不卖**：Reddit 共识 + 我们自己的证伪（E≈C）+ Alpha Arena（4/6 模型巨亏）三重证据。
5. **prop/带单是幸存者叙事**：中位 $0-2000/月，5-10% 拿过 payout，且需要先证明自己——我们没有 track record。
6. **真正的印钞机是平台**：HLP 一个金库 2023 年至今赚 $138.8M，零营销。

**推论：个人在这个生态的期望值排序 = 卖工具/卖验证 > 卖内容 > 卖时间（接单/咨询）> 卖信任（带单）> 用自己的钱交易。**

---

## 5. 对我们的方向评估与排序

### 我们的资产盘点

| 资产 | 稀缺性 |
|---|---|
| path-aware replay 引擎（bar 内 SL/TP 判定、maker 穿越成交模型） | ★★★ 市面 95% 回测是"向量化近似"，不问执行细节 |
| 成本校准体系（实测 60bps vs 配置 120bps） | ★★★ 直接打中 backtest-live gap 这个行业第一痛点 |
| 随机入场对照组 + bootstrap CI + 事前证伪规则 | ★★★ 这是"验证方法论"，可迁移到任何策略/项目 |
| 7-agent 编排 + WAL 账本 + 看门狗/roundlock/STOP 工程 | ★★ 工程能力证明（面试有用） |
| ML/数据背景 + 竞赛/专利/SRC 安全能力 | ★★ 复合背景 |
| 时间碎片化、无本金、无 track record、中国身份（Stripe 提现链路待验证） | 约束 |

### 方向排序

**S 级——立刻可做，与现有资产强协同**

| # | 方向 | 为什么 | 怎么做 | 预期 |
|---|---|---|---|---|
| S1 | **求职作品集化（ROI 最高）** | 归档 README 的 6 条否定结论 + 仪器链 = "我会验证、我会止损、我懂成本" | 把 README 精炼成简历项目经历 + 面试讲述（如何证伪、如何校准、如何设计对照组） | 直接服务 AI 应用开发实习岗主线 |
| S2 | **策略验证即服务（Backtest/Audit-as-a-Service）** | 市面回测服务画曲线，我们做执行级验证+对照组；Toku 上 research/code review/security audit 有真实需求；海外 gig 价 $120-1200 | 把 replay 引擎包成 CLI/SaaS：输入策略描述 → 输出 path-aware 回放 + 成本敏感性 + 随机对照组 + 证伪报告 | 首单 $100-500；3 个案例后可定价 $500+ |

**A 级——3-6 个月启动**

| # | 方向 | 关键数据 | 风险 |
|---|---|---|---|
| A1 | 开源审计工具 + broker 返佣（DepthSight/Hummingbot 模式） | 500+ star 才有 $100-800/月；90% 项目 $0 | 需持续运营 6-12 个月；中国身份开 broker 账户待验证 |
| A2 | 内容线（B站/公众号/X）："我如何用对照组证伪了自己的交易系统" | 财经/quant 是最高 RPM 赛道；诚实向内容是蓝海（别人都在晒收益率） | 与简历线协同；4-18 个月才见钱；需要表达能力 |
| A3 | Agent marketplace 接单（Toku 等） | 零成本注册；security audit 有真实 job；85% 归己 | Stripe KYC 需本人证件+W-8BEN；市场流动性早期，收入不稳定 |

**B/C 级——不建议或仅玩具**

| # | 方向 | 否决理由 |
|---|---|---|
| B1 | 空投/积分 farming | 需本金+大量时间；casual 净期望 $200-600/月；国内合规灰色；Sybil 检测趋严 |
| B2 | Prop firm | 85-90% 挂 evaluation；与我们"无 edge"的实证结论冲突 |
| B3 | 带单/卖信号 | 需要可验证 track record（没有）；且执行滞后杀 edge；道德风险 |
| B4 | 自营交易 | 已双重证伪（自己实验 + Alpha Arena）。除非转做市/vault，需资本 |
| B5 | 通用 agent 交易平台创业 | 红海 + 需要资金/合规/团队 |

### 如果还想做"agent 自动赚钱"：最小验证路径（换靶心）

```
旧靶心：agent 自己交易赚钱          → 已证伪（E≈C + Alpha Arena 4/6 巨亏）
新靶心：agent 替人干活收钱          → 已验证（Toku 真实 USD + micro-SaaS $4.8K-18K MRR）

产品：策略验证服务（引擎已有，包装即可）
渠道：Toku 接单 + 闲鱼/淘宝 + 知识星球
指标：30 天内 3 个付费 job（或 10 个免费 job 换 case study）
预算：$0-50/月（API + VPS）
止损线：60 天零付费 → 回到 S1 求职线，工具链留作作品集
```

---

## 6. 来源与可信度附注

**高可信（一手/实测）**：nof1.ai Alpha Arena 官方数据（多家媒体交叉）；Broker Tested Reviews 实盘对照（2024-2025 funded accounts）；eToro 官方 Popular Investor 条款；Hyperliquid 官方 docs + DeFiLlama；TrustMRR/BigIdeasDB 3,478 家 MRR 统计；Indie Hackers 公开帖；Toku 官方口径帖（thecolony.ai）；jakeinsight OSS 收入报告；FTMO 官方 payout 统计。

**中等可信（行业媒体/聚合）**：bigideasdb、statly/earntrackr（RPM 数据为区间估计）；fortraders/rbtrading（prop 分布数据）；雪球 B站 UP 主收入解析。

**低可信（SEO 内容站，仅作方向参考，数字未采信）**：earningideashub、paraguay-resident、protraderdaily（空投月入 $50K 类）、fomoed（自带推广）、breakthecubicle/moltstack（agent 副业案例集，方向有参考价值，具体数字需自行验证）。本报告所有进入结论的数字均来自高/中可信来源。

---

*调研完成时间：2026-10-29。建议 90 天后复查：Toku 流动性、AI SaaS 中位 MRR、Alpha Arena S2 结果。*
