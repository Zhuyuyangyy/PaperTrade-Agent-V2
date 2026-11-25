# S1 — 拥挤洗盘反转（Crowded Flush Reversal）规则与证伪契约

分支 `audit/v3-acceptance-fixes`。**只设计与契约，未跑回测、未接实盘、未合 PR #26。**

契约已事前锁定：`research/v4/locks/s1_crowded_flush_reversal.lock.json`
sha256 `a1aa277fda436081f17cd0872c9f5be86a581ce2cc9b500475a76a8c2bdf7a5f`
锁定时间 2026-11-25T02:35:50Z。**下面的 falsify_if 不得因结果修改；改了就是契约篡改，锁会检出。**

---

## 一、经济机制（先讲清谁在付钱）

永续多头拥挤 → 资金费率被推到高分位 → 维持保证金压力 → 一次急跌把高倍杠杆强平 →
OI 急降（仓位被清洗）而不是价格继续新低。此时**被迫卖压已经出清**，
但价格还没恢复，随后 4–24h 内的反弹衰竭提供可捕获空间。

付钱方 = 被强平的杠杆多头；我们赚的是**清洗后的错杀反弹**，不是预测 BTC 方向。

**不成立的情形**（必须 FLAT）：
- 费率高分位但 OI 没降 → 拥挤还在，没有出清，逆势接飞刀
- OI 降但费率不高 → 可能是正常减仓，不是强平
- 价格创新低而非止跌 → 清洗未完成

这三条拒绝条件必须在信号生成层实现，不能靠 LLM 临场判断。

## 二、数据（全部已有，无需新增采集器）

| 数据 | 来源 | 字段 | 精度 |
|---|---|---|---|
| 资金费率 | `data/v3_funding/funding.json` | `rate`, `funding_ms` | 8h |
| OI 快照 | `research/cross_market/collectors/binance_microstructure.py` | `oi_snapshot`, `event_ts` | 服务端时间戳 |
| OI 变化率 | 同模块 `oi_deltas_from_store()` | `oi_delta_bps` | 事件级 |
| K线 | `data/v3_funding/bars_1h.json` → 聚合 4h | OHLCV | 1h→4h |
| CVD | 同采集器 `_cvd_delta()` | `cvd_delta` | 1m 本地算 |

**防前视铁律**：沿用 `cross_market/schemas.py::LookaheadBiasError` ——
信号只能用 `event_ts <= 决策时刻` 的快照，成交在下一根 4h 开盘。

**资金费率的已知陷阱**：E4/E5 已经证明滚动 z 必须**不含当次**；
这里的高分位同样用 `window = values[i-lookback:i]`，不能用 `<=i`。

## 三、入场规则（全部阈值事前写死）

```
lookback        = 90 条 funding（≈30 天，与 E4/E5 一致）
q_high          = 0.95        # 费率滚动分位
oi_drop_bps     = -300        # OI 相对前值下降 ≥3%
stability_bars  = 1           # 一根 1h 收盘不再新低
max_body_atr    = 1.5         # 企稳那根实体 ≤1.5×ATR(14)，防追空
cooldown_bars   = 6           # 4h 根，防同族串仓
direction       = SHORT       # 高分位多头拥挤 → 反向做空
```

信号 = 四条**同时**满足：

1. `funding_z ≥ z(0.95)` 且为**正**（多头付溢价）
2. `oi_delta_bps ≤ -300`（杠杆出清）
3. 最近一根已收盘 1h：`close > prev_close` 且 `low ≥ prev_low`（止跌企稳）
4. 该根 `|close-open| ≤ 1.5 × ATR14(1h)`

任一不满足 → 该事件不出信号，**不做"弱化版"替代**。

**对称空头拥挤不纳入本版**：E2 已证明多空不对称，把做多版本塞进来等于扩大搜索空间，
没有事前依据。若要做，必须另开 S1b 并单独锁契约。

## 四、退出规则

| 项 | 值 | 理由 |
|---|---|---|
| take_profit | +1.5% | 目标 120–300bps 毛利；1.5% 落在下沿，避免彩票结构 |
| stop_loss | −1.2% | 风险 120bps，盈亏比 1.25 |
| timeout | 6 根 4h（=1 天） | 清洗效应应在 24h 内兑现 |
| trailing | 0.8% | 启动后只收紧不放松 |
| 单仓 | 是 | 承接 R0 的 `busy_until` 约束 |

同根双向触发按**止损优先**；跳空按更差开盘价成交（复用 `funding_exp.simulate_signal`）。

## 五、成本口径

- `fee_rate = 0.0004`（4bps/边，与 E4/E5 冻结口径一致）
- `fill_model = taker`，滑点 5bps/边 → 往返 18bps
- `funding = true`，逐次按真实费率结算（做空在正费率时**收**费）
- `fee_multiples = [1,2,5]` —— 2x/5x 必须重跑，不许只看基准

## 六、事前证伪契约（已锁）

| 规则 | 阈值 | 触发含义 |
|---|---|---|
| `min_fills` | 30 | 成交不足 → INCONCLUSIVE，不是"策略很好只是样本少" |
| `control_lift_ci_includes_zero` | true | 随机入场对照 95% CI 含 0 → 无选择性，直接杀 |
| `net_negative_at_fee_multiple` | 2 | 2x 成本转负 → 成本脆弱，直接杀 |
| `worst_case_net_negative` | true | 最坏路径转负 → 执行脆弱，直接杀 |
| `grid_positive_ratio_min` | 0.5 | 参数网格正收益格 <50% → 尖刺非高原，直接杀 |

**补充验收（E5 血泪，未写进引擎但必须人工执行）**：

| 项 | 判据 |
|---|---|
| 剔除最大 1/2/3 笔盈利 | 剔除 top1 后仍为正；否则判 `FRAGILE_SINGLE_TRADE` |
| 自然季度拆分 | 至少 6/8 季度为正 |
| 样本外 | walk-forward 5 折，最后一折独立，任何规则触发即整体 falsify |
| 试验计数 | 本策略记为第 1 次独立试验，累计计数写入 `research/v4/ledger.md` |

## 七、与随机基线的对接（必须做）

S1 不能只跑 `verify()` 就完事。R0 已经证明"从网格里挑最优"能凭空造出 +6.9：

- 随机对照 **必须重跑完整选择流程**（随机方向 + 4信号集 × 12退出网格）
- 真实值必须落在随机分布的 **>95 百分位**才算有选择性
- 报告里同时给 raw p 和 Bonferroni 校正（家族数至少 4：E4/E5/S1/S2）

## 八、可复算路径（设计，尚未执行）

```
1. 数据准备：aggregate_bars(bars_1h, 4) → 4h K线；funding 缓存只读
2. 信号生成：脚本 research/v4/build_s1_entries.py（待写）
   → research/v4/entries/s1_btc_4h_entries.csv  (t,side)
3. 契约校验：python -m research.verify --spec ... --lock research/v4/locks/...lock.json
4. 证伪跑批：python -m research.verify --spec ...  （未跑）
5. 随机对照：复用 research/experiment/baseline.py::mc_r0_signal_family
```

**当前状态**：第 1–3 步的契约与锁已就位；第 2 步信号脚本、第 4–5 步跑批**均未执行**。
本轮产出的是"可以立刻被证伪的命题"，不是"看起来能赚钱的回测"。
