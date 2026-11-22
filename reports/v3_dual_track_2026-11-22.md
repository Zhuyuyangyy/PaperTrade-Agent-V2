# V3 双轨研究：零交易样本处理与配对回放（2026-11-22）

分支：`audit/v3-acceptance-fixes`（继续在独立分支上，未合 PR #26）
测试：**891 passed, 1 skipped**
所有交易仍限制在本地虚拟盘；Binance Demo 自动执行未重启；无真实资金。

---

## P0 连续 HOLD 根因

报告：`reports/v3_hold_root_cause_2026-11-22.md`

两个叠加原因：
1. `MarketSnapshot.summary()` 的核心技术特征来自 `state.features`，而它来自 1h K 线；
   5m 决策每 5 分钟问一次，但模型看到的核心技术条件一小时才变一次。
2. 418/429 后历史刷新失败，引擎仍用旧 K 线重复决策：
   `data/v3_abcd_24h` 前 **10 轮决策只对应 4 根不同的已收盘 K 线**。

修复：
- 新增"重复 K 线门禁"：`views["5m"][-1].open_ms <= _last_bar` 且最新收盘距 as_of 超过一根 5m
  → 跳过本轮，记 `duplicate_bar_skips`。**无新行情就不决策、不开仓**。
- 测试覆盖：同一根 K 线不会被重复处理。

## P1 实验 E：多周期入场输入

新增：
- `research/decision/entry.py`：`EntrySnapshot`
  - 5m/15m/1h/4h 各自的 close/EMA20/60/200/RSI/ADX/MACD/ATR%/volume_z/slope、last_closed_ms、age_ms；
  - 新闻带 `occurred_at_ms`/可信度/方向/是否相对上轮新增，**未来事件直接过滤**；
  - 成本：双边手续费、双边滑点、点差、roundtrip_bps、min_net_edge_bps；
  - 冻结基线决策一并送入，便于比较。
- `research/decision/providers/entry_multitf.py`：候选模型。原 `DeepSeekProvider` **保持冻结不动**，
  测试断言旧基线输入里没有 `timeframes`/`roundtrip_bps`。
- CLI：`--entry-provider legacy|multitf`；每轮把完整输入写 `<root>/entry_inputs.jsonl`，
  决策日志带 `entry_input_hash` 与 `entry_model`，可逐轮审计。

**没有以"多交易"为目标**：HOLD 仍是合法决策；不强推方向、不降 confidence、不放宽风控。

状态：代码与单测完成；原定 6 分钟实盘冒烟因 fapi 再次 418 触发 10 分钟全局冷却而未完成，
**不把这次冒烟算作有效实验**。

## P2 实验 M：配对历史事件回放

新增 `research/experiment/paired.py` + CLI `python -m research.experiment paired`：
- 事件生成规则固定：5m EMA20 上穿做多 / 下穿做空，cooldown 3 根，全量生成、不事后挑选；
  事件 i 只使用 bar[0..i]，成交价就是 bar[i] 的已收盘价。
- A/B/C/D 从**相同入场时间、方向、成交价、数量、费用、初始风险预算**各自独立开始：
  A 固定 ATR 止损止盈；B 成本感知退出 + 冷却；C 确定性分批 + 移动止损；
  D 注入真实 PositionManager；无模型则标 `NO_AI`，不得冒充 AI。
- 净收益统一按 `positions.pnl + position_fills.pnl`，不重复计算。
- K 线只拉一次并缓存 `bars_5m.json`，回放阶段零 REST。

真实历史 K 线（BTCUSDT 5m，300 根）首次运行结果：

| 事件数 | A | B | C | D |
|---|---|---|---|---|
| 8（无 AI） | -1.2046 | -2.1517 | -0.8802 | 0（NO_AI ×8） |
| 3（接 DeepSeek PM） | 0 | -0.7750 | +0.1274 | 0（24/24 次模型成功，全部 HOLD） |

**这些是工程验证数据，不是策略结论**：事件数太少、D 未产生任何管理动作、未实现盈亏未计入。
但至少证明：配对机制可用、D 确实调用了模型且成功率为 24/24、不会把 NO_AI 冒充成 AI。

## P3 REST 请求预算

新增 `research/market/budget.py`：`RequestBudget` 全局共享闸门。
- 滑动窗口 30 次/分钟；418/429 后全局冷却 10 分钟；
- 请求数、预算拦截数、限流数、等待时长、冷却触发次数全部计数；
- `BinanceKlineSource` 所有请求经过闸门；引擎每轮把 `rest_budget` 快照写进 `data_stats`；
- 配对回放 CLI 改为**单次请求 + 本地缓存**，不再用四周期批量抓取。

残余风险：新闻采集仍走独立 fetch，没有接入全局预算；下一步要并入。

## P4 已提交材料与未完成项

已提交：
1. 连续 HOLD 根因报告；
2. 多周期模型输入证据（代码 + `entry_inputs.jsonl` 格式 + 单测）；
3. 历史交易事件与无前视生成规则（`generate_events`）；
4. 四组配对交易与管理动作日志（`paired_results.jsonl`）；
5. 含手续费/滑点的净收益核算（统一口径）；
6. 当前样本量与不确定性：8 / 3 事件，明显不足；
7. AI 调用成功率：D 组 24/24，动作全是 HOLD，回退比例 0。

未完成，不能宣称有效：
- 足够样本量与样本外验证；
- 趋势 / 震荡 / 急涨急跌不同 market regime 分层；
- AI 推理费用计入净收益；
- 新闻采集接入共享请求预算；
- 多周期入场候选的真实运行对照（418 冷却阻断）。

## 两个最终问题（当前）

| 问题 | 答案 |
|---|---|
| AI 能不能找到有效机会？ | **待验证**。输入缺陷已修，候选模型已实现，但没有拿到有效运行样本 |
| 找到机会后它是否比固定规则更会管理持仓？ | **待验证**。配对机制已跑通，D 在 3 事件里 24 次调用全 HOLD，样本远不足以比较 |

当前 `data/v3_abcd_24h` 继续视为工程诊断，不参与任何收益排名。
