# 封存声明 · 发帖终稿（与 main 逐字对齐）

> 生成时间：2026-11-26
> 对齐基准：`main` @ `48fcf53`，README 徽章 `1058 passed, 2 skipped`
> 仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
> **本文件所有数字均来自 `main` 分支 README 与 `research/` 下报告，可直接引用。**

---

## 一、6 处措辞修改对照（改前 → 改后）

| # | 位置 | 改前（公信力打折版） | 改后（精确版） | 为什么这么改 |
|---|---|---|---|---|
| 1 | 测试数 | `409 passed` | **`1058 passed, 2 skipped`** | 徽章已随 main 更新，帖子必须同口径，否则点进去对不上 |
| 2 | 路线总数口径 | 「15 条路线全部 FALSIFIED」 | **「15 条路线、0 条通过：11 条 FALSIFIED + 4 条样本不足/无法拒绝零假设」** | 「全部证伪」是错的。#7/#8 是 INCONCLUSIVE，#13/#15 无法拒绝零假设 |
| 3 | 未通过内部划分 | 未区分，笼统带过 | **「2 条 INCONCLUSIVE（样本不足）+ 2 条无法拒绝零假设」** | 精确到条，读者可逐条去仓库核对 |
| 4 | 证伪理由 | 「净利为负，所以不行」 | **「有效区间净 +673bps，但 5 个窗口 Wilcoxon p=0.8125，无法拒绝零假设」** | 原来的说法会被 +673bps 打脸；说清「样本不够」比说「亏钱」更站得住 |
| 5 | 校准结论 | 「校准有错价」/「完全有效」 | **「整体偏离 −1.0pp / −0.75pp，Bonferroni 阈值 0.0056，最小 p=0.0497，无一通过」** | 给出数字和检验方法，而不是形容词 |
| 6 | 封存范围 | 「不做量化了」 | **「封存的是『免费公开 API + 散户费率 + 脚本自动跑』这一阶段；M2–M5 不启动」** | 封的是方法边界，不是能力上限。留了三条重启红线的余地 |

**核心原则**：把 4 条「没攒够样本」如实说出来，比声称「15 条全灭」更可信——因为任何人都能去仓库里数，数出 4 条不是 FALSIFIED，整篇的可信度就全塌了。

---

## 二、终稿正文（可直接发）

### 中文版（知乎 / 掘金 / 公众号通用）

**我把 15 条量化路线全测了一遍，0 条通过。然后我封存了这个阶段。**

仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
（`main` 分支，README 里有一张 15 条路线的证伪总表，每个数字都能用仓库内命令重跑）

---

**先说结论，说精确一点：15 条路线、0 条通过。其中 11 条被一手数据证伪，4 条卡在样本不足或无法拒绝零假设。**

那 4 条我没算进「证伪」，因为不算。它们不是「还有希望」，是「连证伪所需的样本都没攒够」——同样不足以支撑任何一分钱投入。

**Crypto 侧 8 条：**

| 路线 | 实测 | 判定 |
|---|---|---|
| 现货-永续 funding carry | 年化 3.77%（门槛 5%），样本外 0 笔 | FALSIFIED |
| Top-12 动态池截面动量 | 样本外 A −5.42% / B +5.92%；2024 +66% → 2026 −7.9% | FALSIFIED |
| 离散度 FLAT 门控 | 门控 OOS −0.026 vs 无门控 +24.31% | FALSIFIED |
| 日线 vol-target + 风险平价 | +6.87%/夏普 0.366，被静态基准 +11.92%/0.500 全面压制 | FALSIFIED |
| 15m 教科书三策略 | 毛 −0.99/+0.31/+1.05bps，成本占 ATR 中位 87.9% | FALSIFIED |
| V4 S2（BTC 4h 挤压突破） | 参数网格 1/9 为正 | FALSIFIED |
| V4 S2b（BTC 1h，F/N 双契约） | 样本内 +3.91/+7.33% → 样本外 −5.58/−7.31% | INCONCLUSIVE |
| V4 S1（拥挤清算反转） | OI 数据不可得，无法执行 | INCONCLUSIVE |

**Polymarket 侧 7 条：**

