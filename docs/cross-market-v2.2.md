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

## P0 交付（本分支）

| 模块 | 文件 | 测试 |
|---|---|---|
| 数据模型 | `research/cross_market/schemas.py` | test_cross_market_core.py |
| 三时间戳 | `research/cross_market/timestamp.py` | 同上 |
| 事件存储 | `research/cross_market/storage.py` | 同上 |
| 确定性回放 | `research/cross_market/replay.py` | 同上 |
| 市场映射 | `research/cross_market/market_mapping.py` + `config/market_mapping.yaml` | 同上 |
| 无前视（P0 安全） | — | `test_no_lookahead.py` |
| 数据源探测 | `research/cross_market/network_probe.py` → `network_probe.json` | — |

## 数据源实测（2026-11-18，部署节点）

```
gamma-api.polymarket.com  OK（markets 查询，clobTokenIds 结构确认）
clob.polymarket.com/book  OK（REST 订单簿，真实 token_id）
ws-subscriptions-clob…    OK（WebSocket 握手）
verdict: ALL_REACHABLE（直连，无代理绕过）
```

## 已知预先存在环境问题（非本分支引入）

1. `tests/test_skill_standalone.py::test_repo_mode_env_still_works` —
   Windows GBK 控制台无法打印 U+2212（−），`research.verify --demo` 崩溃；
   本分支未触碰该路径（git status 全部为新增文件）。
2. `tests/test_roundlock.py` — 本环境偶发挂起（代码无循环，`os.kill` 探测 0.1s），
   未诊断到根因；同样预先存在。

## 边界

Agent 只负责执行，不私自扩大范围；禁为凑 A/B/C 结果补造缺失数据；
数据源不可达必须报告阻塞，不得伪造成功。
