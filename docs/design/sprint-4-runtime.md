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

---

## 9. 真实数据源适配（已就位）

`research/runtime/live.py::BinanceKlineSource`：

- 拉 `fapi/v1/klines` 的 5m/15m/1h/4h（limit 500），只保留 `close_ms <= as_of`；
- 只读公开端点、无 key；超时/重试/断流统一抛 `SourceError`，由 `RunLoop` 计失败并熔断；
- `fetch` 可注入 → 4 项离线测试（映射、未收盘过滤、缺 1h、网络异常计数）。

替换方式：

```python
source = BinanceKlineSource(symbol="BTCUSDT")
loop = RunLoop(root, broker, limits, source, ...)
```

**尚未在部署节点验证**：网络链路仍是 fake-ip + MITM 代理，第一次真实运行时先看
`stats()["errors"]` 与心跳的 `last_error`，再谈 72h。

---

## 10. 真实数据首跑（2026-11-21 20:52 CST）

```bash
python -m research.runtime live --rounds 1
```

```
[1795265222491] state=TREND_DOWN/HIGH_VOL/FLAT/RISK_HIGH conf=0.85
  decision=SHORT trend_follow shadow=True
  risk=ALLOW ('OK',) — 放行 100 USDT（波动预算 76.4bps − 成本 20.0bps = 56.4bps；
       这是波动空间，不是期望收益，需独立验证）
  equity=1000  source.stats={'requests': 4, 'errors': 0}
```

- 数据：BTCUSDT 5m/15m/1h/4h 各 299 根已收盘 K 线，最新 5m 收盘 58942；
- 状态：1h 空头排列 + ADX 强，波动率处于高分位 → `TREND_DOWN / HIGH_VOL / RISK_HIGH`；
- 策略给出 SHORT；因为是 `UNTESTED`，**只生成影子意图，不进账本**；
- 风控仍完整执行：波动预算 76.4bps，扣 20bps 成本后 56.4bps，放行额度 100 USDT。

**这不是结论，是快照。** 单次运行无法证明任何优势；`shadow=True` 是故意的：
未经独立验证的策略不允许占用模拟账本。真实 72h 运行与看板对账仍未做。

首次运行还确认了两件事：真实端点在本机可拉通（4 请求 0 错误），以及
`BinanceKlineSource` 的未收盘过滤在真实数据上生效（最新一根 5m 的 `close_ms`
必须早于 `as_of`，否则会被丢弃）。

---

## 11. Sprint 4.1：真实运行加固

复审发现 `live` 路径绕过了 `RunLoop`，心跳/熔断/STOP 都不生效；还留了 `--verified`
提权口子。四项修复：

| 问题 | 修法 |
|---|---|
| `live` 直接 `run_round`，不写心跳、不熔断、不认 STOP | `live` 改为构造 `BinanceKlineSource` + `RunLoop`，所有轮次走 `step()`：心跳、熔断、STOP、日志、幂等一条路径 |
| 幂等键是运行时毫秒，同一根 K 线内重复轮询会产生新的交易机会 | 幂等键改为**已收盘 K 线周期**：`key = as_of // decision_period_ms * decision_period_ms`；同一根 K 线只决策一次 |
| 熔断状态只在内存，重启即清零 | 熔断状态写入心跳 `circuit` 字段；`RunLoop` 启动时 `CircuitBreaker.from_dict()` 恢复，重启不会把"已熔断"变成"从未发生" |
| `live` 暴露 `--verified`，可用 `DEMO-ONLY` 把策略提升并成交 | 正式入口删除该参数；`VERIFIED` 仍必须带 `verified_by`（独立报告 id）。`--verified` 只存在于 `demo` |

另外修正 `--sleep-s` 语义：缺省 = `interval-ms / 1000`，1h 周期就是每小时一轮；
旧文档写的"12 轮 × 300s ≈ 一小时"是 5 分钟一轮，已改。

### 影子信号追踪

`python -m research.runtime shadow --root data/openalpha_live`：

- 从决策日志提取 `shadow=true` 的意图；
- 用后续 1h K 线评估 +1h/+4h/+24h：止损/止盈优先（同根双触发按止损），
  否则用 horizon 收盘价；
- 收益按方向计，缺数据返回 `null`，不猜；
- 输出每条信号的 exit/return_bps/reason 与汇总胜率、均值。

