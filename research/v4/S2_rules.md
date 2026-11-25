# S2 — 波动率压缩突破 + 防追高：改造点清单与契约

分支 `audit/v3-acceptance-fixes`。**只设计，未改 `strategies.py`，未跑回测，未接实盘。**

---

## 一、先说清楚：现有代码已经有什么，缺什么

`research/strategy/strategies.py::breakout_confirm` 的实际逻辑（已读源码，不是转述）：

```
gate 风控 → ATR/close/ref_high/ref_low/volume_z 缺一即 INSUFFICIENT_FEATURES
close > ref_high × (1+5bps)  且 volume_z ≥ 1  → LONG
  止损 = ref_high − 0.5×ATR
  止盈 = close + 2×ATR
close < ref_low × (1−5bps)   且 volume_z ≥ 1  → SHORT（对称）
```

**它已经有**：5bps 噪声缓冲、量能确认、双向、ATR 止损。

**它缺的（长文本里点名的三件事，逐条对应）**：

| 缺口 | 现状 | 为什么要改 |
|---|---|---|
| 波动率压缩前置 | 无任何 regime 过滤，任何时刻都能突破 | Meme 实验 `chg_1h` 负 IC（−0.38）= 追已拉飞的 K 线在亏 |
| 防追高 | `volume_z≥1` 是**唯一**量能门，太松；无实体长度限制 | 长文本提的 `ALREADY_RUN_UP` 拒绝逻辑没有对应实现 |
| 防拥挤 | 完全不看 funding | 费率极端同向时突破 = 接最后一棒（E4 已证拥挤有信息） |
| 止盈障碍 | `close + 2×ATR` 固定，无可达性检查 | `verify/excursions.py` 会直接判 barrier；2×ATR 在 1h 上多数到不了 |

## 二、改造点（精确到函数与字段）

**不改 `breakout_confirm` 本身**——它是共享函数，改了会污染所有在跑的实验。
新增 `s2_squeeze_breakout(state, funding_state)` 注册进 `FUNCTIONS`，独立演进。

| # | 新增条件 | 阈值（事前写死） | 拒绝码 |
|---|---|---|---|
| 1 | ATR 压缩比 | `ATR14 / ATR60 ≤ 0.75` | `NOT_SQUEEZED` |
| 2 | 突破量能 | `volume_z ≥ 1.5`（现为 1.0） | `WEAK_VOLUME` |
| 3 | 防追高 | 突破根实体 `≤ 1.0 × ATR14` | `ALREADY_RUN_UP` |
| 4 | 防拥挤 | 同向 funding 滚动分位 `< 0.90` | `CROWDED_SIDE` |
| 5 | 持效确认 | 突破后 1 根 4h 收盘仍站稳 ref_high | `BREAKOUT_FAILED` |

**顺序很重要**：1→2→3→4→5，任一失败即返回拒绝码，**不做降级放行**。
这与 S1 的原则一致——"弱化版信号"是过拟合的温床。

### 退出端改造

| 项 | 现值 | 改为 | 理由 |
|---|---|---|---|
| 止盈 | `close + 2×ATR` | `close + 1.2×ATR` | 降低 barrier 失败率；用 trailing 0.8×ATR 保留上行 |
| 止损 | `ref − 0.5×ATR` | `ref − 0.8×ATR` | 压缩后波动小，0.5 太紧会被噪声扫 |
| EARLY_EXIT | 不存在 | 突破后 2 根内跌回压缩区间 → 立即平 | 长文本点名要复用 PR #26 逻辑，但**PR #26 不合并**；这里独立实现，只是同规则 |

**PR #26 边界**：长文本说"复用 PR #26 的 EARLY_EXIT"。
我不合并、不 checkout、不改那个分支——只读看了一眼它的存在性，本方案是**独立重写同规则**，
不从 PR #26 复制代码。这符合你全程的硬约束。

## 三、Spec 契约（待锁，本轮先给出内容）

```json
{
  "name": "s2-squeeze-breakout-btc-4h",
  "symbol": "BTCUSDT", "interval": "4h", "venue": "futures",
  "fee_rate": 0.0004, "fill_model": "taker", "funding": true,
  "exit": {"take_profit": 0.012, "stop_loss": -0.010,
           "timeout_bars": 12, "trailing": 0.008},
  "signals": {"kind": "csv", "path": "research/v4/entries/s2_btc_4h_entries.csv", "side": "both"},
  "control": {"enabled": true, "n_random": 2000, "seed": 20261125},
  "walk_forward": {"enabled": true, "folds": 5},
  "param_grid": {"axes": {"take_profit": [0.008,0.012,0.016],
                          "stop_loss": [-0.008,-0.010,-0.014]}, "max_cells": 9},
  "fee_multiples": [1,2,5],
  "falsify_if": {
    "control_lift_ci_includes_zero": true,
    "net_negative_at_fee_multiple": 2,
    "worst_case_net_negative": true,
    "min_fills": 30,
    "grid_positive_ratio_min": 0.5
  }
}
```

**注意**：这份契约**还没锁**。原因：S1 已经锁了第 1 次试验；
S2 的阈值是从 S1 的失败模式推导的，但我还没跑过 S1。
按"事前锁定"的本意，现在锁 S2 是合规的（数字还没读），
但我建议**等 S1 跑完再锁 S2**——这样若 S1 揭示新的失败模式，S2 的拒绝条件可以吸收它，
而不是锁死后再偷改。

**这是设计决策，不是拖延**：宁可少锁一个，也不要锁了再违反。

## 四、最容易被拦的关卡（预判，不作为结论）

| 关卡 | 预判 | 依据 |
|---|---|---|
| `barrier` | 高 | 现有 2×ATR 止盈几乎必然触发；改 1.2×ATR 后仍需实测 |
| `grid` | 高 | E2/E3 已经证明改止盈止损能扫出假阳性 |
| `cost` | 中 | 4h 成本占 ATR 11.2%，比 1h 好很多，但 S2 换手可能更高 |
| `insufficient` | 中 | 5 道过滤叠加后信号数可能 <30 笔 |

**如果 S2 也倒在这四关，那不是"参数没调好"，是这类信号在成本后没有空间。**
到那时候应该按 V4.1 计划的止损线转低频数据沉淀，而不是继续调阈值。

## 五、当前状态

- 已做：读源码、定位 4 个真实缺口、写出改造点与契约草案
- 未做：新增函数、锁契约、生成信号 CSV、跑 `verify()`
- 不动：`strategies.py` 现有代码、PR #26、任何环境/缓存
