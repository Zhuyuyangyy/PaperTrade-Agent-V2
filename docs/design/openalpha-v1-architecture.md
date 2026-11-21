# OpenAlpha V1.0 — Autonomous AI Trader 设计文档（Sprint 0 基线）

> 状态：**DESIGN / RESEARCH-ONLY**。本文件描述的是目标系统与数据契约，
> 不是收益承诺。V1.0 全链路只做只读采集、策略研究与**模拟**执行。
>
> 边界：NO PRIVATE KEY · NO ORDER API · NO LIVE EXECUTION · NO AUTO COPY
>
> 标的：**BTC/USDT 单标的**（SOL 的 L2 数据保留在研究库，不进入 V1 决策链）。
> 原则：**允许空仓，不允许停止思考和记录；允许判断错误，不允许隐瞒为什么错。**

---

## 0. 一句话定义

系统自己获取行情、理解市场状态、生成并验证交易假设、在风控允许时做模拟交易，
并持续把「为什么这么做 / 为什么错了」写进可核验的日志。

人类只做三件事：改风控参数、看复盘、决定是否让某个策略进入下一阶段。
人类**不**逐笔告诉系统做多还是做空。

---

## 1. 分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│ L1 情报层  Binance(多周期K线/L2/资金费/OI/CVD) · 新闻 · Polymarket │
│            钱包监测(只读)                                            │
│            输出：RawEvent（append-only，三时间戳）                    │
├─────────────────────────────────────────────────────────────────────┤
│ L2 理解层  趋势/波动率/动量/情绪/事件风险/跨市场关系                  │
│            输出：MarketState + Evidence + Contradictions + RiskLevel │
├─────────────────────────────────────────────────────────────────────┤
│ L3 决策层  Hypothesis → StrategyRegistry → 验证 → 选择 → LONG/SHORT/HOLD│
│            输出：TradeIntent（含依据、策略版本、失效条件）            │
├─────────────────────────────────────────────────────────────────────┤
│ L4 风控与模拟执行  独立确定性程序：费用/滑点/仓位/限额/熔断/撮合       │
│            输出：RiskDecision(ALLOW/DENY/FREEZE) + PaperOrder/Fill   │
├─────────────────────────────────────────────────────────────────────┤
│ L5 公示与复盘  DecisionLog(JSONL) · Dashboard · 归因 · 研究迭代       │
└─────────────────────────────────────────────────────────────────────┘
```

**关键约束：LLM 是研究员，不是风控开关。**
L3 可以用 LLM 做事件理解与假设生成；L4 的风控阈值、仓位、熔断、下单许可
全部由确定性代码执行，LLM 无权修改，也无法绕过。

---

## 2. 现有资产 → 目标模块映射

| 目标能力 | 现有资产 | 处理 |
|---|---|---|
| 原始事件存储 | `research/cross_market/storage.py`（P1.1 已修可用性排序/去重） | 直接复用 |
| 三时间戳 | `timestamp.py` | 复用，补时钟偏移估计 |
| 确定性回放 | `replay.py`（P1.1 按可用时间回放） | 复用 |
| 市场映射 | `market_mapping.py` + `config/market_mapping.yaml` | 复用，加 YES/NO 角色 |
| Binance 采集 | `collectors/binance_microstructure.py` | 复用，补 K 线多周期与 L2 |
| Polymarket 采集 | `collectors/polymarket_rest.py` | 复用；WS 单列 Sprint 1 任务 |
| 特征对齐 | `feature_engine.py`（P1.1 point-in-time + YES 归一化） | 复用，补 K 线指标 |
| 标签 | `label_engine.py`（P1.1 相对收益） | 复用 |
| 成本模型 | `cost_model.py`（P1.1 含资金费） | 复用 |
| A/B/C 实验 | `experiment.py`（P1.1 共同决策点 + payoff 配对） | 复用，接策略注册表 |
| 策略证伪/回测 | `research/verify/*`、`research/audit/*`、`research/replay/*` | 复用（详见审计报告资产表） |
| 模拟执行/账本 | `user_data/` 现有 dry-run 配置、`legacy_v1/paper_agent.py` | 改造为统一 PaperBroker |
| 看板 | `scripts/dashboard*.py`、`templates/dashboard.html` | 升级为决策公示看板 |
| 安全基线 | `scripts/safety_check.py`、CI | 扩展为风控/模式断言 |

**不新建重复模块**：同一个能力只保留一份实现；旧实现要么删，要么明确标注为 legacy 并加引用禁令。

---

## 3. 数据契约（Sprint 0 已落地 v2）

### 3.1 RawEvent（`EventStore`）

```
(source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms, payload)
```

- **可用时间** `available = max(event_ts, ingest_ts)`；回放/对齐/标签只认它。
- 窗口按 `event_ts` 界定，排序按 `available`——迟到数据属于它发生的那一刻，
  但不能在到达之前被使用。
- `dedupe=True`：完全相同的记录只落一次（Polymarket 轮询同一盘口）。
- 三个时间戳的用途固定：`event_ts` 研究对齐；`exchange_ts` 交叉校验；
  `ingest_ts` **只**用于可用性与延迟诊断，禁止进特征。

### 3.2 FeatureSnapshot v2（决策时刻 t 已知的信息）

```python
FeatureSnapshot(
  feature_ts_ms=t,
  polymarket={
    yes_bid, yes_ask, yes_mid, no_mid,
    consistent,            # |yes_mid + no_mid − 1| <= 0.05
    prob_usable,           # False → 不得当概率用
    yes_mid_delta_bps,     # 相对 t−lookback
    spread_bps, stale,
  },
  binance={
    price, price_delta_bps,
    oi_snapshot, oi_delta_bps,
    volume, cvd_delta,
    funding_rate,
    liquidation_observed_usdt, liquidation_coverage,  # 永远 PARTIAL/UNKNOWN
    stale,
  },
  schema_version=2,
)
```

铁律：
1. 不含 `ingest_ts`；不含任何 t 之后的数据；
2. **方向信号只用变化量**（`*_delta_bps`），不用水平值；
3. Polymarket 概率必须归一化到 YES 口径；NO token 的报价不是 YES 概率；
4. 一致性校验失败（两 token 一价关系破）→ `prob_usable=False`，信号层弃权。

### 3.2.1 时间语义三条硬规则（Round 2 复审钉死）

| 规则 | 含义 | 违反后果 |
|---|---|---|
| 事件时间界定 | 窗口/标签/因果都按 `event_ts` 说事 | 用"到达时间"当"发生时间"，迟到数据变成未来数据 |
| 可用时间门禁 | 决策时刻 t 只能使用 `max(event_ts, ingest_ts) <= t` 的事件 | 数据还没到就被使用 = 隐蔽前视 |
| 状态按事件时间取最新 | 同一 key 的状态以 `event_ts` 最大者为准；迟到旧数据不得覆盖 | 12:01 的旧报价在 12:06 覆盖 12:05 的报价，状态倒退 |

三条在代码里各有独立测试（`tests/cross_market/test_time_semantics.py` R2-01..R2-03）。

### 3.2.2 采样与独立样本

同一份数据按 5 秒采样、5 分钟视界，相邻 60 个点的标签高度重叠。A/B/C 实验因此必须：

- `block = ceil(horizon / step)` 作为 bootstrap 块长；
- 门槛用**有效独立共同点** `aligned_n / block`，而不是原始点数；
- 另报非重叠子样本（每 block 个点取一个）的增量 CI；
- 全样本显著、非重叠不显著 → 结论降级 INCONCLUSIVE。

这不是统计上的完备解（没有 HAC、没有多重检验校正），但足以阻止"用重叠点刷样本量"。
完整的多重检验校正在 Sprint 2 与 FDR 一起做。

### 3.3 ResearchLabel

```python
ResearchLabel(
  origin_ts_ms, horizon,
  future_return,        # future_price / origin_price − 1
  max_adverse_excursion, # 可选
  origin_price, label_ts_ms,
)
```

- 未来价格必须取 `available >= origin+H` 的第一条快照；
- 早于 horizon 的价格只配做诊断，不能当标签；
- 标签与特征逐点同索引，`run_abc` 会强制校验长度相等。

### 3.4 Decision 与风控（Sprint 2 落地）

```python
TradeIntent(
  intent_id, decision_ts_ms, symbol, side,           # LONG/SHORT/HOLD
  strategy_id, strategy_version,
  market_state, confidence,
  entry_condition, invalidation_condition,
  evidence: [EvidenceRef], contradictions: [EvidenceRef],
  expected_holding_ms, hypothesis_id,
)
RiskDecision(
  intent_id, decision,           # ALLOW / DENY / FREEZE
  checks: {data_fresh, spread_ok, cost_ok, position_ok, daily_loss_ok, ...},
  size_notional, stop_price, freeze_reason,
)
PaperFill(
  order_id, intent_id, ts_ms, side, qty, price, fee, slippage_bps,
  funding_paid, broker_version,
)
```

每笔模拟成交必须能回溯到：原始信号 → 特征快照 → 策略版本 → 风控检查 → 成交。

---

## 4. 市场理解层（L2）

### 4.1 状态定义（互斥 + 一个独立风险维度）

| 维度 | 取值 | 判定（示例，参数在配置里） |
|---|---|---|
| 趋势 | `TREND_UP` / `TREND_DOWN` / `RANGE` | EMA(20/60/200) 排列 + ADX ≥ 25 |
| 波动 | `LOW_VOL` / `NORMAL_VOL` / `HIGH_VOL` | ATR(14)/Price 分位 + 布林带宽 |
| 动量 | `MOMENTUM_UP` / `MOMENTUM_DOWN` / `FLAT` | MACD 柱 + RSI 区间 + CVD 方向 |
| 风险 | `RISK_LOW/NORMAL/HIGH/EXTREME` | 波动 + 点差 + 数据陈旧 + 事件临近 |

输出必须带：
- `evidence[]`：每个判断引用的具体特征值与阈值；
- `contradictions[]`：互相矛盾的信号（例如价格新高但 CVD 走弱）；
- `risk_level` 与触发原因。

**禁止**只输出一个 `bullish/bearish` 标签。没有证据链的状态判断视为无效。

### 4.2 多周期

5m / 15m / 1h / 4h 各自算指标，然后做一致性打分：
- 同向周期数 ≥ 3 → 趋势确认；
- 2 对 2 → `RANGE` 或降级为 HOLD；
- 大周期与小周期冲突 → 降低 confidence，并写入 contradictions。

### 4.3 新闻与 Polymarket（L3 输入，默认不参与下单）

事件抽取 → 来源核验 → 时间标记 → 宏观影响分类。Polymarket 侧做概率变化、
盘口、历史钱包行为。两者在通过增量验证前只作为**参考信号**展示，
不得改变模拟交易决策（`advisory_only=true`）。

---

## 5. 策略注册与自主选择（L3）

### 5.1 候选策略（V1 只做三类）

| 策略 | 适用状态 | 方向 | 失效条件 |
|---|---|---|---|
| `trend_follow` | TREND_UP/DOWN + NORMAL/HIGH_VOL | 顺势 | ADX 跌破 20 或 EMA 排列反转 |
| `range_mean_revert` | RANGE + LOW/NORMAL_VOL | 逆势 | 区间被有效突破（收盘 + 成交量确认） |
| `breakout_confirm` | RANGE→TREND 转换 | 突破方向 | 假突破（回到区间内） |

每个策略必须声明：适用状态、方向、入场条件、失效条件、持有期、成本假设、
版本号、以及**它在什么情况下不该出手**。

### 5.2 选择规则

1. 先由 L2 状态过滤出适用策略；
2. 对每个候选策略查它的证据状态（`UNTESTED/UNVERIFIED/PREDICTIVE_SUPPORTED/ECONOMIC_SUPPORTED/FALSIFIED`）；
3. `FALSIFIED` 直接排除；`UNTESTED` 只允许 shadow 记录，不允许下单；
4. 多个可用 → 按预注册优先级 + 近期样本外表现排序，**不做样本内择优**；
5. 全部不适用 → `HOLD`（空仓是合法输出，必须写日志说明为什么空仓）。

**市场层同样 fail-closed**：未登记、已过期、到期规则不明（`expiry_unknown`）的市场
一律不可进入研究，更不能进入决策。映射表不是"注册即可用"，是"满足条件才可用"。

### 5.3 A/B/C 增量实验的正确口径（Sprint 0 已修）

- 所有臂在**同一决策点向量**上计算；
- 主指标：逐点 payoff（方向 × 未来收益，弃权=0）的配对差 C−A，Block Bootstrap CI；
- 诊断指标：共同决策点命中率差、各臂 coverage；
- 门槛：共同决策点 < `min_aligned` → INCONCLUSIVE，不得宣布增量；
- **共同点命中率差恒为 0 是 C⊆A 的定义后果**，不能当作证据——这一点写进报告与
  实验输出，防止下次又拿它当"Polymarket 有用"的证明。

---

## 6. 风控与模拟执行（L4，确定性）

### 6.1 风控检查顺序（任一失败 → DENY/FREEZE）

1. `data_fresh`：任一侧数据 `stale` 或缺失 → FREEZE 新开仓；
2. `spread_ok`：点差 > 阈值 → DENY；
3. `cost_ok`：`EV_net = EV_gross − fee − spread − slippage − latency − impact − funding > 0`；
4. `position_ok`：单笔风险 ≤ 账户 0.5%，总敞口 ≤ 20%，同向持仓数上限；
5. `daily_loss_ok`：日内亏损 ≥ 2% → 当日停止新开仓；
6. `kill_switch`：连续 3 次异常 / 数据断流 > N 分钟 → FREEZE 全部。

### 6.2 模拟撮合

- 市价单按对手价 + 滑点（bps 可配，默认 5bps）成交；
- 限价单只在价格穿越时成交，不假设排队优先；
- 手续费双边计；资金费按持有期折算；
- 订单幂等：`intent_id` 唯一，重复提交不重复成交；
- 所有成交写入 append-only 账本，重启后从账本恢复状态。

### 6.3 时钟

- 每个数据源估计 `exchange_ts − ingest_ts` 偏移（滚动中位数）；
- 时钟偏移超过阈值（默认 10s）→ 该源标记 `CLOCK_SUSPECT`，不得用于新开仓；
- 时间一律用 UTC 毫秒整数；禁止本地时区字符串进数据层。

---

## 7. 24h 自主运行（L5 的运行基座）

### 7.1 调度

| 任务 | 周期 | 失败策略 |
|---|---|---|
| Binance 快照 | 5s | 重试 2 次 → 标记断流 |
| K 线收盘事件 | 5m/15m/1h/4h | 补拉最近 N 根，幂等写入 |
| Polymarket 盘口 | 5s（REST）/ WS（Sprint 1） | 重试 → 标记断流 |
| 市场状态计算 | 每 5s 或事件驱动 | 用最近可用特征，标 stale |
| 决策 | 每 5m（收盘后） | 跳过本轮并记录 |
| 复盘 | 每小时 + 每日 | 生成报告，不阻塞交易 |

### 7.2 心跳与恢复

- 每个任务写心跳（`state/heartbeat.json`：任务名、最后成功时间、连续失败数）；
- 心跳超时 → 看板红色告警 + 熔断新开仓；
- 进程重启 → 从账本 + 事件库恢复，不重放已成交的 intent；
- 所有定时任务必须幂等：同一时间窗重复执行不产生重复订单/重复事件。

### 7.3 断流熔断

- Binance 断流 > 60s 或 Polymarket 断流 > 120s → FREEZE；
- 恢复后先回补数据，再做一次对齐完整性检查，通过才解除；
- 熔断与解除都写入决策日志（谁触发、什么条件、持续多久）。

---

## 8. 公示与复盘

### 8.1 DecisionLog（每轮一条，JSONL）

```json
{
  "ts_ms": 0, "round_id": "", "symbol": "BTCUSDT",
  "market_state": {"trend": "", "vol": "", "momentum": "", "risk": ""},
  "evidence": [], "contradictions": [],
  "candidates": [{"strategy_id": "", "version": "", "eligible": true, "reason": ""}],
  "decision": "LONG|SHORT|HOLD",
  "reason": "", "confidence": 0.0,
  "risk": {"decision": "ALLOW", "checks": {}, "size_notional": 0},
  "orders": [], "fills": [],
  "data_health": {"binance_age_ms": 0, "poly_age_ms": 0, "skew_ms": 0}
}
```

### 8.2 复盘指标

- 每策略：样本数、命中率、平均 payoff、最大回撤、费用占比、覆盖率；
- 每个 HOLD：是"策略不适用"还是"数据不可用"还是"风控拒绝"，分开统计；
- 失败归因：信号错 / 风控过严 / 数据问题 / 执行滑点，四类必须能区分；
- 与简单基准对比：买入持有、随机入场、纯 A 臂。

### 8.3 看板

必须显示：当前状态、最近决策及证据链、持仓与盈亏、数据健康度、
熔断状态、策略证据状态、72h 运行连续性。

---

## 9. 14 天路线图（可验收版本）

| 阶段 | 天 | 交付 | 通过条件（不通过不放行） |
|---|---|---|---|
| **Sprint 0** | 1–2 | 审计报告、数据修复、契约文档、回归测试 | 新增回归测试全绿；全量测试无回归；无前视/对齐缺陷 |
| **Sprint 1** | 3–5 | 多周期指标、市场状态机、证据链输出 | 状态输出可解释；冲突信号被显式记录；离线回放确定性通过 |
| **Sprint 2** | 6–8 | 策略注册表、三类策略、A/B/C 决策中枢 | 每个策略有样本外检验；HOLD 有原因；决策→风控→模拟闭环 |
| **Sprint 3** | 9–11 | 新闻 Agent、Polymarket 情报、跟单研究 | 增量价值单独验证；未通过前 `advisory_only` 不参与下单 |
| **Sprint 4** | 12–14 | 调度、心跳、恢复、断流熔断、看板 | 72h 无人值守：无不可解释中断、无重复订单、无风控绕过 |

### 验收硬条件

1. 所有分析只用决策时刻已知数据（可用性门禁 + 前视测试）；
2. 每个策略声明适用状态，并做样本外、成本、稳健性检验；
3. 每笔模拟交易可追溯到信号/策略版本/风控/成交；
4. 数据陈旧、异常、超限 → 自动冻结新交易；
5. 72h 内无不可解释中断、重复订单、风控绕过；
6. 上线后继续 30 天模拟：净收益、最大回撤、稳定性、相对简单基准的改善。

**即使全部完成，也不代表策略必然正收益。** 这些条件证明的是"系统没有作弊、
没有失控"，不是"系统会赚钱"。

---

## 10. 明确不做（V1）

- 不接实盘、不接私钥、不自动跟单；
- 不做多标的（SOL 只做研究数据保留）；
- 不做"生成 1000 个策略"的搜索式过拟合；
- 不让 LLM 直接决定仓位、杠杆、熔断；
- 不在没有样本外验证的情况下把任何信号接进下单路径。

---

## 11. 失败模式与对策

| 失败模式 | 症状 | 对策 |
|---|---|---|
| 前视泄漏 | 回测漂亮、模拟拉胯 | 可用性门禁 + 每个决策点独立取数 + 回归测试 |
| 标签语义错 | 命中率异常高（>80%） | 标签必须是相对收益；命中率异常触发审计 |
| 样本不对齐 | A/C 增量显著但不可复现 | 共同决策点对齐 + 配对 payoff + 记录 aligned_n |
| 重复采集 | 同一盘口多次入库，coverage 虚高 | payload 去重 + 采集诊断计数 |
| 时钟偏移 | 偶发"未来数据" | 偏移估计 + CLOCK_SUSPECT + 保守门禁 |
| 过拟合 | 参数微调后样本内全绿 | 预注册 + 样本外 + 多重检验校正 + 参数敏感性 |
| 静默失败 | 数据缺了但系统照跑 | loud failure + 心跳 + 断流熔断 + 看板告警 |
| LLM 越权 | 风控被"解释"绕过 | 风控独立进程/独立函数，LLM 只产出 intent |

---

## 12. 目录规划（Sprint 1 起逐步落地）

```
research/
  cross_market/          # 原始事件、对齐、标签、成本、A/B/C（Sprint 0 已修）
  market/                # L2：指标、状态机、证据链        （Sprint 1）
  strategy/              # L3：注册表、三类策略、假设管理    （Sprint 2）
  intel/                 # L3：新闻、Polymarket、钱包       （Sprint 3）
  risk/                  # L4：确定性风控                  （Sprint 2）
  paper/                 # L4：撮合、账本、幂等             （Sprint 2）
  runtime/               # L5：调度、心跳、恢复、熔断        （Sprint 4）
scripts/
  openalpha.py           # 单入口：collect / decide / run / report
docs/design/             # 本文件与各 Sprint 设计
data/openalpha/          # events.sqlite / decisions.jsonl / ledger.sqlite / state/
```

一个入口、一个事件库、一个账本、一份决策日志。任何"另一个数据源/另一份账本"
都必须先证明现有实现不可用。