| 路线 | 实测 | 判定 |
|---|---|---|
| 静态完备集套利 | `sum_ask=0.999`，逐档吃下 −158.7bps | FALSIFIED |
| 二元合成 `ask(Y)+ask(N)<1` | 最低 1.001，正好一个 tick | FALSIFIED |
| 尾盘 0.95+ 折价 | 兑现 96.06% vs 均价 96.15% → −48.3bps | FALSIFIED |
| 毫秒级延迟套利 | 37.7 万条推送，`sum_ask` 最低 1.0010 | FALSIFIED |
| BTC 5m 抢延迟 | 有效区间 +673bps，但 Wilcoxon p=0.8125 | 无法拒绝零假设 |
| 气象 METAR 抢先 | 跳变 p50 −12s，高分歧 −18s | FALSIFIED |
| M1' 概率校准 | 整体偏离 −1.0pp / −0.75pp，Bonferroni 全不通过 | 无法拒绝零假设 |

---

**这 15 条撞的是同一堵墙，三块砖：**

**一、费率地板。** 15m 往返 24bps，要吃掉的是一根 K 线平均波幅（ATR 中位 27.3bps）的 **87.9%**。Polymarket 在 p=0.50 处 taker 费率是名义 **2.5%**，而一个最小报价单位的套利空间连它的 1/5 都不够。这些费率结构就是为筛掉散户设计的。

**二、信息同刻定价。** 气象线最干净：价格在源方入库前后 **2 秒内**从 0.52 跳到 0.995。BTC 5m 更彻底——结算走 Chainlink **TWAP-60s 均值**，一个 5bps 瞬时跳动对 5 分钟均值的贡献是 `5bps × 1/300 ≈ 0.017bps`，远小于 1 tick。**盘口不动不是市场迟钝，是数学上正确的。**

**三、样本量的物理上限。** Polymarket 0.95–0.96 桶实测 97.66% 对均价 95.34%，按 Wilson 下界要过关需要 **310 个**样本，我只有 171 个。而已结算市场里落在这个桶的本来就只有 171 个——攒够 310 要再等 1–2 年。这不是「再跑几天」，是产出速度的硬约束。

---

**所以我停在这里。** 封存的是「免费公开 API + 普通散户费率 + 写脚本自动跑」这一整段，M2 云节点 / M3 热路径 FOK / M4 极小资金 / M5 放量全部不启动，也不再开第 16 条免费数据探测线。

全仓只留 1 个零维护火种：`research/live/funding_shadow.py`，周频、只读、零资金、不盯盘。留着它只为一件事——Phase 1 说 funding 溢价在枯竭（2024 年 12% → 2026 年 2.7%），枯竭到什么程度、会不会回流，只有持续记录才答得上来。

**重启量化的唯一红线**，三条拿到任意一条才重启：
1. 别人拿不到的专有/一手数据
2. 结构性通道壁垒（跨平台合规资金通道）
3. 自己具备深度认知的垂直领域建模

共同点是：**都不是「写个脚本自动跑」能获得的**。这正是这 15 条全部失败的根本原因——它们都在「脚本可得」的范围内竞争，而那个范围里的钱已经被收干净了。

---

**最后说一句这个仓库真正留下的东西。** 它不是一堆失败的策略，是一套**能杀死策略的仪器**：path-aware 回放（最坏/最好并列）、1x/2x/5x 成本敏感性、障碍可达性、随机入场对照组（bootstrap 置信区间）、walk-forward、参数网格，加上事前写死的 `falsify_if` 规则——裁决先于任何数字存在。

全仓 **1058 个测试**守着这套规则。引擎 MIT 开源，离线可跑，新克隆没有行情缓存也能跑 demo：

```bash
python -m research.verify --demo
```

**你的回测曲线，敢过一遍对照组吗？** 前 3 单免费，换匿名公开发布权。不荐股、不带单、不代管资金、不承诺收益——只回答一个问题：**你的曲线是不是幻觉**。

---

### 短版（X / Twitter）

> 15 条量化路线，0 条通过。
>
> 11 条被数据直接证伪，4 条卡在样本不足或无法拒绝零假设。
> 我把它写成了仓库里的一张总表，每个数字都能用仓库内命令重跑。
>
> 三堵墙：费率地板（15m 往返成本吃掉 87.9% 的 ATR）、信息同刻定价（METAR 价格在源方入库 ±2s 内跳完）、样本量物理上限（过 Wilson 下界要 310 个样本，只有 171 个）。
>
> 所以封存。封的是「免费 API + 散户费率 + 脚本自动跑」，不是能力上限。
> 重启只要三样之一：专有数据 / 通道壁垒 / 垂直深度建模。
>
> 留下的是一个能杀死策略的开源仪器（MIT，1058 个测试，离线可跑）：
> `python -m research.verify --demo`
>
> 你的曲线，敢过一遍对照组吗？

