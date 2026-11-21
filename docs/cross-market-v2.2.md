# Cross-Market Intelligence v2.2 — Research-Only 规格

> 第一阶段唯一目标：**验证 Polymarket 信息是否在 Binance 微观结构信息之外提供
> 稳定、可重复、样本外的增量预测价值。**
>
> NO PRIVATE KEY · NO ORDER API · NO LIVE EXECUTION · NO AUTO COPY · NO CAPITAL AT RISK

## 状态模型（两态分离）

Evidence State：`UNTESTED → UNVERIFIED → PREDICTIVE_SUPPORTED → ECONOMIC_SUPPORTED`，
证伪出口：`FALSIFIED`。
Permission State（完全独立）：`OBSERVE_ONLY / PAPER_SIMULATION / LOCKED_FALSIFIED`。

**"有预测价值" ≠ "允许拿真钱交易。**

## 数据纪律

- 价格/价差一律 `Decimal`；
- 三时间戳分置：`event_ts / exchange_ts / ingest_ts`（ingest 只做延迟诊断，禁入特征）；
- 强平永远是**单所做市所 PARTIAL 观测值**（`LiquidationObservation.coverage=PARTIAL`），
  禁输出"全网清算"；
- OI 状态机只用观察性语言（`PRICE_UP_OI_EXPANSION_BUY_FLOW` …），
  禁推断交易员意图；
- Feature/Label 严格分离，`assert_no_lookahead` 为 P0 安全断言。

## 实验设计

A = Binance only；B = Polymarket only；C = A+B。
核心问题：`Performance(C) − Performance(A)`（增量），不是 C 自己能否赚钱。
预注册单一 primary horizon；多重检验必须校正（primary + pre-registration +
correction + effect size + CI）。
统计链：CCF → Lead-Lag → Diebold-Mariano → Block Bootstrap → OOS
（train/validation/locked test）。

## 经济层

`EV_net = EV_gross − fee − spread − slippage − latency − impact − funding`，
`EV_net > threshold` 才允许 `ECONOMIC_SUPPORTED`。

## P1 交付（本分支，2026-11-19 完成）

| 模块 | 文件 | 测试 |
|---|---|---|
| 数据模型 | `research/cross_market/schemas.py` | test_cross_market_core.py |
| 三时间戳 | `research/cross_market/timestamp.py` | 同上 |
| 事件存储 | `research/cross_market/storage.py` | 同上 |
| 确定性回放 | `research/cross_market/replay.py` | 同上 |
| 市场映射 | `market_mapping.py` + `config/market_mapping.yaml`（3 个真实市场） | 同上 |
| 无前视（P0 安全） | — | `test_no_lookahead.py` |
| Poly REST 采集器 | `collectors/polymarket_rest.py` | test_collectors.py |
| Binance 微结构采集器 | `collectors/binance_microstructure.py` | test_binance_micro.py |
| 特征对齐 | `feature_engine.py`（可用性门禁+时钟偏移保守处理） | test_experiment.py |
| 标签引擎 | `label_engine.py`（仅未来窗口） | 同上 |
| 成本模型 | `cost_model.py`（EV_net） | 同上 |
| A/B/C 实验骨架 | `experiment.py`（预注册+Block Bootstrap+证伪适配器） | 同上 |
| 数据源探测 | `network_probe.py` → `network_probe.json` | — |

## P1 端到端实测（2026-11-19，部署节点）

```
双采集器真连：Binance 微结构（BTC $62,450 · OI 96,518 · CVD −1.272）
              + Polymarket 盘口（BTC<60k 市场 Yes/No 双 token）
EventStore：三时间戳落盘（event_ts=服务端时间，禁本地冒充）
对齐层：双源特征齐备（poly 4 字段 + binance 7 字段）
A/B/C：冒烟输出 INCONCLUSIVE（无未来标签——诚实）
时钟偏移实况：CLOB 服务端比本地快 ~4s → 可用性门禁保守挡下（安全方向）
```

## 审查后加固（2026-11-18/19）

1. **可用性门禁（P0）**：事件在 `max(event_ts, ingest_ts)` 之前不可用——
   `event_ts=1000 / ingest_ts=5000` 的行情在 `decision=2000` 时不可使用
   （延迟到达 = 隐蔽前视）。`ReplayEngine.run(decision_now_ms=...)` 实现该门禁；
   `ingest_ts` 只判可用性，**永不作为特征**。恶意用例：
   `test_delayed_arrival_is_not_available_at_decision_time`。
2. **OI 三字段拆分**：`price_direction / oi_direction / taker_flow_direction`
   相互独立；taker 方向只能来自实际成交数据，缺数据 = `None`（禁推断）。
   修正了"价格涨+OI 平被吞成 FLAT"的旧错误。
