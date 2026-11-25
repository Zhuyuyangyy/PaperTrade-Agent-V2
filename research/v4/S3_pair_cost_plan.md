# S3 — BTC/ETH/SOL 跨币种残差配对：成本与瘸腿测试方案

分支 `audit/v3-acceptance-fixes`。**只设计，未实现，未跑回测，未接实盘。**

---

## 一、为什么这个方向对成本最敏感

双腿 = 两次手续费 + 两次滑点 + 两次资金费，且**两条腿不一定同时成交**。
长文本给的毛利目标 80–150bps（双腿）是合理的，但要先把成本算清楚：

| 成本项 | 单腿 | 双腿往返 | 来源 |
|---|---|---|---|
| Taker 手续费 | 4bps × 2 = 8bps | **16bps** | 冻结口径 |
| 滑点（不利方向） | 5bps × 2 = 10bps | **20bps** | 冻结口径 |
| 资金费 | 逐次真实结算 | 做多/做空腿符号相反，部分对冲 | `funding.json` |
| **合计基础往返** | 18bps | **36bps** | — |
| 2x 压力 | — | **72bps** | `fee_multiples` |
| 5x 压力 | — | **180bps** | `fee_multiples` |

**结论先行**：36bps 基础成本下，80bps 毛利只剩 44bps 净；
2x 成本下只剩 8bps；5x 直接负。
所以 S3 的毛利门槛必须**事前抬到 ≥120bps**，而不是长文本下沿的 80bps。
这是从 R0 的成本敏感性表推出来的，不是拍脑袋。

## 二、瘸腿（Legging）风险：最容易被忽略的亏损源

配对交易的两条腿在真实市场不会原子成交。三种情形必须分别建模：

| 情形 | 定义 | 保守处理 |
|---|---|---|
| L0 理想 | 两腿同一根收盘价成交 | **只作参照，不作验收依据** |
| L1 单根错位 | A 腿在 bar i 成交，B 腿在 bar i+1 成交 | B 腿用 bar i+1 **开盘 + 不利滑点**；A 腿持仓期间按单腿裸露头寸计风险 |
| L2 拒单 | B 腿限价未成交，只成交 A 腿 | **强制按单腿止损平掉 A**，记一笔亏损；不允许"等等看" |

**L2 是致命项**：如果回测里 B 腿永远成交，这个策略的净值就是假的。
必须在信号 CSV 里带 `leg_b_fill_prob` 字段，并按最坏口径（拒单率 20%）跑一遍。

### 敞口时间政策

| 参数 | 值 |
|---|---|
| 最大错开时间 | 1 根 4h（4 小时） |
| 超过则 | 视为 L2，平掉已成交腿 |
| 单腿裸露头上限 | ≤0.5% 权益 |

这是风险约束，不是收益优化——不许因为"多等一根能成交"而放宽。

## 三、时间戳对齐（已有能力，直接用）

`research/cross_market/timestamp.py::align(a, b, tolerance_ms=2000)` 现成。
三币种 K 线必须全部对齐到 UTC 4h 边界，tolerance 2 秒。
对齐失败 → 该 bar 不产生配对信号，**不用前向填充**（前向填充 = 前视）。

## 四、可复现依赖（已有能力）

| 能力 | 模块 |
|---|---|
| 成本分解 `EV_net = EV_gross − fee − spread − slippage − latency − impact − funding` | `research/cross_market/cost_model.py::evaluate_cost` |
| 资金费拖累 | 同模块 `funding_drag_bps` |
| block bootstrap CI | `research/cross_market/experiment.py::block_bootstrap_ci` |
| 证伪适配 | 同模块 `FalsificationAdapter` |

**注意**：`replay/labels.py` 不是这个用途——它是 Meme 投币标签库（mint/quote_mints），
长文本里"用 labels.py 做候选消融"的说法在本仓库不成立。
候选消融要用 `verify` 的 control + `param_grid`，或新写配对专用对照组。

## 五、成型规则（待锁契约）

残差定义（无前视）：

```
ret_i(t) = log(close_i(t) / close_i(t-1))
beta_i   = 滚动 60 根 OLS(ret_i, ret_BTC)，window 不含当次
resid_i(t) = ret_i(t) - beta_i × ret_BTC(t)
z_i(t)   = 滚动 120 根 z-score(resid_i)，同样不含当次
```

入场（两腿同时）：

```
z_i ≥ +2.0  → 做空超涨腿 i / 做多 BTC（等 beta 名义）
z_i ≤ −2.0  → 做多超跌腿 i / 做空 BTC
```

退出：
- 残差回归 `|z| ≤ 0.5` → 平仓
- 止损：`|z| ≥ 3.5` → 认错（残差继续发散）
- 超时：24 根 4h（4 天）
- 重大事件否决：复用 `research/intel/` 摘要，但 `advisory_only` 不直接改仓——只作日志过滤

**禁止**：BTC/ETH/SOL 三币两两组合全扫。那是 3 对 × 多参数 = 隐性多重比较。
本版**只做 ETH-BTC 一对**；SOL 留作样本外确认。若 ETH-BTC 不过，不试 SOL。

## 六、判定（事前）

| 规则 | 阈值 |
|---|---|
| min_fills | 30 |
| control_lift_ci_includes_zero | true（随机时机对照） |
| net_negative_at_fee_multiple | 2 |
| worst_case_net_negative | true（含 L1/L2 瘸腿） |
| grid_positive_ratio_min | 0.5 |
| **新增：L2 拒单后仍为正** | 必须（否则瘸腿一击致命） |

最后一条引擎不原生支持，需要单独脚本输出，作为人工验收项写进报告。

## 七、当前状态与下一步

已做：读 `cost_model.py` / `timestamp.py` / `experiment.py` 源码；算出双腿真实成本与瘸腿口径。
未做：契约锁、三币种对齐数据、信号 CSV、verify 跑批。

**优先级判断**：S3 的成本基础（36bps）是 S1/S2 的两倍，且多了瘸腿风险。
即使 ETH-BTC 有统计显著残差，扣完成本也很可能不合格。
建议排在 S1、S2 之后——**先用最便宜的实验把"有没有空间"问完**。

如果 S1/S2 都倒下，S3 的边际价值也很低，不必硬做。
