# S3 契约草案 — BTC/ETH/SOL 跨币种 Beta 残差配对（未锁、未跑）

分支 `audit/v3-acceptance-fixes`。**草案状态：未锁契约、未写残差模块、未跑批。**
日期 2026-11-25。三币 3 年数据已到位（`data/v3_market/`，各 26,298 根 1h + 3,287 条 funding，
零缺口），S3 数据前提已满足。按用户选 A 的指令：先把 S2b BTC 1h 跑完，再起草本草案供审阅。

成本与瘸腿的详细设计已在 `S3_pair_cost_plan.md`（本草案不重复，只引用并补契约骨架）。

---

## 一、经济机制（先讲清谁在付钱）

三币高度相关，共同受大盘 Beta 驱动。局部流动性冲击（某币被单独砸盘/逼空）会让
该币相对其它币的**Beta 调整后残差**短暂偏离，随后均值回归。

付钱方 = 造成局部冲击的被动流动性（止损/强平/大额市价单）；
我们赚的是**相对定价错误的回归**，不是预测大盘方向。

**与 E4/E5/S1 的本质区别**：那些是"费率之后 BTC 会不会涨跌"（绝对方向），
S3 是"同刻费率更贵的币是否相对更弱"（横截面相对定价）。剥离 Beta 后，
大盘涨跌不再是收益来源。

**不成立的情形**（必须 FLAT）：
- 残差偏离由独立基本面事件造成（链上宕机、ETF 审批、下架）——需 intel 事件否决
- 残差持续扩大不回归（结构性重定价，不是流动性冲击）
- 两腿无法同时成交（瘸腿 → L2 强平，见成本方案）

## 二、Beta 残差口径（需新增模块，现成代码里没有）

核实结果：`research/cross_market/` 已有成本模型、时间戳对齐（2s 容差）、
block bootstrap（20 根）、A/B/C 实验框架与 `FalsificationAdapter`，
**但没有残差/Beta 计算**——S3 必须新增 `research/v4/s3_residual.py`（草案，未写）。

```
窗口 W=168 根 1h（7 天），滚动估计：
  r_i(t) = ln(P_i(t) / P_i(t-1))              各币对数收益
  r_BTC(t) 为市场因子
  beta_i(t) = Cov(r_i, r_BTC) / Var(r_BTC)     仅用 t 之前 W 根（严格不含当根）
  residual_i(t) = ln(P_i(t)) - beta_i(t)·ln(P_BTC(t))   ← 价格空间残差
  z_i(t) = (residual_i(t) - mean(residual_i, W)) / std(residual_i, W)
```

**防前视铁律**：
- beta 与残差只用 `t-W : t` 的已收盘数据，信号在 bar t 收盘生成
- 成交在 bar t+1 开盘（无前视撮合，沿用 E2/R0 口径）
- 时间戳用 fapi 返回的 `t`（服务端时间），不是本地接收时间
- 三币 K 线必须按 `t` 严格对齐（缺口为零已验证），任一币缺根 → 该时刻跳过

**为什么用价格空间残差而非收益残差**：残差回归交易的是"价格关系的恢复"，
价格空间残差有明确均值回归锚点；收益残差是平稳的但没有回归目标。

## 三、入场 / 退出（阈值事前写死，等审阅通过才锁）

| 项 | 值 | 依据 |
|---|---|---|
| 数据 | BTC/ETH/SOL 1h，3 年，三币对齐 | 已回补 |
| 配对 | ETH-BTC、SOL-BTC 两组独立 | 两组各自计数 |
| 残差窗口 W | 168 根（7 天） | 覆盖一个完整周周期，非优化 |
| 入场 | `\|z_i\| ≥ 2.0` 且事件否决通过 | 极端偏离，避免高频换手 |
| 方向 | z>0 做空超涨腿+做多 BTC；z<0 反向 | 市场中性（Beta 已剥离） |
| 退出 | z 回归到 \|z\|≤0.5 或 timeout 24 根（1 天）或止损 2.0×残差std | 回归/超时/风控三层 |
| 单仓 | 每组独立单仓；同方向两组合并敞口 ≤ 权益 2% | 防相关叠加 |
| cooldown | 6 根（6h） | 防同族串仓 |
| 事件否决 | intel 重大事件窗口 ±4h 内 FLAT | 防基本面结构性重定价 |
| 成本 | 双腿往返 **36bps**（16 手续费 + 20 滑点） | `S3_pair_cost_plan.md` |
| 瘸腿 | L0/L1/L2 三口径，**L2 拒单后仍为正**才算通过 | 同上 |
| fee_multiples | [1,2,5]（2x=72bps，5x=180bps） | 同上 |

