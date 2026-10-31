# 分发提交包：渠道清单 + 提交文案（2026-10-30）

目标：提升 GitHub stars（Skill 售卖的信任货币）。
原则与 `reddit-algotrading.md` / `x-thread.md` 一致：**只发可复现的数字，链接指向开源仓库；不荐股、不带单、不承诺收益**。所有草稿语气沿用现有口径——诚实、自嘲、数据密集。
铁律：本文只创建 `docs/dist/submissions.md` 一个文件；所有渠道 URL 均已联网核实存在（核实日期 2026-10-30）。

## 0. 数据口径备忘（所有草稿共用，别改数字）

| 数字 | 出处 |
|---|---|
| 36 小时实验、252 笔平仓、六 agent 管线、四实验臂 | README |
| E（六 agent 全开）-94.68 vs C（organic_score 买前 3）-102.75，增量价值约 $8 | README |
| 14 个筛选队列，lift 的 95% CI 全部跨 0 | README |
| 毛利 +18bps/笔 vs LP 费地板 50bps（结构性为负） | README |
| Path-aware：CascadeFader 仓库自记 +469%/91% 胜率 → 路径回放 +132%/PF 1.62（同批信号差 3.5 倍） | README |
| 成本：$100 单进中位 $799k 池往返 60bps，其中 50bps 是 LP 地板；配置里 120bps 是给 $1,000 单定的价 | README |
| 四个事前声明案例（统一 29,347 根 1h futures 真实 K 线）：抄底 BTC 1310 笔 -505.65%（refuted）；追涨 BTC 1638 笔 **+34.35%** 仍 FALSIFIED（vs 随机对照 +0.03%，CI 跨 0）；抄底 ETH 1419 笔 -1144.10%；maker 限价 1301 笔 -453.20% | docs/cases/ |
| FundingRate：三档费率 × 两种成交模型，maker 真实成交 PF 1.00 | README |
| A 股可转债：509 只券、334,639 债券日、55,412 信号，1 日超额 ±0.5bp 内（噪声） | README |
| 行业外部数据：backtest→live 中位差 -14.3pp；17/22 零售 bot 是指数 beta（Broker Tested Reviews） | docs/dist/ 现有文案 |
| 测试：verify 引擎 60 用例，全套 358 passed / 2 skipped；MIT | README |
| 服务口径：前 10 单免费换匿名公开发布权 / $99 / $299；不接荐股带单代管资金 | toku-profile.md |

链接（全文统一用这两个）：
- 仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
- 落地页：https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/

---

## 1. 渠道总表（17 个，含 2 个"不建议"）

| # | 渠道 | 真实 URL | 类型 | 优先级 | 单次耗时 |
|---|---|---|---|---|---|
| 1 | awesome-quant | https://github.com/wilsonfreitas/awesome-quant | GitHub 列表 PR | **P0** | 20 min |
| 2 | Show HN | https://news.ycombinator.com/submit | 英文社区 | **P0** | 30 min |
| 3 | GitHub Topics | 仓库页 → About 齿轮 | 站内设置 | **P0** | 10 min |
| 4 | awesome-systematic-trading | https://github.com/paperswithbacktest/awesome-systematic-trading | GitHub 列表 PR | P1 | 20 min |
| 5 | r/algotrading | https://www.reddit.com/r/algotrading/ | Reddit | P1 | 已有文案 |
| 6 | 掘金 | https://juejin.cn/editor/drafts/new | 中文社区 | P1 | 60 min |
| 7 | V2EX | https://www.v2ex.com/go/create | 中文社区 | P2 | 30 min |
| 8 | 知乎 | https://www.zhihu.com/creator | 中文社区 | P2 | 60 min |
| 9 | r/quant | https://www.reddit.com/r/quant/ | Reddit | P2 | 20 min |
| 10 | LINUX DO | https://linux.do/ | 中文社区 | P2 | 30 min |
| 11 | r/Python | https://www.reddit.com/r/Python/ | Reddit | P3 | 20 min |
| 12 | Ruby China | https://ruby-china.org/ | 中文社区 | P3 | 30 min |
| 13 | awesome-crypto-trading-agents | https://github.com/shakeebshaan/awesome-crypto-trading-agents | GitHub 列表 PR | P3 | 15 min |
| 14 | Product Hunt | https://www.producthunt.com/launch | 产品目录 | P3 | 90 min |
| 15 | r/quantfinance | https://www.reddit.com/r/quantfinance/ | Reddit | P3 | 15 min |
| 16 | AlternativeTo | https://alternativeto.net/ | 产品目录 | P3 | 20 min |
| — | ~~botcrypto-io/awesome-crypto-trading-bots~~ | https://github.com/botcrypto-io/awesome-crypto-trading-bots | GitHub 列表 PR | **不建议** | 见 §2.4 |
| — | ~~joelowj/awesome-algorithmic-trading~~ | https://github.com/joelowj/awesome-algorithmic-trading | GitHub 列表 PR | **不建议** | 见 §2.4 |

---

## 2. GitHub awesome 列表（PR 提交）

### 2.1 awesome-quant（P0，最优先）

**URL**：https://github.com/wilsonfreitas/awesome-quant
**核实事实**：30.3k stars，最后提交 2026-10-19（周更级活跃），有正式 CONTRIBUTING.md，PR 必须过 `Validate PR` + `PR Review` 两个 CI，偏好一个项目一个 PR。

