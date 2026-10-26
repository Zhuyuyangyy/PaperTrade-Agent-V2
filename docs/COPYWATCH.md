# CopyWatch · 链上跟单模拟实验（D 账户）

D 账户是一个**事件驱动**的独立虚拟账户。它不和 A/B/C 的均线策略共享任何账本、资金或信号。

## 1. 它为什么存在

A/B/C 是低频趋势机器人：BTC 现价低于 SMA20/SMA50 时正确行为就是 HOLD，所以「一直不动」是策略设计的产物，不是故障。

D 换的是信号来源：

| 维度 | A/B/C | D · CopyWatch |
| --- | --- | --- |
| 信号来源 | SMA / EMA / RSI 指标 | 真实链上 swap 事件 |
| 触发条件 | K 线收盘 + 指标成立 | 被跟踪钱包发生买入 |
| 更新频率 | 1h / 15m | 45s 轮询（受公链出块约束） |
| 无信号时 | 页面几乎不变 | 仍有 WATCH / SKIP 事件流 |
| 资金 | 各 1,000 USDT | 独立 500 USDT |

## 2. 数据来源

- `https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/trades`
- 无需 API key，只读，无法签名或转账。
- 每条事件含：`tx_hash`、`block_number`、`block_timestamp`、`tx_from_address`（钱包）、
  `kind`(buy/sell)、`volume_in_usd`、`price_to_in_usd`、`is_anomalous`。
- 默认两个池子（WETH/USDC，真实地址已核对）：

| 网络 | 池子 | 用途 |
| --- | --- | --- |
| base | `0x72ab388e2e2f6facef59e3c3fa2c4e29011c2d38` | 低成本链，$100 名义额可经济成交 |
| eth | `0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640` | 高流动性主网，用于验证「成本过滤真的会拒绝」 |

**没有可访问的事件源时，账户显示「等待数据」，不产生任何模拟成交。**

## 3. 「核验」的定义

这里**不是**身份、声誉或收益核验，而是**链上可验证的活跃度**：

- 被观察钱包在同一链上至少 3 笔成交，且累计名义额 ≥ 2,000 USD → 升级为 `tracked`，其买入可被跟单；
- 单笔 ≥ 20,000 USD 的真实买入 → 标记为 `whale` 信号类（无历史记录，但事件本身链上可查）；
- 其余钱包只记入观察列表，永不跟单（`WALLET_UNVERIFIED`）。

被跟踪的钱包地址、成交笔数、累计名义额全部可在 `wallets` 表与 `swaps` 表里回溯到具体 tx hash。

## 4. 风控顺序（任一不通过即 SKIP，且写入原因码）

| # | 代码 | 判定 |
| --- | --- | --- |
| 1 | `WALLET_UNVERIFIED` | 未达跟踪门槛且非巨鲸单 |
| 2 | `ANOMALOUS` | 数据源 `is_anomalous=true` |
| 3 | `NO_FRESH_PRICE` | 池价缺失或超过 180s |
| 4 | `POOL_RESERVE` | 池子储备 < 250,000 USD |
| 5 | `PRICE_IMPACT` | 名义额占储备 > 5 bps |
| 6 | `COST_TOO_HIGH` | 往返成本 > 60 bps |
| 7 | `INSUFFICIENT_CASH` | 虚拟现金不足 |
| 8 | `MAX_POSITIONS` | 同时持仓 ≥ 2 |
| 9 | `ALREADY_OPEN` | 同标的已有仓位 |
| 10 | `COOLDOWN` | 距上次开仓 < 20 分钟 |
| 11 | `DAILY_LOSS` | 当日已实现亏损 ≤ -30 USDT |

## 5. 成交与成本口径

```
往返成本(bps) = 2 × (池子手续费 + 滑点) + 2 × gas / 名义额 × 10000
```

- 手续费：池子费率（base 1 bps / eth 5 bps）；
- 滑点：固定 5 bps，**入场加价、出场减价**；
- gas：`gas_usd` 每边各计一次（base 0.02 / eth 2.4）。

示例（名义额 100 USDT）：

| 网络 | 往返成本 | 结果 |
| --- | --- | --- |
| base | ≈ 16 bps | 通过 |
| eth | ≈ 490 bps | `COST_TOO_HIGH` 拒绝 |

这不是缺陷，而是结论：**$100 级别的跟单在以太坊主网被 gas 吃掉，只有在 L2 上才有意义。**

## 6. 记账口径

- `PAPER 成交价 = 发现时刻池价 × (1 + 5bps)`，**不是**被跟钱包的成交价；
- 每个仓位记录 `drift_bps = (发现价 − 源成交价) / 源成交价`，跟单延迟的代价因此可量化；
- `cash = 初始资金 − 未平仓成本 + 已实现盈亏`；
- `持仓净值 = qty × mark × (1 − 5bps) − 手续费 − gas`（按立即清算估）；
- `equity = cash + Σ 持仓净值`；任一持仓缺新鲜标记 → `equity = None`（fail-closed，不猜）；
- 退出条件在**含成本的净收益**上判定：止盈 +120 bps / 止损 −80 bps / 45 分钟超时。

## 7. 运行

主机（只读公网数据，不占端口）：

```bash
python -m research.copywatch.agent once     # 单轮
python -m research.copywatch.agent loop     # 常驻，45s 轮询
python scripts/copywatch.py status          # 权益/持仓/跳过分布
python scripts/copywatch.py wallets         # 观察与跟踪钱包
python scripts/copywatch.py events          # 事件流
python scripts/copywatch.py promote <addr> --network base   # 手工纳入跟踪（只改本地列表）
```

Docker（与其余服务同一镜像，无端口暴露，`read_only` + `cap_drop: ALL`）：

```bash
docker compose up -d copywatch
```

账本：`runtime/copywatch/copywatch.sqlite`（已被 `.gitignore` 排除，不进 Git）。
导出可追溯快照请用 `scripts/export_experiment.py`。

## 8. 已知限制（不要当成已解决）

1. 公开 API 限流约 30 次/分钟，池子越多轮询越慢；
2. 只取每个池子最近 300 条成交，停机超过窗口会漏事件（漏掉的事件不会被补造）；
3. 无法回放历史：D 的权益曲线只能从首次运行往后积累；
4. 跟单延迟是真实存在的成本，`drift_bps` 只度量不消除；
5. 没有做钱包标签（CEX 热钱包、MEV bot、做市商）；被跟踪的「活跃钱包」很可能就是做市机器人，
   跟它们的成交方向没有alpha含义——这是当前最大的模型缺陷；
6. 未实现：按总权益熔断、退出滑点分布估计、多链跨池同一钱包聚合。