---

### 英文版（Reddit / r/algotrading）

> **I tested 15 quantitative routes. Zero passed. Then I archived the whole phase.**
>
> To be precise: 15 routes, 0 passed — **11 FALSIFIED by first-hand data, 4 stuck at insufficient sample or "cannot reject the null."**
>
> I did *not* count those 4 as falsified. They aren't "maybe there's hope" — they're "I couldn't even gather enough data to falsify them," which is equally disqualifying for putting money in.
>
> Crypto (8): funding carry 3.77% annualized vs 5% bar, zero trades out-of-sample; cross-sectional momentum +66% in 2024 → −7.9% in 2026; dispersion gating that cut the 65 most profitable weeks; vol-target beaten by a static benchmark; 15m textbook strategies whose gross edge is ~1bp against 24bp round-trip cost (87.9% of median ATR); a 4h breakout that was positive in only 1 of 9 parameter cells; two 1h variants positive in-sample and negative out-of-sample; one that couldn't even get OI data.
>
> Polymarket (7): static arbitrage (best `sum_ask` 0.999 → −158.7bps after walking the book); binary synthesis floored at 1.001 (exactly one tick); late-stage 0.95+ discount (96.06% realized vs 96.15% average price → −48.3bps); millisecond latency arb (377k pushes, `sum_ask` never below 1.0010); BTC 5m (+673bps in the valid range but Wilcoxon p=0.8125); METAR weather (price jumps p50 = −12s relative to source receipt — the market prices it *before or at the same instant* I can read it); probability calibration (overall deviation −1.0pp / −0.75pp, Bonferroni kills every bucket).
>
> **Same three walls every time:**
> 1. **Fee floor** — 24bps round-trip eats 87.9% of a median 15m ATR; Polymarket's taker fee at p=0.50 is a nominal 2.5%, and one tick of arbitrage covers less than a fifth of it.
> 2. **Simultaneous repricing** — weather prices move 0.52 → 0.995 within ~2 seconds of the source publishing. BTC 5m settles on a Chainlink TWAP-60s, so a 5bps tick contributes `5bps × 1/300 ≈ 0.017bps`. The book not moving isn't slowness — it's *mathematically correct*.
> 3. **Sample-size physics** — the 0.95–0.96 bucket needs 310 samples to clear a Wilson lower bound; I have 171, and that's *all* such markets that exist right now. Waiting longer isn't a fix.
>
> So I'm archiving: no cloud nodes, no hot-path FOK, no micro-capital probe, no scaling. One zero-maintenance read-only weekly shadow script stays alive for funding-rate monitoring only.
>
> **I'd only restart with one of three things:** proprietary/first-hand data, a structural channel advantage, or genuine domain depth where the edge is modeling rather than latency. Note what they have in common — none of them are obtainable by "just write a script."
>
> What survives is the instrument, not the strategies: path-aware replay (worst/best case side by side), 1x/2x/5x cost stress, barrier reachability, random-entry controls with bootstrap CIs, walk-forward, parameter grids, and pre-declared `falsify_if` rules that fire before any number is read.
>
> MIT, 1058 tests, fully offline:
> ```
> python -m research.verify --demo
> ```
> **Would your backtest survive a control group?**

---

## 三、发帖前 60 秒自检

| 检查项 | 怎么验 | 期望 |
|---|---|---|
| 徽章数字 | 打开仓库主页看徽章 | `1058 passed, 2 skipped` |
| 总表存在 | 主页搜「15 条路线，0 条通过」 | 命中 |
| 划分口径 | 总表下方「精确口径」一行 | `11 条 FALSIFIED + 4 条未通过` |
| demo 能跑 | `python -m research.verify --demo` | 30 秒出报告 |
| 链接不 404 | 点仓库地址 | 200，默认分支 `main` |

**不要写的三句话**（写了会被抓）：
- ❌「15 条全部证伪」→ 实际 11 + 4
- ❌「409 个测试」→ 实际 1058
- ❌「找到了 edge 但被费率吃掉」→ 一条 edge 都没找到，别暗示差一点