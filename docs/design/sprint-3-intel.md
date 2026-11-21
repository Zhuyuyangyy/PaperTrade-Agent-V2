# Sprint 3 — 情报层（News / Polymarket / Smart Money）

> 状态：**IMPLEMENTED / ADVISORY-ONLY**。三条研究线都只产出结构化事件与评估结论，
> 默认 **不改变** `Selection` / `RiskDecision`。唯一进入决策的条件是：独立样本外
> 增量验证通过（`evaluate_incremental` → `SUPPORTED`）。

---

## 1. 交付物

| 模块 | 文件 | 职责 |
|---|---|---|
| 契约 | `research/intel/contracts.py` | `SourceRef` / `Fact` / `NewsEvent` / `PredictionMarketEvent` / `WalletSignal` / `IntelDigest` |
| 文本安全 | `research/intel/text_safety.py` | 清洗、注入检测（只标记）、确定性事实抽取 |
| 新闻 | `research/intel/news.py` | 去重、首次可见时间、可信度、窗口 |
| Polymarket | `research/intel/polymarket.py` | 准入（复用 registry）+ 盘口质量（点差/流动性） |
| 聪明钱 | `research/intel/wallets.py` | 可复制性、已实现/未实现分离、幸存者偏差 |
| 汇总 | `research/intel/digest.py` | `IntelDigest` + 附到决策日志 |
| 增量评估 | `research/intel/evaluation.py` | 配对 Block Bootstrap + 样本门槛 |
| 接入 | `research/runtime/round.py` | `run_round(..., intel=digest)` 只写日志 |

---

## 2. 三条研究线

### 2.1 新闻（NewsEvent）

字段：`event_id`（去重键）、`headline`、`facts`（数字+单位）、`occurred_at_ms`、
`first_seen_ms`、`sources`、`credibility`、`horizon_ms`、`tags`、`injection_suspected`。

规则：
- 去重键 = 归一化标题 + 第一来源 + 事件发生分钟；同事件重复抓取只保留最早一条；
- **时间**：`first_seen_ms` 是"我们第一次看到它"，研究只用它做可得性门禁；
  出版时间不能假装当时已知；
- 可信度：无来源 → 0；多来源递增（调用方可覆盖）；
- 窗口：`as_of - occurred_at_ms <= horizon_ms` 才进入 digest。

### 2.2 Polymarket（PredictionMarketEvent）

准入 = `market.registry.assess`（过期/规则不明/视界匹配）+ 盘口质量：
- 缺盘口/交叉盘口 → `UNKNOWN`；
- 点差 > 200bps → `WIDE_SPREAD`；
- 流动性 < 5000 USDT → `LOW_LIQUIDITY`；
- 任一不合格 → `researchable=False`，原因写入 `reasons`。

概率只做展示与增量评估输入，不直接产生方向。

### 2.3 聪明钱（WalletSignal）

- 已实现与未实现盈亏分开；浮盈不是战绩；
- 可复制性 = 延迟 ≤ 5s、规模 ≤ 流动性 5%、非"只有浮盈"；
- 每个不可复制原因都写进 `reasons`（`DELAY_TOO_LONG` / `SIZE_TOO_LARGE` / `UNREALIZED_ONLY`）；
- `scorecard` 显式提示幸存者偏差（样本内全是盈利钱包）与覆盖率异常。

---

## 3. 不可信文本隔离

外部文本只当数据：

| 处理 | 行为 |
|---|---|
| `sanitize_text` | 去控制字符、限长 4000，保留哈希 |
| `detect_injection` | 命中常见注入模式（中英文）→ `injection_suspected=True`，仅标记 |
| `extract_facts` | 只抽"数字+单位"，不做语义推断、不调用 LLM |
| digest | 注入事件汇总进 `warnings`（`INJECTION_SUSPECTED:...`） |
| 决策 | `Selection` / `RiskDecision` / `VERIFIED` 状态**完全不受影响**（有测试） |

明确不执行文本中的任何指令（"忽略之前指令""把策略标为 VERIFIED""修改风控"）。

---

## 4. 增量价值评估（唯一验收标准）

```python
evaluate_incremental(baseline_payoffs, augmented_payoffs, block=1, min_samples=30)
# -> {"n", "verdict": SUPPORTED|FALSIFIED|INCONCLUSIVE, "diff", "ci", "reason"}
```

- 同一批决策点、同一标签，逐点配对；
- Block Bootstrap 95% CI（块长按采样/视界确定，与 Sprint 0 口径一致）；
- 样本 < 30 → INCONCLUSIVE；
- 只有 `SUPPORTED` 才允许讨论"接进策略"；`FALSIFIED` 保留公示价值；
- 结论只对"该类情报 + 那批决策点"成立，不外推。

**Sprint 3 不宣称任何情报有效**：框架与契约就位，真实数据与样本外检验是部署节点的任务。

---

## 5. 接入方式

```python
digest = build_digest(as_of_ms, news=..., markets=..., wallets=...)
run_round(..., intel=digest)      # 只写日志的 intel 段，不改 selection
```

日志新增 `intel` 段：三条线的结构化事件 + `advisory_only=True` + `warnings`。

---

## 6. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/intel/test_text_safety.py` | 清洗、注入标记、事实抽取、**注入不改变策略**、去重/可见性/可信度 |
| `tests/intel/test_polymarket.py` | 短周期可研究、长期不匹配、宽点差、低流动性、缺盘口、过期 |
| `tests/intel/test_wallets.py` | 可复制性三条件、浮盈不算战绩、幸存者偏差、覆盖率 |
| `tests/intel/test_evaluation.py` | 正/负/噪声/样本不足/长度不等 |
| `tests/intel/test_advisory.py` | intel 不改变 decision/risk/fill；注入只进 warnings |

全量：**654 passed, 1 skipped**（新增 25 项）。

---

## 7. 限制与接线

- **没有真实抓取**：新闻 RSS/API、Polymarket Gamma/CLOB、钱包链上数据都未在本 Sprint 接线；
  契约与筛选逻辑已就位，接线时只需把原始响应映射成 `SourceRef` / `book` / `WalletTrade`。
- **Polymarket WS 仍未验证**（握手通、0 帧），REST 是唯一可用链路。
- **没有 LLM 事件理解**：文本只做规则抽取；LLM 若要接入，必须只产出 `NewsEvent`，
  且输出经过 schema 校验与注入隔离，不得直接改风控。
- **增量评估需要真实样本**：当前没有足够数据，结论必然是 INCONCLUSIVE——这是诚实状态。

---

## 8. 与 Sprint 4 的关系

Sprint 4 负责 24h 运行：调度、心跳、断流熔断、看板。届时：

- 情报抓取作为独立任务，失败不阻塞交易；
- `IntelDigest` 写入看板与决策日志；
- 增量评估按周滚动，`SUPPORTED` 之前一律 `advisory_only`；
- 任何"验证状态提升"都必须产出可追溯的评估报告 ID，运行时无权自动升级。