# OpenAlpha V3 — Autonomous Position Manager：实现进度

日期：2026-11-22 17:00–17:30
提交：`efd41d5`（P0/P1/P2 基础）+ 本轮后续提交
测试：**872 passed, 1 skipped**

---

## 已实现（可运行、可回放、有测试）

### P0 决策接口 — 完成

`research/decision/position.py`

| 组件 | 内容 |
|---|---|
| `PositionSnapshot` | 实际成交均价、数量、名义敞口、当前价、**扣费后**浮盈亏、已实现盈亏、双边预估手续费、MFE/MAE、持仓时长、当前止损止盈、原始理由、失效条件、新闻变化、5m/15m/1h 趋势、剩余风险预算、数据时间 |
| `PositionDecision` | 6 类有界动作：`HOLD / CLOSE_ALL / CLOSE_PARTIAL / MOVE_STOP / UPDATE_TP / ADD_TO_WINNER` |
| 严格校验 | 每个动作必须有 rationale、data_as_of_ms、invalid_if；`CLOSE_PARTIAL` 的 fraction 必须 ∈ (0,1)；`MOVE_STOP` / `UPDATE_TP` 必须给目标价；字段缺失一律 `PositionDecisionError` 拒收，**不猜默认值** |
| 风险底线 | `validate_against_position()`：多头止损只能上移、空头只能下移（**只准收紧**）；止盈必须在正确一侧；`ADD_TO_WINNER` 只在净浮盈时可用。AI 不能撤止损、不能逆势摊平 |

### P1 管理决策 — 完成第一版

- `DeepSeekPositionManager`：模型看到的是**一整笔交易的处境**，不是一根 K 线；
  非法动作 / 解析失败 / 网络失败统一降级为 `HOLD / ERROR`，不中断循环。
- `rules_position_action`（确定性对照组）：浮盈达 1R → 减仓 50%；
  之后按 1×R 移动止损；**浮亏不再机械退出**。
- 引擎 `position_manager=off|rules|ai`：
  - `off`：旧行为（固定 ATR + 两根 K 线认错）；
  - `rules` / `ai`：有仓位时由持仓管理接管，旧出口同时停用，避免两个出口互相打架。
- **第一轮禁止 `ADD_TO_WINNER`**：加仓是独立实验，不混进动态退出对照。
- 冷却状态重启后从账本 `MAX(exit_ts)` 重建，重启绕不过冷却。

### P2 执行与账本 — 完成核心部分

| 能力 | 实现 |
|---|---|
| 部分平仓 | `PaperBroker.close_partial()`：按数量比例分摊开仓手续费、结转已实现盈亏、写 `position_fills` |
| 移动止损 / 调止盈 | `PaperBroker.move_stop()` / `update_tp()`，持久化并可用于回放 |
| 账本扩展 | `positions` 增加 `reason` / `invalid_if` / `realized_net`，含旧库迁移 |
| 交易所减仓 | `DemoExecutionAdapter.reduce()`：reduceOnly 市价，按 stepSize 取整 |
| 换保护单 | `replace_stop()` / `replace_tp()`：**先挂新单确认 NEW，再撤旧单**；撤旧失败保留两张，绝不裸仓 |

CLI 新增：`--position-manager off|rules|ai`、`--partial-fraction`、`--trail-r`，
以及上一轮的 `--exit-bars / --exit-min-loss-bps / --cooldown-bars`。

本地虚拟盘冒烟：`--position-manager rules` 正常起停，无交易所订单。

---

## 尚未实现 / 尚未验证（不要当成完成）

| 项 | 状态 | 为什么先不做 |
|---|---|---|
| **P3 共享行情流 + A/B/C/D 四本隔离账** | 未开始 | 用户明确要求"不要让多个进程各自请求 Binance REST"。当前引擎每本账一套 config，做不到单进程多策略；需要先改成 per-book config，再共用一份 feed。**直接开四个进程会再次触发 418/429** |
| **复利仓位（按净权益调整风险预算）** | 未实现 | 用户要求与动态退出分开实验；当前 notional 仍固定 100 |
| **顺势加仓** | 契约支持，执行层禁用 | 第一轮明确禁止混入对照 |
| **P4 事件序列回放** | 未做 | 需要覆盖持仓保持、阶段止盈、移动止损、趋势反转、新闻失效、断网、模型超时等固定事件流 |
| **样本外回测 / Shadow Forward Test** | 未做 | 必须先完成回放与一致性校验 |
| **镜像到 Binance Demo** | 未做 | 用户要求独立验证通过后才允许冻结版本镜像；本轮账户已清空 |

---

## 下一步的正确顺序

1. **单进程多策略**：把 `V2Config` 从"整台引擎一份"改成 per-book override，
   一份 feed 同时喂 A/B/C/D，避免 REST 限流并保证入场信号完全相同。
2. **固定事件序列回放**：验证 HOLD / MOVE_STOP / CLOSE_PARTIAL / CLOSE_ALL
   确实更新了本地持仓与保护单，且日志可逐条回放。
3. **A/B/C/D 本地虚拟盘对照**：≥100 笔平仓或 ≥24h，比较扣费净收益、
   每笔平均 bps、MFE/MAE、利润捕获率、最大回撤、换手成本、AI 推理费用、退出原因。
4. **达样本门槛且独立验证通过**后，才选择一个冻结版本镜像 Demo。

**验收标准不变**：AI 为什么继续持有、为什么移动止损、为什么部分止盈、为什么平仓，
这些动作是否真的执行、能否追溯、扣费后是否优于固定规则。
