# 连续 HOLD 根因诊断（P0）

分支：`audit/v3-acceptance-fixes`
诊断对象：`data/v3_abcd_24h`（只读，未打断）

## 结论

连续 HOLD 不是"模型太保守"这一个原因，而是**两个结构性缺陷叠加**：

1. **模型输入几乎不变**：`MarketSnapshot.summary()` 给 DeepSeek 的核心技术特征
   全部来自 `state.features`，而 `state.features` 由 `analyze()` 从 **1h** K 线计算
   （`research/market/state.py:262`）。5m 决策每 5 分钟调用一次，但喂给模型的
   1h 特征一小时才更新一次。5m/15m/4h 的动量、成交量、局部结构、突破/回撤
   根本没有送进入场模型。
2. **数据管线让输入更不变**：418/429 之后历史刷新失败，`views["5m"][-1]`
   停在上一次成功的收盘价；引擎仍在下一根 5m 继续调用模型。

## 实证

`data/v3_abcd_24h/deepseek_5m_decisions.jsonl` 前 10 轮：

| 决策时刻 | 使用的已收盘 K 线 | 历史刷新 | 决策 |
|---|---|---|---|
| 17:40:50 | 17:40 | ok | HOLD |
| 17:45:30 | **17:40** | 未尝试 | HOLD |
| 17:50:30 | **17:40** | 失败 | HOLD |
| 17:55:30 | 17:55 | ok | HOLD |
| 18:00:30 | **17:55** | 失败 | HOLD |
| 18:05:30 | **17:55** | 失败 | HOLD |
| 18:11:31 | 18:10 | ok | HOLD |
| 18:15:30 | **18:10** | 未尝试 | HOLD |
| 18:20:30 | 18:20 | ok | HOLD |
| 18:25:30 | **18:20** | 失败 | HOLD |

**10 轮决策只对应 4 根不同的已收盘 K 线**，其中 6 轮是在同一根旧 K 线上重复判断。
DeepSeek 的理由逐轮高度相似（"Downtrend but flat momentum, RSI neutral, weak ADX;
conflicting microstructure..."），正好符合"输入没变 → 输出不变"。

这不是策略结论——**是数据完整性和输入设计问题**。

## 代码证据

- `research/market/state.py:262`：`features = _features(closed_only(1h...), critical, 1h atr)`
- `research/decision/contracts.py:31-56`：`MarketSnapshot.summary()` 只发 `state.trend/volatility/momentum`
  和 `features.adx/rsi/atr_pct/range_*`，没有分周期明细。
- `research/decision/providers/deepseek.py:41-44`：user message 就是上述 summary 的 JSON。
- `research/runtime/v2_engine.py`：每轮 `_decide()` 复用同一份 `state`；历史刷新失败时仍继续决策。

## 已做修复（P0）

引擎新增"重复 K 线门禁"：
- 若 `views["5m"][-1].open_ms <= _last_bar` 且最新收盘距 `as_of` 超过一根 5m，
  本轮**直接跳过**，记 `data_stats["duplicate_bar_skips"]`，不再调用模型。
- 原来 15 分钟的 stale 门禁仍在；无新行情时**不开新仓**。

这保证后续实验里："10 轮决策 4 根 K 线"不会再发生；每轮决策都对应一根**新的已收盘 K 线**。

## 不做什么

- 不强制 LONG/SHORT，不降低 confidence，不放宽风控；
- HOLD 仍然是合法决策，不以交易次数为优化目标。

下一步用 P1 的多周期 EntrySnapshot 真正解决"输入不够精细"，而不是让模型"积极一点"。
