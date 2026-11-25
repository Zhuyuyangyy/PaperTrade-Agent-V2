# research/live：Phase 3 基线 1 的 dryrun 管线

状态：**代码就绪、自检与演练已通过，但 4 周 dryrun 未启动。**
原因见 [`../lite/reports/phase3_correction_2026-11-25.md`](../lite/reports/phase3_correction_2026-11-25.md)：
修正"最低价当收盘价"的 bug 后，基线 1 被同仓位静态基准全面压制，部署前提消失。

---

## 1. 分层

```
voltarget_signal.py     纯函数：日线 → sigma20 → 目标权重。不写账本、不下单
        │  (返回 dict)
        ▼
voltarget_runner.py     账本 + ladder：结算、对账、冻结、落盘
        │
        ▼
ladder.py               状态机：halt > demote > promote（promote 必须人工确认）
```

物理隔离靠签名保证：信号层的 `compute()` 接收日线列表和当前权重，返回一个 dict，
没有任何文件写入、没有状态对象。运行器是唯一持有 `nav / weight / pending` 的地方。

## 2. 规则（与 Phase 3 契约逐字一致，未改）

```
sigma20 = stdev(最近 20 个日收盘收益率) × sqrt(365)
raw     = min(0.30 / sigma20, 1.0)
w_new   = raw      if |raw - w_prev| > 0.10
        = w_prev   otherwise
```

- 决策：D-1 收盘（UTC 00:00）→ 成交：D 开盘（UTC 00:00）
- 成本：11bps/边，`|Δw| × 11bps`
- 不加杠杆、不做空、上限 100%

## 3. 防前视与数据校验

| 项 | 实现 |
|---|---|
| 只用已收盘 K 线 | `close_time < serverTime`（复用 `alpha_lite.split_closed`） |
| 日线完整性 | 当天 1h 不足 20 根 → 丢弃该日，不补 |
| 缺口 | 只看最近 30 天；有缺口 → `DataError` → 冻结，不降级 |
| 陈旧 | 最后一根已收盘日线超过 2 天 → 冻结 |
| 指纹 | 每根 1h `sha256[:16]`；日线按天聚合出 `day_digest`；最近 25 天再聚合出 `window_fingerprint` |

缺口只查 30 天是刻意的：现货库里有 28 处 2023-03 之前的历史缺口，
修不了、也与当日信号无关，拿它阻断实盘等于永远不开机。政策写在
`voltarget_live.json` 的 `data_policy.note` 里。

## 4. 命令

```bash
python research/live/voltarget_runner.py selfcheck    # 与回测逐日对齐（必须 PASS）
python research/live/voltarget_runner.py drill        # 缺口/断流/对账 冻结演练（必须 3/3）
python research/live/voltarget_runner.py bootstrap    # 建初始状态（空仓），不记假成交
python research/live/voltarget_runner.py once         # 跑一轮（幂等，重复跑会 skip）
python research/live/voltarget_runner.py loop         # 每天 UTC 00:00:10 自动跑
python research/live/voltarget_runner.py status       # 看账本与 ladder
```

`loop` 退出条件：建 `research/live/STOP` 文件，或触发冻结。

**定时**：本机 `schtasks` 被安全策略拦截，不要用。要么在终端里跑 `loop`（关掉终端就停），
要么放到 Lighthouse 服务器上用 cron：

```
10 0 * * * cd /path/to/PaperTrade-Agent-V2 && python research/live/voltarget_runner.py once >> logs/voltarget.log 2>&1
```

## 5. 4 周验收的四个指标（当前状态）

| # | 指标 | 怎么验 | 状态 |
|---|---|---|---|
| 1 | 连续 28 天零漏跑、指纹可复算 | `ledger.jsonl` 每个决策日一条；`window_fingerprint` 可重算比对 | 未开始（时钟未启动） |
| 2 | 目标仓位与离线回测逐日 100% 对齐 | `selfcheck`：1,073 天，最大偏差 **0.00e+00**，调仓 91 次一致 | **PASS** |
| 3 | 模拟成交价 vs 11bps/边假设的真实偏差 | ledger 记 `fill_ref_price` / `fill_source` / `late_hours`，事后与首小时 VWAP 比 | 待采样 |
| 4 | 缺口/断流/对账异常自动冻结 | `drill`：三种故障全部冻结且不产新决策 | **PASS 3/3** |

## 6. 已验证的两件事

**自检**（2026-11-25）：

```
逐日净收益最大偏差 0.00e+00（2026-11-24）
净值 live 1.21544557 vs backtest 1.21544557
调仓次数 live 91 vs backtest 91
平均仓位 live 0.72730177 vs backtest 0.72730177
PASS 零口径漂移
```

**演练**：

```
PASS  缺口 → 冻结  —  信号窗口有 1 处缺口：[('2026-11-19', '2026-11-21')]
PASS  断流 4 天 → 冻结  —  数据陈旧 4.4 天，超过 2 天上限
PASS  账本被改 → 冻结  —  对账不平：1 笔本地账本与交易所不匹配
```

## 7. 为什么不启动 dryrun

Phase 3 首版的 +19.10%/夏普 0.683 来自"最低价当收盘价"的 bug。
修正后基线 1 是 +6.87%/0.366，而**同平均仓位的静态 BTC（72.7%）是 +11.92%/0.500**。
动态控波在这段样本里是负价值，不是"好风控但没提升夏普"。

拿一个已知被静态基准全面压制的策略跑 4 周 paper，除了制造"我们在推进"的错觉，
不会产生任何新信息。管线留着，等下一个有正期望的候选再挂。
