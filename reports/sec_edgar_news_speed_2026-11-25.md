# SEC 8-K 作为一手新闻源：实测验证

日期 2026-11-25｜只读公开数据｜**未碰实盘、未下任何订单**

## 结论先行

对美股公司事件（以黄仁勋复职为例），**SEC EDGAR 的 8-K 是比 X 快讯号更快、更硬、且免费的源**。

- 8-K 被 SEC 接受的精确时刻：**2026-11-23 13:57:54 UTC**（美东 08:57:54 EST / 北京 21:57:54）
- 财联社次日汇总稿发布时间：**2026-11-24 02:20 UTC**（北京 10:20）
- **媒体滞后 742.1 分钟 = 12.37 小时**（实测，不是估计）
- X 上马斯克的回应发生在"得知董事会决定后"（各家媒体一致措辞），即在 8-K 公开之后；
  但 **X 帖子的精确时间戳拿不到**（需付费 API），所以"比 X 快几分钟"无法量化，不外推。

## 一、实测时间线（全部来自真实 API / 页面）

| 时间（UTC） | 事件 | 来源 | 精度 |
|---|---|---|---|
| 2026-11-22（当日） | 董事会一致决议复职，"effective immediately" | 8-K 原文 Item 5.02 | 仅天 |
| **2026-11-23 13:57:54** | **8-K + 新闻稿同时被 SEC 接受** | EDGAR `acceptanceDateTime` | **秒** |
| 2026-11-23 美东白天 | 马斯克转发声明："The mistake has been corrected" | 媒体转述 X | 无精确戳 |
| 2026-11-23 美东白天 | 黄仁勋回到总部；X 简介改回 "CEO of NVIDIA" | 中央社/中时 | 无精确戳 |
| **2026-11-24 02:20** | 财联社汇总稿 | 页面发布时间 | 分钟 |

8-K 原始记录：

| 字段 | 值 |
|---|---|
| 公司 | NVIDIA CORP，CIK 0001045810 |
| accession | 0001045810-26-000090 |
| reportDate | 2026-11-22 |
| filingDate | 2026-11-23 |
| **acceptanceDateTime** | **2026-11-23T13:57:54.000Z** |
| Items | 5.02（高管变动）, 9.01 |
| 原文 | `nvda-20261122.htm` |
| 新闻稿 | `nov23pressrelease.htm`（同一批次 08:57:54 EST 上传） |

对照 11 月全部 8-K（三次，都在美东开盘前后披露）：

| reportDate | 公开时刻（UTC） | items | 事件 |
|---|---|---|---|
| 11-11 | 11-12 13:52:41 | 5.02 | 辞任 CEO |
| 11-17 | 11-17 21:20:51 | 2.02 | 业绩发布 |
| 11-22 | 11-23 13:57:54 | 5.02 | 复职 |

**规律**：5.02（高管变动）两次都在 13:5x UTC ≈ 美东 08:5x EST 披露，正好是开盘前窗口。
这个时间规律对择时有用——已知"高管变动类 8-K 倾向于盘前发布"，可以针对性轮询。

## 二、最重要的工程点：两个时间不能混

这次验证产生的最大价值不是"谁更快"，而是发现了一个会直接毁掉回测的陷阱：

| 字段 | 含义 | 精度 | 能不能用于择时 |
|---|---|---|---|
| `reportDate` / `occurred_at_ms` | 董事会决议日（11/22） | **只有天** | **不能** |
| `acceptanceDateTime` / `first_seen_ms` | SEC 接受时刻（11/23 13:57:54Z） | **秒** | **这才是市场可知时刻** |

如果把 11/22 当成事件可知时刻，事件会提前**一整天**进入特征——教科书级前视泄漏。
适配器里从数据结构上堵死：`occurred_at_ms` 只作事实字段，`first_seen_ms` 才是择时字段，
并有专门测试（`test_news_event_separation_prevents_lookahead`）守着。

## 三、可复用模块

`research/intel/sec_edgar.py`：

```python
from research.intel.sec_edgar import (
    CIK_MAP, fetch_submissions, extract_8k, to_news_events, latency_ms)

p = fetch_submissions(CIK_MAP["NVDA"])          # 免费，带 User-Agent
fs = extract_8k(p, since="2026-11-01")          # 精确匹配 8-K，排除 8-K/A 修订
events = to_news_events(fs, symbol="NVDA")      # → NewsEvent，advisory_only=True
```

输出契约沿用 `research/intel/contracts.py` 的 `NewsEvent`：
- `credibility = 1.0`（法定披露最高）
- `direction = "neutral"`（**不替模型判断利好利空**）
- `advisory_only = True`（默认只入日志/复盘，不改仓位）
- `tags` 带 item 编号，可按 `5.02 / 2.02` 过滤

**踩坑**：第一版用 `form.startswith("8-K")` 过滤，把 `8-K/A` 修订版也算进来了——
同一事件会双计。已改成精确匹配，并写进测试。

测试：`tests/intel/test_sec_edgar.py`，10 个用例，全部离线（injected fetch + 缓存命中断言），
不依赖网络。

## 四、tanggestock 的收费边界（顺带确认）

| 层 | 内容 |
|---|---|
| Observer（免费） | 分钟~季度 K 线、标准指标、成交量峰值、**500 档盘口**、BTC 大单墙、指标编写与回测、10 万 paper USDT |
| Open API sandbox | 免费，但**仅合成数据** |
| Open API 真实数据 | 需单独申请 |
| Trader / Strategist | 官方指标、高级数据、告警与自动通知 |

站点明确写着「学生、教师、研究者可申请 Growth Support」——在校生身份可以直接申请。
**结论：先申请这个；API 不是必需的，免费层的盘口和大单墙对突破策略已经够用。**

## 五、X 快讯源清单（作交叉验证，不作首选）

| 账号 | 定位 | 风险 |
|---|---|---|
| @DeItaone（Walter Bloomberg） | 7×24 短标题，最快 | **不是彭博官方**，聚合转述，必须回原源 |
| @FirstSquawk | 转专业 squawk | 同上 |
| @Unusual_Whales | 异常期权流，常先于新闻 | 领先指标，非事实源 |
| @KobeissiLetter | 宏观快讯+解读 | 带观点 |
| @ReutersBiz / @business | 官方媒体号 | 慢一层但可验证 |

要接的话走 RSS 桥接公开时间线，**不要买 X API**——先验证它到底比 8-K 快多少。
按这次事件看，它并不更快。

## 六、对 OpenAlpha 的落地建议

| 优先级 | 动作 | 现状 |
|---|---|---|
| 1 | SEC EDGAR 接进 intel，NVDA 先跑 | 模块已完成，10 测试通过 |
| 2 | 加密资产走官方公告/IR 的 RSS，不碰 SEC | BTC 没有 8-K 等价物 |
| 3 | tanggestock 申请 Growth Support | 未申请，零成本可试 |
| 4 | X 快讯号先只观察不接 | 付费且这次无明显速度优势 |

现有 intel 日志里的 `GAMMA_FAILED:URLError` 和 `WALLET_SOURCE_UNAVAILABLE` 是另外两条线的故障，
本次没有处理，也不影响这个适配器。

## 七、边界

- 全程只读公开数据；未登录交易账户、未下订单、未接 Demo 自动交易。
- 未合并 PR #26。
- 8-K 内容**不做方向判断**（`direction=neutral`）——公告本身不是交易信号，
  要变成信号必须走样本外增量验证，沿用 `advisory_only` 的既定验收标准。
- "SEC 比 X 快多少"**无法量化**：X 帖子精确时间戳需要付费 API。本报告只陈述可验证的部分。
