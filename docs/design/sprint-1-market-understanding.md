# Sprint 1 — L2 市场理解层（Market Understanding）

> 状态：**IMPLEMENTED / RESEARCH-ONLY**。本层不产生交易信号，只回答一个问题：
> 现在的市场是什么状态、凭什么这么说、什么情况下这个判断失效。
>
> 完成标准：即使尚无任何可交易策略，系统也能持续、准确、有证据地解释市场正在发生什么。

---

## 1. 交付物

| 模块 | 文件 | 职责 |
|---|---|---|
| 契约 | `research/market/contracts.py` | `Candle` / `TimeframeView` / `Evidence` / `Contradiction` / `DataQuality` / `MarketState` |
| K 线 | `research/market/klines.py` | CSV 读取、对齐校验、只取收盘、聚合 5m/15m/1h/4h、缺口检测 |
| 指标 | `research/market/indicators.py` | SMA/EMA/MACD/RSI/ATR/ADX/Bollinger/量能 z/斜率/分位 |
| 状态机 | `research/market/state.py` | 趋势/波动/动量/风险 + 证据/冲突/失效条件/数据质量 |
| 准入 | `research/market/registry.py` | `horizon_match`、到期规则、resolution source，全部 fail-closed |
| 合成行情 | `research/market/demo.py` | 三种 regime（趋势/震荡/高波动），确定性 seed |
| CLI | `research/market/__main__.py` | `demo` / `analyze` |
| 测试 | `tests/market/` | 43 项，离线确定性 |

---

## 2. 数据流

```
1m K线 CSV ──► load_csv（对齐/去重/只取收盘）
                 │
                 ├─► resample(5m) ─┐
                 ├─► resample(15m) ─┤
                 ├─► resample(1h)  ─┼─► indicators ─► TimeframeView
                 └─► resample(4h)  ─┘
                                        │
                                        ▼
                              state.analyze → MarketState
                                        │
                                        ├─ evidence（支持）
                                        ├─ contradictions（冲突）
                                        ├─ invalidations（失效条件）
                                        └─ data_quality（缺口/陈旧）
```

**时间语义**沿用 Sprint 0 的三条硬规则：

1. 窗口与因果按**事件时间**；
2. 决策时刻 t 只使用 `close_ms <= t - settle` 的 K 线（未收盘的高/低/收还会变）；
3. 状态按事件时间取最新，迟到旧数据不得覆盖。

缺口处理是硬约束：聚合时一个 bucket 的分钟数不足或时间不连续就**丢弃**并记录；
分析前 `ensure_contiguous()` 遇到缺口直接抛错，不允许"看起来连续"。

---

## 3. 指标口径

| 指标 | 口径 | 前导 |
|---|---|---|
| EMA | SMA 种子，`k=2/(n+1)` | n−1 |
| MACD | EMA12−EMA26；信号 = EMA9(MACD)；柱 = 差 | 见序列 |
| RSI | Wilder 平滑 | 14 |
| ATR | Wilder，TR 从第 2 根起 | 14 |
| ADX | Wilder +DM/−DM/TR → DI → DX → ADX | 2n−1 |
| Bollinger | SMA20 ± 2σ（总体标准差） | 19 |
| 量能 z | 20 根窗口（含当前），std=0 → None | 19 |
| 斜率 | 20 根净变化 bps | 19 |

全部返回与输入等长的列表，前导 `None`——调用方永远可以把指标与 K 线按 index 对齐。

---

## 4. 状态机规则

### 4.1 趋势（主周期 1h）

| 条件 | 输出 |
|---|---|
| EMA20 > EMA60 > EMA200 且 ADX ≥ 20 | `TREND_UP` |
| EMA20 < EMA60 < EMA200 且 ADX ≥ 20 | `TREND_DOWN` |
| ADX < 20 | `RANGE` |
| 排列不完整且 ADX ≥ 20 | `RANGE`（按震荡处理） |
| EMA200/ADX 缺失（< 205 根） | `UNKNOWN` |

4h 只做一致性校验：1h 多头而 4h `EMA20<EMA60` → `1h_up_vs_4h_down` 冲突，扣 confidence。

### 4.2 波动（1h ATR%）