这是研究记录，不是交易指令；它的用途是回答"第一次 SHORT 之后市场到底怎么走"。

### 真实 72h 的前置

1. 在部署节点用 `live --interval-ms 3600000 --rounds 72`（或进程守护）跑满 72 小时；
2. 每轮看心跳 `rounds/last_ok/circuit` 与 `data/decisions.jsonl` 行数是否对得上；
3. 用 `shadow` 报告统计影子信号表现；
4. 期间任何一次熔断都要有 `halt_reason`，恢复要有连续成功记录。

本机可以验证逻辑，但"真实时钟 72h 进程稳定性"只能在部署节点验收。

---

## 12. Sprint 4.2：双模拟盘（实验盘 / 验证盘）

影子信号不能只记录方向。两本完全隔离的模拟账本：

| 账本 | 资金 | 谁能用 | 文件 |
|---|---|---|---|
| 验证盘 | 1000 USDT | 只有 `VERIFIED`（且必须带 `verified_by`） | `verified.sqlite` |
| 实验盘 | 1000 USDT | 只有影子意图（`UNTESTED`/`SHADOW`） | `shadow.sqlite` |

- `PaperBroker(shadow_mode=...)`：`reject`（验证盘，拒影子）/ `shadow_only`（实验盘，只收影子）/ `allow`（测试）；
- 实验盘照常计手续费、滑点、**资金费**（`funding_bps_per_8h`，多头支付/空头收取）；
- 平仓 `pnl` 是净额（毛 − 双边费 − 资金费）；
- 两本账的现金、持仓、订单、回撤互不影响；
- 实验盘的成交**不改变**策略的 `VERIFIED` 权限。

`python -m research.runtime report --root <dir>` 输出两本账：现金、成交数、平仓数、胜率、
已实现/浮动盈亏、资金费、最大回撤、按策略拆分。

### 真实历史 72 小时实验盘（2026-11-21，BTCUSDT 1h × 500 根）

```bash
python -m research.runtime replay --rounds 72 --limit 500
```

| 指标 | 实验盘（shadow） | 验证盘（verified） |
|---|---|---|
| 起始资金 | 1000 USDT | 1000 USDT |
| 订单 / 平仓 | 37 / 34 | 0 / 0 |
| 已实现盈亏 | **−9.44 USDT（−0.94%）** | 0 |
| 胜率 | 32.4% | — |
| 最大回撤 | 1.08% | 0 |
| 资金费（净） | 收取 0.08 USDT | 0 |
| 趋势跟随 | −7.72，胜率 17.6%（17 笔） | — |
| 区间回归 | −0.88，胜率 46.7%（15 笔） | — |
| 突破确认 | −0.84，胜率 50.0%（2 笔） | — |

**结论：目前不赚钱。** 趋势跟随在最近 500 小时里明显被反复止损（17.6% 胜率），
区间回归接近打平；扣掉全部模拟成本后仍是负的。这正是实验盘存在的意义：
在不动用验证盘、不授予任何 VERIFIED 的前提下，先把"能不能管好 1000 USDT"量化出来。

注意：这是历史回放（不是未来），且该回放只喂了 1h K 线（5m/15m 缺失），
状态判断只用到 1h/4h；真实 72h 运行应使用完整多周期数据。

---

## 13. Sprint 4.3：账本审计修正

| 问题 | 修法 |
|---|---|
| `RunLoop` 的 `day_start_ms` 固定为 0，日亏限制可能按累计历史执行 | 每轮用 `utc_day_start(as_of)` 重算 UTC 日界并传给风控；`RunLoop` 不再接受固定日界 |
| 用整根 1h OHLC 判止损：若开仓发生在该根中途，会把开仓前波动算进去 | `mark_bar(..., bar_open_ms=...)`：开仓时间晚于该根开盘 → 跳过本根，下一根起判断（路径未知，不虚报亏损） |
| `orders.shadow` 恒写 0 | 按意图写 `1`（实验盘影子）/`0`（验证盘） |
| 资金费是固定费率估算 | `summarize()` 输出 `funding_source: "estimated"`；`--funding-bps` 仅作估算，接入历史真实费率前不冒充事实 |

代价要说清：开仓当根不判止损，可能**漏掉**真实触发（下一根才处理），这是"不虚报亏损"的保守取舍；
更精确的做法是接入 5m/15m 子周期，属于后续数据接线工作。