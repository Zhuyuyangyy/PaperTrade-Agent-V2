# Sprint 4 — 全天候运行、监控与恢复

> 状态：**IMPLEMENTED（离线可加速验证）**。目标不是"跑起来"，是**出事能自己停下来、
> 重启不重复下单、运维能从文件里看出发生了什么**。

---

## 1. 交付物

| 组件 | 文件 | 职责 |
|---|---|---|
| 循环 | `research/runtime/loop.py` | 按 `as_of` 推进、幂等、STOP、熔断接线、写日志与心跳 |
| 心跳 | `research/runtime/heartbeat.py` | 原子写/读、连续失败、last_ok、staleness |
| 熔断 | `research/runtime/circuit.py` | 连续失败 → halt；连续成功 → 恢复 |
| 数据源 | `research/runtime/sources.py` | `CandlesSource`（离线回放/72h）、`FlakySource`（测试断流） |
| 看板 | `research/runtime/dashboard.py` | 只读心跳/日志/账本 → JSON 或自包含 HTML |
| CLI | `research/runtime/__main__.py` | `demo` / `run` / `dashboard` |

---

## 2. 一轮的固定顺序

```
幂等检查（last_round_ts >= as_of → 跳过）
  → STOP 文件 → 退出
  → 取数（失败则计一次失败、写心跳、不产生决策）
  → 数据可用即 record_success（连续成功达到阈值则本轮就允许开仓）
  → run_round：
       mark_bar（跳空按开盘价）/ expire（超时按当前价）
       select（VERIFIED 优先）
       risk（数据门 → 成本 → 仓位 → 日亏 → 现金）
       仅 ALLOW 且非影子才 submit
  → 写决策日志（含 intel）
  → 写心跳（rounds/last_ok/halted/reason）
```

---

## 3. 幂等与恢复

- **幂等键是时间**：心跳里的 `last_round_ts_ms`。同一 `as_of` 重跑直接跳过；
- **订单幂等**：`orders.intent_id` 主键 + `INSERT OR IGNORE`；
- **重启恢复**：账本在 sqlite；`PaperBroker.restore()` 只读回现金与持仓，不重放历史；
- **看板只读**：`read_ledger()` 用 `mode=ro` 打开，不写活账本。

---

## 4. 断流与熔断

| 事件 | 计数 | 结果 |
|---|---|---|
| 数据源异常/返回空/缺 1h | failure+1 | 达到 `fail_threshold`（默认 3）→ `halted` |
| halted 期间 | 照常 mark/expire 与写日志 | 日志 `hold_reason="HALTED: ..."`，不开新仓 |
| 成功取数 | success+1 | 达到 `recover_threshold`（默认 2）→ 解除 |

熔断不隐藏事实：心跳记录 `halted`、`halt_reason`、`consecutive_failures`；
看板 `health=HALTED`；恢复后 `trips` 仍保留次数。

---

## 5. 72h 无人值守验收（离线加速）

`tests/runtime/test_loop_72h.py`：400 根 1h 合成 K 线、1h 步进、72 轮。

| 断言 | 结果 |
|---|---|
| 72 轮全部执行 | ✅ |
| 每轮一条决策日志 | ✅ 72 行 |
| 心跳连续（last_ok = 最后一轮） | ✅ |
| 订单唯一（intent_id 不重复） | ✅ |
| 重启后重跑同一区间 | skipped=11，订单不增 |
| 中途注入 3 次断流 | trips=1 → halted → 2 次成功后恢复；日志 69 行（失败轮不写） |

`python -m research.runtime run --hours 24` 可直接跑一段真实合成回放查看输出。

---

## 6. 看板

```bash
python -m research.runtime dashboard --root /path/to/run --html /tmp/board.html
```

显示：health（OK/STALE/HALTED/UNKNOWN）、heartbeat age、rounds、halt reason、
cash / open positions / realized、最近 5 轮决策（state/decision/risk/equity/hold）。

**缺失即 UNKNOWN**：没有心跳文件 → `NO_HEARTBEAT`；年龄超 2h → `STALE`；
账本打不开 → `LEDGER_UNREADABLE`。看板不替系统假装健康。

---

## 7. 明确限制

- 这是**离线调度器**：真实 24h 运行还需要进程守护（systemd/Windows 服务）、
  定时器或 supervisor；当前 `run` 是阻塞循环，适合前台观察与验收集。
- **没有通知**：告警只写心跳与看板；邮件/IM 推送未接。
- **数据源未接真实 API**：`CandlesSource` 是回放；真实采集器（Binance K 线/Polymarket）
  在 Sprint 0/3 已有契约，接上后替换 `Source` 即可。
- **day_start_ms** 仍由调用方传入；真实运行的日界（UTC）应在启动脚本里算好并固定。
- **72h 验收是加速模拟**：它验证的是循环逻辑、幂等、熔断与恢复，不是真实时钟下的
  进程稳定性；后者需要一次真实 72h 运行（Sprint 4 的部署验收）。

---

## 8. 下一步

- 接真实数据源（Binance REST/WS、Polymarket REST，WS 仍待验证）；
- 进程守护与告警；
- 真实 72h 运行，再用看板逐项核对心跳与订单；
- 情报增量评估按周滚动，`SUPPORTED` 之前一律 advisory。