用 **ATR% 的历史分位**（最近 200 个 1h ATR%）而不是绝对阈值：
≥80 分位 `HIGH_VOL`，≤20 分位 `LOW_VOL`，其余 `NORMAL_VOL`；样本 <30 或缺失 → `UNKNOWN`。

### 4.3 动量（1h）

MACD 柱 > 0 且 RSI > 50 → `MOMENTUM_UP`；柱 < 0 且 RSI < 50 → `MOMENTUM_DOWN`；其余 `FLAT`。

### 4.4 风险

数据优先：`stale` 或存在 ≥3 个缺口 → `RISK_EXTREME`；有缺口/stale → `RISK_HIGH`；
否则按波动：`HIGH_VOL` → `RISK_HIGH`，`LOW_VOL` → `RISK_LOW`，其余 `RISK_NORMAL`。

### 4.5 confidence

基础 0.50；趋势明确 +0.15；4h 无冲突 +0.10；ADX ≥ 30 +0.10；动量同向 +0.05；
每个冲突 −0.10；有缺口 −0.10；stale 时上限 0.40；`UNKNOWN` 固定 0.15。范围 [0.05, 0.95]。

---

## 5. 准入（市场是否配进入研究）

三个条件，全部 fail-closed：

1. 未过期；
2. 到期时间可解析、`resolution_source` 非空；
3. **视界匹配**：`剩余到期时间 / primary_horizon <= 500`。

第 3 条是 Sprint 0 审计的直接落地：仓库现有的三个市场都是 2026 年底到期的长期事件，
与 5m 视界比值上万 → `tests/market/test_registry.py` 有一条测试专门断言它们被拒绝。
换成短周期市场后该测试会失败，提醒更新断言。

---

## 6. demo 输出（真实运行结果）

```
[trend_up]  TREND_UP / NORMAL_VOL / MOMENTUM_UP / RISK_NORMAL (conf=0.90)
            — 1h EMA 多头排列且 ADX=100.0
            证据 4 条；冲突 0；缺口 0；周期 [5m,15m,1h,4h]
            失效：ADX 跌破 20 / EMA20 下穿 EMA60 / ≥2 根 1h 缺口或陈旧 > 2h
[range]     RANGE / NORMAL_VOL / MOMENTUM_UP / RISK_NORMAL (conf=0.60)
            — 1h ADX=13.7 < 20，趋势强度不足
[high_vol]  RANGE / HIGH_VOL / MOMENTUM_UP / RISK_HIGH (conf=0.60)
            — 1h ADX=12.1 < 20，趋势强度不足；波动 HIGH_VOL
```

命令：

```bash
python -m research.market demo
python -m research.market demo --json
python -m research.market analyze --csv klines_1m.csv --as-of 1795881601000 --json
```

---

## 7. 明确限制

- **没有 L2 盘口 / 资金费 / 未平仓量**：这些在 `cross_market` 的 Binance 采集器里，
  Sprint 2 接线后作为额外证据进入 `MarketState`，本层不自己采。
- **没有新闻与 Polymarket**：Sprint 3 的输入，且默认 `advisory_only`。
- **4h 需要 60 根才可靠**（EMA60）；数据不足时 4h 仅参与"有值才校验"。
- **ATR% 分位是相对自身历史**：跨品种不可直接比较；换品种要重算窗口。
- **状态机是规则模型，不是概率模型**：confidence 是证据强度的加权，不是胜率。
  任何"用 confidence 当仓位"的做法都必须先过独立的样本外验证。

---

## 8. 给 Sprint 2 的接口

策略层只消费 `MarketState`：

```python
state.trend        # TREND_UP / TREND_DOWN / RANGE / UNKNOWN
state.volatility   # LOW_VOL / NORMAL_VOL / HIGH_VOL / UNKNOWN
state.momentum     # MOMENTUM_UP / MOMENTUM_DOWN / FLAT / UNKNOWN
state.risk         # RISK_LOW .. RISK_EXTREME
state.evidence     # tuple[Evidence]：code/timeframe/supports/value/threshold
state.contradictions
state.invalidations
state.data_quality # gaps / stale / age / complete
```

策略注册表据此判断"适用 / 不适用"，HOLD 时必须引用 `state.reason` 与
`state.invalidations`，不允许只写 "no signal"。`UNKNOWN` 状态下禁止开新仓——
不知道的时候，正确答案是不出手。