3. **强平覆盖受约束枚举**：`coverage ∈ {PARTIAL, UNKNOWN}`，禁传 FULL。
4. **CI 依赖**：`requirements-dev.txt` 增加 `PyYAML>=6.0,<7`。
5. **探测诚实化**：DNS 落在 `198.18.0.0/15` → 标记
   `SUSPECTED_FAKE_IP_OR_PROXY`（不宣称全链路直连）；hostname 仅记录，
   附 `deployment_node: UNCONFIRMED` 待操作者确认。

## 数据源实测（2026-11-18 初测 / 2026-11-19 加固）

```
gamma-api.polymarket.com  OK（markets 查询，clobTokenIds 结构确认）
clob.polymarket.com/book  OK（REST 订单簿，真实 token_id）
ws-subscriptions-clob…    握手 OK，但订阅后 0 消息（两种订阅格式均试过）
                           → 消息流未证明：P1 阻塞项，不伪造成功
directness: SUSPECTED_FAKE_IP_OR_PROXY（DNS 解析落在 198.18.0.0/15）
```

**验收口径（遵审查裁定）：API 可访问 ✓；生产采集链路 ✗ 未验证。**

## 已知预先存在环境问题（非本分支引入）

1. `tests/test_skill_standalone.py::test_repo_mode_env_still_works` —
   Windows GBK 控制台无法打印 U+2212（−），`research.verify --demo` 崩溃；
   本分支未触碰该路径（git status 全部为新增文件）。
2. `tests/test_roundlock.py` — 本环境偶发挂起（代码无循环，`os.kill` 探测 0.1s），
   未诊断到根因；同样预先存在。

## P1 阻塞项（审查裁定，启动前必须解决）

1. **CLOB WS 消息流未证明**：握手 OK 但订阅后 0 消息（已试
   `assets_id` 数组 + `/ws/` 路径）。需定位正确订阅格式并在活跃市场上
   捕获真实消息。
2. **部署节点确认**：hostname 仅为记录；需操作者确认探测机器即部署节点。
3. **直连性未证明**：DNS 落在 fake-ip 段，Python 层未挂代理不等于全链路直连。

## 边界

Agent 只负责执行，不私自扩大范围；禁为凑 A/B/C 结果补造缺失数据；
数据源不可达必须报告阻塞，不得伪造成功。

---

## P1.1 数据正确性修复（Sprint 0，2026-11-21）

审计发现 13 项会让研究结论失效的缺陷（完整清单见 `docs/audit/sprint-0-audit-report.md`），
本分支已修复并给出回归用例（`tests/cross_market/test_data_correctness.py`，18 项；
修复前 17 failed / 1 passed，修复后 18 passed）。

关键口径变化（会让旧结果不可比，必须重跑）：

1. **标签是相对收益**：`ResearchLabel.future_return = future_price/origin_price − 1`，
   不再是价格。旧数据里 `future_return` 的绝对值不能再当收益用。
2. **方向信号用变化量**：A 臂看 `price_delta_bps`（含 deadband），B 臂看
   `yes_mid_delta_bps` 并按 `event_comparator`（ABOVE/BELOW）映射到标的方向。
   旧的"价格>0 → 做多"已在数学上被删除。
3. **A/C 按共同决策点比较**：主指标是逐点 payoff 差（弃权=0，配对 Block Bootstrap），
   输出 `aligned_n`；`aligned_n < 30` 一律 INCONCLUSIVE。
   `common_hit_ci` 只是诊断，且在当前 C⊆A 构造下恒为 0。
4. **特征 point-in-time**：`decision_now_ms` 只作实时天花板，不再让窗口内所有点
   共用同一个决策时刻；每个点用 `t` 与 `t−lookback` 两个独立游标。
5. **Polymarket 概率归一化**：`yes_mid`/`no_mid`/`consistent`/`prob_usable`；
   一价关系破（|yes+no−1|>0.05）→ B 臂弃权。
6. **采集**：best bid = max(bids)、best ask = min(asks)；缺服务端时间戳丢弃；
   相同 book hash 去重；CVD 只用已收盘 K 线；资金费计入 `EV_net`。

新增/修改的公共 API：

```python
store.query_available(source, start_ms, end_ms, as_of_ms=None)  # 按可用时间排序
store.last_available(source, key, as_of_ms)                     # point-in-time 取值
store.first_available_after(source, key, from_ms, until_ms=None) # 标签未来窗口
store.append(..., dedupe=True)                                  # 重复盘口去重
align_features(..., lookback_ms=300_000, max_age_ms=600_000)     # 双游标 + 变化量
label_from_store(store, symbol, origin_ts_ms, horizon_ms, band_ms=600_000, with_mae=False)
run_abc(points, labels, h)  # -> AbcVerdict(aligned_n, common_hit_ci, increment_ci, ...)
```

仍然阻塞（未在本次修复范围内）：

- CLOB WS 消息流未验证（REST 仍是唯一可用链路）；
- `config/market_mapping.yaml` 的市场与 5m 主视界不匹配（设计级问题，见审计报告 §4.1）；
- 本机 Windows 全量 `pytest -q tests` 在 61% 处退出（既有环境问题，CI 不受影响）。