**提交位置**：README 的 `## Trading & Backtesting` 章节（CONTRIBUTING 明示：Python 回测类项目归此节）。

**PR 标题**：
```
Add PaperTrade-Agent-V2: strategy falsification engine
```

**PR 正文**：
```
Adds one project under `## Trading & Backtesting`.

- [PaperTrade-Agent-V2](https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2) - `Python` `CLI` - Strategy falsification engine: path-aware bar replay, cost sensitivity, barrier reachability, and a random-entry control group, with verdicts from pre-declared falsification rules.

Notes for the reviewer:
- Active repo: last commits 2026-10-28, MIT, 358 tests (60 in the verify suite).
- Distinct from backtesting frameworks: it doesn't produce equity curves, it tries to kill them. Verdicts (FALSIFIED / NOT_FALSIFIED / INCONCLUSIVE) come from `falsify_if` rules written into the spec before any number is read.
- Sample report on real cached Binance data and a 4-case matrix (including a +34% strategy ruled FALSIFIED because it can't beat random entry) are in the repo.
- I searched the existing README and open PRs first; no duplicate found.
```

**注意事项**：
- 条目格式必须**逐字符**匹配 `- [Name](url) - \`Tag\` - One sentence ending with a period.`，维护者的解析器靠正则，多个标签要各自独立反引号（`` `Python` `CLI` `` 而非 `` `Python, CLI` ``）
- PR 描述不能为空（空描述自动关闭）；先搜 README 和 open/closed PRs 防重复
- CI 挂了就改格式重推，不要 @ maintainer 催

### 2.2 awesome-systematic-trading（P1）

**URL**：https://github.com/paperswithbacktest/awesome-systematic-trading
**核实事实**：15.1k stars，2026-09-28 仍有提交 + 月度 link-check CI，条目按 star 数降序排在表格里，带 stars badge。

**提交位置**：`Libraries and packages` → `Backtesting and Live Trading`（若维护者认为更适合 `Analytics → Metrics computation`，听他们的——我们的工具本质是回测后的统计验证）。

**PR 标题**：
```
Add PaperTrade-Agent-V2 to Backtesting and Live Trading
```

**PR 正文**：
```
Adds one entry at the bottom of the Backtesting and Live Trading table (list is ordered by descending stars, and this repo is new):

[PaperTrade-Agent-V2](https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2) — Strategy falsification engine: path-aware bar replay (worst-case SL/TP precedence), cost sensitivity at 1x/2x/5x fees, barrier reachability, and a random-entry control group with bootstrap CIs. Verdicts from pre-declared `falsify_if` rules. MIT, Python.

It is not a backtesting framework — it's the verification layer that runs after one. Four published cases included, one of them a +34% strategy ruled FALSIFIED because it cannot be distinguished from random entry (CI spans zero). Repo active, last commit 2026-10-28.
```

**注意事项**：
- 表格条目带 `![GitHub stars](https://img.shields.io/github/stars/Zhuyuyangyy/PaperTrade-Agent-V2)` badge，star 少就排在章节末尾，正常，不是歧视
- 该仓库官方口径是"submit an issue with suggestions"也可，PR 更快；别同时发 issue 又发 PR

### 2.3 awesome-crypto-trading-agents（P3）

**URL**：https://github.com/shakeebshaan/awesome-crypto-trading-agents
**核实事实**：CC0 协议，贡献规则明确：分类内按字母序、格式 `owner/repo — 一句话`、要求"近 6 个月有提交""与加密或跨资产算法交易相关""非无实质改动的 fork"。本仓库 2026-10-28 仍有提交 ✓，覆盖 Binance BTC/ETH + A 股可转债（跨资产 ✓）。

**提交位置**：`Backtesting Engines` 分类。

**PR 标题**：
```
Add Zhuyuyangyy/PaperTrade-Agent-V2 to Backtesting Engines
```

**PR 正文**：
```
Alphabetical placement within Backtesting Engines:

Zhuyuyangyy/PaperTrade-Agent-V2 — Strategy falsification engine: path-aware bar replay, cost sensitivity, barrier reachability, and random-entry control, with verdicts from pre-declared falsification rules. MIT, Python, offline replay.

Meets the contributing criteria: open source with commits within the last 6 months (last commit 2026-10-28), applicable to crypto (Binance BTC/ETH futures) and cross-asset (A-share convertible bonds), not a fork.
```

**注意事项**：字母序插入（Z 开头，基本在末尾）；描述别带营销词，维护者明示要 "one-line description focused on what makes it distinctive"。

### 2.4 两个"不建议"（省下时间，也是专业判断）

- **botcrypto-io/awesome-crypto-trading-bots**（https://github.com/botcrypto-io/awesome-crypto-trading-bots，PR welcomed）：该列表分类是"开源 bot / TA 库 / 行情源 / API / 图表库"，**只收交易机器人**。本项目是验证层不是 bot，硬塞会被拒还耗维护者好感。跳过。
- **joelowj/awesome-algorithmic-trading**（https://github.com/joelowj/awesome-algorithmic-trading）：最后提交 2019-05-21，Projects 章节是空的，主仓库 1 star。曝光≈0，提交纯消耗。跳过。

---

## 3. Show HN（P0，一次机会，准备好再发）

**URL**：https://news.ycombinator.com/submit
**核实事实/惯例**：标题以 "Show HN:" 开头、≤80 字符；周二/周三美东上午发；**绝不求 upvote**；同产品不发第二次。HN 口味：技术深度 >  polished marketing，自嘲和诚实是加分项。

**标题**（76 字符）：
```
Show HN: I falsified my own trading system and open-sourced the instruments
```

**正文**（约 260 词）：

```
I built a six-agent paper-trading system (search → risk → smart-money → LLM judge → FOMO → exit, four parallel arms, WAL ledgers). Ran it for 36 hours. The measurement said no edge, so I stopped it — then kept the instrument that measured it.

What it found, all reproducible from the repo:

- Six agents fully on vs the dumbest rule ("buy the top 3 by organic_score"): -94.68 vs -102.75. The agent stack's incremental value was about $8.
- 14 filter queues, bootstrap CIs on lift: every single one crosses zero.
- Gross +18bps per trade against a 50bps LP fee floor — structurally negative, independent of pool depth.

Then I pointed the same instruments at published strategies. Four of them:

1. Path-aware replay. A bar containing both stop and target resolves to the stop; the optimistic read is printed alongside. One repo's own notes claim +469%/91% win rate from "did price touch TP"; path-aware replay of the same signals gives +132%/PF 1.62. Same signals, 3.5x apart.
2. Cost sensitivity. Replay at 1x/2x/5x the assumed fee. Measured: a $100 order into a median-$799k pool costs 60bps round trip, 50bps of it an LP floor.
3. Barrier reachability. Are your TP/SL levels prices the market actually visits? In one test case the -8% stop was touched in 4.9% of windows.
4. Random-entry control. Same window, same exits, same costs, entries drawn at random, bootstrap CI on the difference.

The case I care about: RSI momentum on BTC 1h, 1638 trades, +34.35% over three years — a publishable curve. The random-entry control in the same window differs by +0.03%/trade with a CI spanning zero. 2023-06 to 2026-10 was a crypto bull run; random entries made money too. That curve sells beta packaged as alpha. Verdict: FALSIFIED.

Verdicts come from `falsify_if` rules written into the spec before any number is read. MIT, 358 tests, offline replay, sample report on real cached Binance data in the repo.

No signals, no subscriptions, no managed capital. The only question the tool answers is whether your curve is real.
```

**注意事项**：
- 链接放正文最后两行（仓库 + 落地页），别用短链
- 发前 1-2 天在 HN 上正常评论几条，冷号直发容易被 kill
- 评论区大概率有人问"那你自己的 edge 是什么"——用 `reddit-algotrading.md` 里那段现成回答（"I'm better at killing strategies than at finding them"）
- 有人质疑 walk-forward/容量模型缺失：承认，README 里写了仪器边界，认账比辩护得分

---

## 4. 中文社区

### 4.1 掘金（P1）

**URL**：https://juejin.cn/editor/drafts/new（Markdown 编辑器）
**核实事实/规则**：原创首发、拒绝广告与无关引流、文字占比大于代码、标题建议 15-35 字、分类必选 1 个、标签 1-5 个。

**标题**：
```
我证伪了自己跑了 36 小时的六 Agent 交易系统，然后把仪器开源了
```

**正文**（技术复盘结构，项目作为"顺带产出"文末轻提）：

```
## 背景

做了一个六 agent 的纸面交易系统：search → risk → smart-money → LLM judge → FOMO → exit，
四个并行实验臂，六本独立 SQLite 账本（WAL 模式），看门狗、止损独立循环都配齐了。
跑了 36 小时，252 笔平仓。然后测量层告诉我：没有 edge。我把系统停了，仪器留下了。

## 仪器测出了什么

1. 六个 agent 全开 vs 最笨的规则（按 organic_score 买前 3）：-94.68 vs -102.75。
   六个 agent 的增量价值大约是 8 美元。
2. 14 个筛选队列，lift 的 95% 置信区间，全部跨 0。没有一个门证明过自己有选择力。
3. 每笔毛利 +18bps，LP 费地板 50bps。结构性为负，和池深无关。

## 然后我把仪器对准了公开策略

四个事前声明案例（统一 29,347 根 BTC/ETH 1h 真实 K 线，规则先于数字写死）：

| 案例 | 笔数 | 净收益 | vs 随机入场对照 | 判定 |
|---|---|---|---|---|
| RSI 抄底 BTC | 1310 | -505.65% | refuted (-0.37%) | FALSIFIED |
| RSI 追涨 BTC | 1638 | +34.35% | unproven (+0.03%) | FALSIFIED |
| RSI 抄底 ETH | 1419 | -1144.10% | refuted (-0.80%) | FALSIFIED |
| 抄底 BTC + maker 限价 | 1301 | -453.20% | refuted (-0.40%) | FALSIFIED |

第二行是重点。三年 +34%，曲线很能发朋友圈。但同一窗口随机入场，差值 +0.03%/笔，
CI 跨 0。2023-06 到 2026-10 是加密牛市，随机入场也赚钱。那条曲线卖的是 beta，包装成 alpha。

## 六件仪器 + 诊断层（都可以离线复现）

1. Path-aware 回放：逐根 K 线走价，同一根 K 线止损止盈同时触发默认判止损，乐观读法并列打印。
   CascadeFader 仓库自己记录：用"是否触及过止盈价"分析得 +469%/91% 胜率，路径回放同一批信号是
   +132%/PF 1.62。同一批信号，差 3.5 倍。
2. 成本敏感性：费率 ×1/×2/×5 重放。实测 $100 单进中位 $799k 池往返 60bps，其中 50bps 是
   LP 地板；配置里那个 120bps 其实是给 $1,000 单定的价——尺子用错了地方。
3. 障碍可达性：止盈/止损位在持仓窗口内到底被摸到过几次。某个测试里 -8% 止损只在 4.9% 的
   窗口被摸到——那个"止损"没有在工作。
4. 随机入场对照组：同窗口、同退出规则、同成本，随机 bar 入场，bootstrap CI 比均值。

## 顺手做成了产品

输入一份策略 spec，输出一份试图杀死它的报告。裁决不是分数，是事前证伪规则的判定结果：
falsify_if 先于任何数字写进 spec，样本不足判 INCONCLUSIVE 永不放过。
MIT，358 个测试，新克隆无行情缓存也能跑 demo。

仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
落地页（30 秒 demo + 案例矩阵）：https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/

不荐股、不带单、不承诺收益。只回答一个问题：你的曲线是不是幻觉。
```

**发布设置**：分类选「开发工具」或「人工智能」；标签 2-4 个：`开源`、`Python`、`量化交易`、`GitHub`；封面用落地页截图。
**注意事项**：
- 掘金对"金融/收益"关键词可能审核更严，正文已避开"稳赚/收益率承诺"类表述，发布前别再手滑加标题党
- 文字占比大于代码（本文代码块只有表格和命令，合规）
- 首发后 1 小时内回复评论区，掘金热度算法吃早期互动

### 4.2 知乎（P2）

**URL**：https://www.zhihu.com/creator（写文章）
**核实事实/惯例**：知乎对营销内容识别严格，但长文方法论 + 自嘲数据流在"量化/交易"话题下有真实受众；外链放文末。

**标题**：
```
跑了 36 小时、252 笔平仓之后，我证伪了自己六 Agent 交易系统
```

**正文**（文章体，比掘金版更"第一人称反思"）：

```
先说结论：这套系统没有 edge，我把它停了。但杀死它的仪器我留下了，还开源了。

## 一个六 agent 系统是怎么死的

search → risk → smart-money → LLM judge → FOMO → exit。每个 agent 都有完整的留痕、
看门狗、WAL 账本。工程上是认真的。36 小时后测量层的答案也是认真的：

- 六 agent 全开 -94.68，最笨规则（organic_score 买前 3）-102.75。六个 agent 的增量价值：约 8 美元。
- 14 个筛选队列，lift 的 95% 置信区间全部跨 0。没有一个门证明过自己有选择力。
- 每笔毛利 +18bps，LP 费地板 50bps。这个市场里看得见的钱，在成本对面。

## 真正让我后背发凉的是第四行数据

仪器不光杀自己，也杀别人。四个事前声明案例，统一 29,347 根真实 K 线，
证伪规则先于数字写死。三个是明显的输家。第四个：RSI 追涨 BTC，1638 笔，三年 +34.35%。

这条曲线是能拿去发朋友圈的。但同一窗口的随机入场对照组和它只差 +0.03%/笔，CI 跨 0。
2023-06 到 2026-10 是加密牛市——随机入场也赚钱。它不是 alpha，是 beta 化了妆。

行业数据同向：Broker Tested Reviews 用真钱测试发现 22 个零售 bot 里 17 个只是指数 beta，
回测与实盘中位差 -14.3pp。大家都在卖没人能验证的曲线。

## 所以我做了个专门杀曲线的东西

六件仪器 + 诊断层：

1. Path-aware 回放。逐根 K 线走价，一根 K 线里止损止盈同时出现默认判止损，乐观读法并列打印。
   某策略仓库自己记录"触及过止盈价"分析得 +469%/91% 胜率；同一批信号路径回放是 +132%/PF 1.62。
   差 3.5 倍。你赚的是价差还是假设，一眼看清。
2. 成本敏感性。费率 ×1/×2/×5 重放。edge 付不付得起自己的过路费，直接见分晓。
3. 障碍可达性。你的止损位，市场到底去不去？某个案例里 -8% 止损只在 4.9% 的窗口被摸到。
4. 随机入场对照组。同窗口同规则随机入场，bootstrap CI 比均值。打不过抛硬币的策略，
   绝对收益数字没有意义。

裁决来自事前写死的 falsify_if 规则：触发即出局，样本不足判 INCONCLUSIVE，永不放过。
NOT_FALSIFIED 也不等于被证实——报告固定带"未建模事项"一节，不知道仪器边界的人会滥用仪器。

## 它现在是个产品

输入一份策略 spec，输出一份试图杀死它的报告。MIT，358 个测试，离线可复现。
前 10 份免费，换匿名公开发布权——我想建公开案例库，不想吵架。
不荐股、不带单、不代管资金、不承诺收益。

仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
落地页：https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/

（文中所有数字都能用仓库里的命令复现。欢迎来证伪我的证伪。）
```

**注意事项**：
- 发布时勾选原创；话题选「量化交易」「Python」「开源」相关
- 知乎评论区常见"所以你能帮我预测吗"——统一回复口径：不能，这工具只判断你的曲线是不是幻觉
- 不要在文内放购买/服务链接（$99/$299 的事去落地页说），知乎对交易类变现内容敏感

### 4.3 V2EX（P2）

**URL**：https://www.v2ex.com/go/create（分享创造节点，个人项目首次发布）
**核实事实/规则**：官方明示"允许推广个人作品，但拒绝纯丢链接的硬广"，要讲开发故事/技术选型；发帖后 10 分钟内可自助改节点；商业软文归 /go/promotions（本项目是个人开源项目，走 create/isv 即可）。

**标题**：
```
花了 36 小时证明自己的交易系统没有 edge，顺手把证伪仪器开源了
```

**正文**：

```
背景：做了个六 agent 纸面交易系统（Solana memecoin 管线 + 四个实验臂 + 六本 SQLite 账本），
工程上自认认真：WAL、看门狗、止损独立循环、429 退避全有。跑了 36 小时，测量层说没有 edge，
我就把它停了——STOP 文件至今还在仓库里。

停之前仪器记下的东西（全部可复现）：

- 六 agent 全开 -94.68 vs 最笨规则（organic_score 买前 3）-102.75。增量价值约 8 美元。
- 14 个筛选队列，lift 的 95% CI 全部跨 0。没有一个门证明过自己有选择力。
- 每笔毛利 +18bps，LP 费地板 50bps。结构性为负。

然后把仪器对准外部策略，最扎心的一条：RSI 追涨 BTC，1638 笔，三年 +34.35%——
曲线很漂亮，但同窗口随机入场对照组只差 +0.03%/笔，CI 跨 0。牛市里随机入场也赚钱。
这条曲线卖的是 beta，包装成 alpha。判定：FALSIFIED。

六件仪器 + 诊断层（MIT，358 测试，离线可跑）：

1. Path-aware 回放：逐根 K 线走价，SL/TP 同现判 SL，乐观读法并列打印。某仓库自记
   +469%/91% 胜率，同批信号路径回放 +132%/PF 1.62。
2. 成本敏感性：费率 ×1/×2/×5 重放。
3. 障碍可达性：止盈/止损位，持仓窗口内到底被摸到过几次。
4. 随机入场对照组：bootstrap CI 比均值。

输入策略 spec，输出一份试图杀死它的报告。裁决来自事前写死的 falsify_if 规则。
不荐股、不带单、不承诺收益——只回答"你的曲线是不是幻觉"。

仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
落地页（30 秒 demo）：https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/

欢迎拍砖，尤其是仪器边界（没做 walk-forward 容量模型，一次只处理一条价格序列）。
```

**注意事项**：
- 节点选 /go/create；若更像技术讨论也可 /go/isv。发完 10 分钟内发现不对可自己移节点
- V2EX 用户对"AI 套壳"和营销腔极度敏感，本文全程自嘲 + 可复现数字，符合口味；别在回复里催 star
- 想先试效果可发 /go/sandbox（不上首页）看排版

### 4.4 LINUX DO（P2）

**URL**：https://linux.do/
**核实事实/规则**（官方 wiki）：纯开源且无间接引流的项目适用「公益推广」，**必须带 `公益推广` 标签，原则上每账号每周不超过 1 篇**；禁止在本社区引流至其他社区；发前先搜索避免重复帖；社区对营销腔和 AI 生成内容敏感，人工润色后再发。

**标题**：
```
【分享】一个试图"杀死"你自己交易策略的开源工具：从证伪自己的系统说起
```

**正文**：

```
长话短说：我做了个六 agent 纸面交易系统，跑了 36 小时，测出来没有 edge，停了。
仪器留下了，开源了。

测出来什么（仓库里全部可复现）：

- 六 agent 全开 -94.68 vs 最笨规则 -102.75，六个 agent 的增量价值约 8 美元
- 14 个筛选队列，lift 的 95% CI 全部跨 0
- 每笔毛利 +18bps vs LP 费地板 50bps，结构性为负

然后仪器对准外部策略。四个事前声明案例（29,347 根真实 1h K 线，规则先于数字写死），
三个是输家。第四个：RSI 追涨 BTC，1638 笔，三年 +34.35%。同窗口随机入场对照只差
+0.03%/笔，CI 跨 0——牛市里随机入场也赚钱，这条曲线是 beta 不是 alpha。判定 FALSIFIED。

六件仪器 + 诊断层：
1. Path-aware 回放（SL/TP 同现判 SL，乐观读法并列）：某仓库自记 +469%/91% 胜率，
   同批信号路径回放 +132%/PF 1.62
2. 成本敏感性 ×1/×2/×5
3. 障碍可达性（止损位市场到底去不去）
4. 随机入场对照组 + bootstrap CI

MIT，360 测试，离线可跑，新克隆无行情缓存也能跑 demo。

仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
落地页：https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/

不荐股、不带单、不承诺收益。只回答一个问题：你的曲线是不是幻觉。
```

**注意事项**：
- **必须打 `公益推广` 标签**（纯开源项目符合定义），否则按普通推广处理；本周若已发过推广帖就换下周
- 发帖前用站内搜索确认没有重复主题；分类选「资源荟萃」或「开发调优」
- 不要提"欢迎也关注我的其他平台"之类——禁止引流至其他社区；GitHub 链接是内容本身，没问题
- 社区对 AI 代写痕迹敏感，发布前自己过一遍改出口语

### 4.5 Ruby China（P3）

**URL**：https://ruby-china.org/（发帖入口：首页右上"发布新主题"）
**核实事实**：Ruby 社区，但有活跃的 Python 节点（/nodes/python）和「分享」「开源项目」节点；社区小、质量高、对硬核内容友好。

**标题**：
```
分享一个试图"杀死"交易策略的开源工具（Python）：从证伪自己的六 Agent 系统讲起
```

**正文**（直接投 Python 节点，开头一句说明来意）：

```
虽然是 Ruby 社区，这个项目是 Python 的，发在 Python 节点，版主觉得不合适可以移动。

一句话：我做了个六 agent 纸面交易系统，跑了 36 小时测出没有 edge，把它停了，
然后把杀死它的仪器开源了。

仪器记下的（全部可复现）：六 agent 全开 -94.68 vs 最笨规则 -102.75；14 个筛选队列
lift 的 95% CI 全部跨 0；毛利 +18bps vs LP 费地板 50bps。

最扎心的是对外部策略的审计：RSI 追涨 BTC 三年 +34.35%，曲线漂亮，但同窗口随机入场
对照只差 +0.03%/笔、CI 跨 0。牛市里随机入场也赚钱。判定 FALSIFIED——这是整个产品
想解决的问题：杀死那些你看不出和运气的区别的策略。

六件仪器 + 诊断层：path-aware 回放、成本敏感性 ×1/2/5、障碍可达性、随机入场对照组。
裁决来自事前写死的 falsify_if 规则。MIT，360 测试，离线可跑。

仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
落地页：https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/
```

**注意事项**：
- 节点选 Python 或「分享」；不要发「产品推广」节点（那是给商业产品的）
- 社区规模小，预期是个位数的 star；价值在于反馈质量高，适合收集仪器边界的批评

---

## 5. Reddit（r/algotrading 之外）

> 通用纪律（三份共用）：账号先有几天正常评论历史；**不同天发、错开**，同内容短时间多版块投放是 spam 的典型姿势；链接放评论区或正文末尾；美东上午发（北京 21:00-23:00）；遵循 9:1——每发 1 个推广帖，先有约 9 次真实评论。

### 5.1 r/quant（P2）

**URL**：https://www.reddit.com/r/quant/
**核实事实/规则**：20.8 万成员；版规要求"内容须与量化行业/职业量化分析师相关，不能是零售向（零售的去看 r/algotrading）"；允许自我推广但反对重复推广同一实体；技术指标类不算量化分析。

**标题**：
```
Open-source strategy falsification engine: path-aware replay, cost sensitivity, random-entry control
```

**正文**（方法论优先，强调行业相关性——回测验证是 model risk 的一部分）：

```
Sharing a tool I built after measuring my own system and finding no edge. The angle is model risk: most published backtests are unfalsifiable by construction, so I built instruments that try to falsify them.

The four instruments (all offline-replayable, MIT, 358 tests):

1. Path-aware replay. Bars are walked, not sampled; a bar containing both stop and target resolves to the stop, optimistic read printed alongside. One repo's own notes claim +469%/91% win from "did price touch TP"; the same signals under path-aware replay give +132%/PF 1.62. Same signals, 3.5x apart.
2. Cost sensitivity. Replay at 1x/2x/5x the assumed fee. Measured here: $100 order into a median-$799k pool costs 60bps round trip, 50bps of it an LP floor.
3. Barrier reachability. Are your TP/SL levels prices the market actually visits? In one test case the -8% stop was touched in 4.9% of windows.
4. Random-entry control. Same window, same exits, same costs, random entries, bootstrap CI on the difference.

Verdicts come from `falsify_if` rules written into the spec before any number is read; insufficient sample returns INCONCLUSIVE rather than a pass.

The case I'd highlight for this audience: RSI momentum BTC 1h, 1638 trades, +34.35% over three years. Random-entry control in the same window differs by +0.03%/trade, CI spans zero. In a bull run, random entries make money too. Published as FALSIFIED anyway.

Happy to discuss the methodology, especially its weakest points (no walk-forward capacity modelling, one price series at a time).

Repo: https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
```

**注意事项**：
- 开头就把框架挂到 model risk / backtest validation 上，避免被当成"零售玩具"；别提"我做了个交易机器人"
- 别贴收益曲线图——这里是 r/quant，贴曲线等于自曝零售向

### 5.2 r/Python（P3）

**URL**：https://www.reddit.com/r/Python/
**核实事实/规则**：150 万成员；项目帖**必须用 showcase flair 且必须是文字帖**；必须包含"项目做什么 / 目标受众 / 与现有方案的对比"等段落；有最低 karma 与账号时长门槛；对纯 AI 套壳 showcase 已收紧到月度集中帖。本项目是统计验证工具不是 AI 套壳，走"I Made This"类 showcase flair。

**标题**：
```
I built a Python CLI that tries to falsify trading strategies (random-entry control, bootstrap CIs, path-aware replay)
```

**正文**（按版规要求的段落结构）：

```
**What it does**

Input a strategy spec (market, timeframe, signal rule, exit rule, falsification rules), get back a report that tries to kill the strategy. Four instruments: path-aware bar replay (worst-case SL/TP precedence, optimistic read printed alongside), cost sensitivity at 1x/2x/5x fees, barrier reachability (are your TP/SL prices the market actually visits?), and a random-entry control group with bootstrap CIs. Verdicts are FALSIFIED / NOT_FALSIFIED / INCONCLUSIVE, decided by `falsify_if` rules declared before any number is read.

**Target audience**

Anyone with a backtest they can't fully trust — including my past self. It's a CLI, offline, MIT, 358 tests. `python -m research.verify --demo` runs on bundled real BTC data with no API keys.

**Comparison**

Backtesting frameworks (backtrader, vectorbt, zipline) answer "what would this strategy have returned?" — they produce curves. This produces a verdict on whether the curve means anything, and it's framework-agnostic: you can feed it signals from any source as CSV.

**Why I built it**

I ran a six-agent paper-trading system for 36 hours, measured no edge, killed it, and kept the instrument. The number that motivated the tool: a strategy making +34.35% over three years (1638 trades) that a random-entry control in the same window could not be distinguished from (+0.03%/trade difference, CI spanning zero).

Code: https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
Landing page with a 30-second demo and the 4-case matrix: https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/

Feedback welcome, especially on the parts that are weakest (no walk-forward capacity modelling yet).
```

**注意事项**：
- flair 选 "I Made This"/showcase 类；文字帖，图可以嵌在正文里
- 若帖被删：新版规对 showcase 有口径变化，备选是当月的 showcase 集中帖，去那里发同一份
- 门槛：约 100 karma + 30 天账号（不够就先评论攒）

### 5.3 r/quantfinance（P3）

**URL**：https://www.reddit.com/r/quantfinance/
**核实事实**：2010 年创建的量化金融社区，定位介于 r/quant 与 r/algotrading 之间，欢迎分享论文、思路和自己写的软件；流量小于 r/quant，规则相对宽松。

**标题**：
```
A tool that tries to kill backtests: path-aware replay + random-entry control (open source)
```

**正文**（短版本，方法论一段带过）：

```
After measuring my own trading system and finding no edge, I open-sourced the instruments that measured it. The core idea: a backtest you can't falsify is a story, not a result.

Four instruments: path-aware bar replay (a bar containing both stop and target resolves to the stop), cost sensitivity at 1x/2x/5x fees, barrier reachability, and a random-entry control group with bootstrap CIs on the difference. Verdicts come from `falsify_if` rules declared before any number is read.

The case that says it best: +34.35% over three years, 1638 trades — and a random-entry control in the same window differs by +0.03%/trade with a CI spanning zero. Published as FALSIFIED.

MIT, Python, 358 tests, offline replay: https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
```

**注意事项**：社区小，预期低；这里可以更学术口吻，有人讨论 multiple-testing / Bonferroni 一类问题就认真回——那是免费的方法论曝光。

---

## 6. 产品目录

### 6.1 Product Hunt（P3）

**URL**：https://www.producthunt.com/launch
**核实事实/规则**：用**个人账号**提交；tagline ≤60 字符、描述 ≤260 字符、topics 最多 3 个；发布锁定 12:01 AM PT；**绝不公开求 upvote**（违规可被取消资格）；每个产品只有一次好 launch；周末 launch 的"Visit"点击高 15%。

**Name**：PaperTrade-Agent-V2
**Tagline**（50 字符）：
```
Instruments that try to kill your trading strategy
```
**Description**（≤260 字符）：
```
Open-source engine that falsifies trading strategies: path-aware bar replay, cost sensitivity at 1x/2x/5x, barrier reachability, and a random-entry control group. Verdicts come from falsification rules declared before any number is read.
```
**Topics**：Developer Tools · Open Source · Fintech

**Maker comment（发布后第一条，自己发）**：
```
Hi PH — I built this after running a six-agent paper-trading system for 36 hours, measuring that it had no edge, and killing it. Then I kept the instrument.

What it answers: not "what would this strategy have returned", but "is that return distinguishable from luck". Four instruments — path-aware replay (worst-case SL/TP precedence), cost sensitivity, barrier reachability, random-entry control with bootstrap CIs. Verdicts come from `falsify_if` rules written before any number is read.

The case I'd point to: RSI momentum BTC 1h, 1638 trades, +34.35% over three years — ruled FALSIFIED, because a random-entry control in the same window differs by +0.03%/trade with a CI spanning zero. In a bull run, random entries make money too.

MIT, 358 tests, offline replay, demo runs on bundled real data with no API keys. First 10 falsification reports free in exchange for publication rights — no signals, no subscriptions, no managed capital.

Happy to answer anything, especially where the methodology is weakest (no walk-forward capacity modelling yet).
```

**注意事项**：
- 画廊至少 2 张图：落地页的 demo 截图 + 案例矩阵截图（1270×760）
- 适配度说实话一般：PH 受众是 maker/SaaS，本项目是开源 dev tool；期望值定为"一条高质量外链 + 首批用户"，不是 viral
- 发布日上午在线回评论；别@朋友来点赞

### 6.2 AlternativeTo（P3）

**URL**：https://alternativeto.net/（登录后走站点自己的 "Add application" 流程，无固定提交链接）
**核实事实/规则**：需免费账号；描述**不得含网址、邮箱、地址**；免费队列积压严重（数千个应用排队），付费加急 1-2 工作日；社区驱动，排序吃用户投票。

**Name**：PaperTrade-Agent-V2
**Description**（无链接版）：
```
Open-source strategy falsification engine for trading strategies. Runs path-aware bar replay (worst-case stop/target precedence), cost sensitivity at 1x/2x/5x fees, barrier reachability checks, and a random-entry control group with bootstrap confidence intervals. Verdicts come from falsification rules declared before any number is read. MIT licensed, Python, offline replay, 358 tests.
```
**Category/Tags**：Development · Trading / Backtesting
**Suggested "alternative to"**：backtrader、vectorbt、zipline（定位写清：不是替代回测框架，是跑在回测之后的验证层）

**注意事项**：
- 期望值放到最低：这是长尾 SEO，不是流量入口；免费队列可能几个月不排到
- 描述里一个字链接都不能有（他们的硬规则），平台/仓库名写进描述即可

### 6.3 GitHub Topics（P0，零成本，今天就能做）

**URL/入口**：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2 → 首页右侧 About 栏齿轮 → Topics（或 Settings → 仓库详情）
**作用**：topic 会进入 `https://github.com/topics/<name>` 聚合页，是站内长尾发现入口；同时也是别人判断"这仓库是什么"的第一个信号。

**建议 topics**（8 个，别贪多）：
```
algorithmic-trading
quantitative-finance
backtesting
trading-strategies
paper-trading
research-tools
falsification
python
```

**同时建议改 About 描述**（现描述 "Six-agent paper trading desk... Paper only." 讲的是已死系统的前世，新访客 3 秒内看不出这是产品）：

```
Strategy falsification engine: path-aware replay, cost sensitivity, and random-entry control with pre-declared verdicts. MIT, offline, 358 tests. (Archive of a 6-agent paper-trading experiment that measured no edge.)
```

Website 栏填：`https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/`

**注意事项**：
- 这是网页设置，不是 git 操作，本人 10 分钟搞定
- topic 别造不存在的新词（没人订阅=没有流量）；上面 8 个都是存量 topic

---

## 7. 优先级结论与理由

**最优先（P0，三天内做完）**：

1. **GitHub Topics** —— 10 分钟，零风险，永久生效。stars 的信任货币先从"访客看得懂这是什么"开始。现 About 描述还在讲已停止的实验系统，每个新访客都在流失。
2. **Show HN** —— 单次曝光上限最高的渠道，且 HN 口味和本项目气质（诚实、自嘲、数据密集、承认边界）天然匹配。一次机会，文案已按 ≤300 词写好，发前养号 1-2 天。
3. **awesome-quant PR** —— 30.3k 星、周更活跃、贡献指南清晰到正则级别、`Trading & Backtesting` 章节与本项目是精确匹配（不是回测框架，是验证层，正好填列表空白）。PR 过两个 CI 就能进，是被动流量入口。

**其次（P1，第一周）**：awesome-systematic-trading（15.1k 星活跃维护，但按 star 降序会沉底，占位为主）；r/algotrading（文案已就绪）；掘金（中文技术流量主入口，长文复盘结构合规）。

**然后（P2，第二周）**：V2EX、知乎、r/quant、LINUX DO——中文社区和 quant 向 Reddit 错峰发，养号与互动成本高于 P0/P1。

**最后（P3，有余力再说）**：r/Python（版规最严、门槛最高）、Ruby China（社区小）、awesome-crypto-trading-agents（列表新、CC0、要求明确，条目成本低可以顺手发）、Product Hunt、r/quantfinance、AlternativeTo。

**不建议**：botcrypto-io/awesome-crypto-trading-bots（只收 bot，本项目不是，硬塞被拒还耗好感）、joelowj/awesome-algorithmic-trading（2019 年后死亡，曝光≈0）。

**为什么不按"流量大小"排**：r/Python 150 万人但版规严、转化低；awesome-quant 3 万人但每个访客都是目标读者。信任货币的汇率比面值重要——**来的都是"会验证回测的人"，一个顶一百个看热闹的**。

## 8. 通用纪律（所有渠道适用）

1. **同一套数字**：所有帖子只用 §0 口径表里的数，一个新数字都别加；加之前先在仓库里复现一遍
2. **不承诺效果**：所有文案不出现"提升 star 保证""流量保证"式表述；服务口径统一为"前 10 免费换授权 / $99 / $299 / 不荐股不带单不代管资金"
3. **错峰**：Reddit 三篇分三个不同天；中文社区与 Reddit 不同周；同一账号不要在短时间内跨版块发同内容
4. **养号**：HN/Reddit/LINUX DO 发前先有几天真实评论历史；r/Python 注意 karma 与账号时长门槛
5. **不买 star、不互赞圈**：star 是信任货币，刷它等于印假钞，被 GitHub 或社区识别的成本远高于收益
6. **发完记账**：每个渠道发帖后，在 `win-log.md` 的来源口径里加一行观察记录（哪天、哪个渠道、仓库 traffic 页 referrer 变化），一周后按 gate 标准决定主攻方向
7. **回复口径**：被问"能帮我预测/带单吗"——不能，只回答"你的曲线是不是幻觉"；被问仪器边界——认账（无 walk-forward 容量模型、一次一条价格序列），认账比辩护得分