**毛利门槛**：≥120bps（双腿），事前写死。理由见 `S3_pair_cost_plan.md`：
80bps 在 2x 下只剩 8bps，基本白干。

## 四、证伪契约骨架（未锁）

```
name        : s3-residual-pair-{ETH,SOL}-1h
symbol      : ETHUSDT / SOLUSDT（相对 BTCUSDT）
interval    : 1h, venue futures, fee_rate 0.0004, fill_model taker
funding     : true（双腿逐次结算，符号相反部分对冲）
exit        : z回归0.5 / timeout24 / stop2.0σ
signals     : csv（含 t, side, hedge_beta, z, leg_b_fill_prob）
control     : enabled, n_random=2000, seed=20261125
              （随机对照必须保留配对结构：随机选时刻但保持双腿与beta中性，
                否则不是有效对照）
walk_forward: enabled, folds=5
param_grid  : z_entry [1.5,2.0,2.5] × z_exit [0.3,0.5,0.8] = 9 格
fee_multiples:[1,2,5]
falsify_if  : 与 S1/S2/S2b 一致五条 +
              legging_l2_net_positive（新增：L2 拒单口径下必须仍为正）
```

**新增判据说明**：`legging_l2_net_positive` 是 S3 特有的硬门槛——
单腿成交后另一腿拒单必须强制平掉，若这样还亏到负，说明策略依赖"两腿永远成交"的
虚假假设。这条事前写进 `falsify_if`，不事后加。

## 五、与现成模块的映射（避免重复造轮子）

| 需求 | 现成能力 | 需新增 |
|---|---|---|
| 双腿成本分解 | `cross_market/cost_model.py::evaluate_cost` + `funding_drag_bps` | — |
| 三币时间戳对齐 | `cross_market/timestamp.py::align`（2s 容差） | — |
| block bootstrap CI | `cross_market/experiment.py::block_bootstrap_ci`（block=20） | — |
| A/B/C 消融 | `cross_market/experiment.py::run_abc` + `FalsificationAdapter` | — |
| 证伪流水线 | `research.verify`（已修两个防呆 Bug） | — |
| **Beta / 残差** | 无 | **`research/v4/s3_residual.py`** |
| **三腿撮合+L2** | 无 | **`research/v4/s3_pair_sim.py`** |

## 六、验收补充（E5 血泪条，人工执行）

- 剔除 top1/2/3 盈利笔后仍为正
- 自然季度 ≥6/8 为正
- walk-forward 最后一折独立，每折 fills ≥6
- ETH-BTC 与 SOL-BTC 两组**分别**判定，不许合并凑样本
- L0/L1/L2 三口径全部跑，报告并列，不许只报 L0
- 多重检验：当前家族数 9 + S2b F/N 2 = 11；S3 两组再加 2 → 13

## 七、当前状态（严格遵守"只草案"）

| 项 | 状态 |
|---|---|
| 三币数据 | ✅ 已回补（3 年零缺口） |
| 经济机制 / 残差口径 | ✅ 本草案已写 |
| 成本与瘸腿方案 | ✅ `S3_pair_cost_plan.md` 已有 |
| 契约骨架 | ✅ 本草案（未锁） |
| `s3_residual.py` | ❌ 未写 |
| `s3_pair_sim.py` | ❌ 未写 |
| 信号 / 回测 | ❌ 未跑 |
| 契约锁 | **故意不锁**——等用户审阅本草案、确认口径后再锁 |

**下一步依赖**：用户审阅本草案 → 确认或修正残差口径/门槛 → 再锁 ETH-BTC 与 SOL-BTC
两份契约 → 写两个新增模块 → 跑 verify + 随机对照。
本轮不锁、不跑、不写模块。
