# research/polymarket：Phase 0 只读盘口探测

零资金、零下单、零密钥。只回答一个问题：**个人用普通 REST 轮询，在 Polymarket 上
到底能不能吃到套利**——不是"中间价看起来有没有"。

```bash
python research/polymarket/arb_probe.py snapshot                 # 一次全市场快照
python research/polymarket/arb_probe.py probe --minutes 10       # 10 分钟高频探测
python research/polymarket/arb_probe.py tail --max-hours 48      # 尾盘折价（<48h，0.90-0.995）
python research/polymarket/arb_probe.py calibrate --markets 1500 # 已结算市场校准
```

产物在 `data/`：`snapshot_*.json`、`probe_*.json`（汇总）、`opps_*.jsonl`（逐轮机会观测）、
`tail_*.json`、`calibration_*.json`。结论见 [`PHASE0_report_2026-11-25.md`](PHASE0_report_2026-11-25.md)。

---

## 1. 真实接口行为（全部实测，不是文档抄的）

| 事实 | 细节 |
|---|---|
| Gamma 分页要 gzip | `limit>50` 时**必须**带 `Accept-Encoding: gzip`，否则 406 `gzip_required` |
| 直连比代理快 4 倍 | 同一请求 2.2s vs 9.4s；代码里显式 `ProxyHandler({})` |
| 批量盘口 | `POST /books` 传 `[{"token_id":...}]`，200 个 token 约 0.9s（5.6ms/本），比逐个 GET 快 300 倍 |
| **bids 是升序的** | `bids[0]` 是**最差**买价，`asks[0]` 是最优卖价；代码统一 `max(bids)/min(asks)` 后再排序 |
| 费率 | `GET /fee-rate?token_id=` 返回 `{"base_fee":1000}`，单位文档未给；官方费率表是 0.01/0.05 两档，地缘政治 0 |
| 手续费公式 | `fee = C × feeRate × p × (1-p)`，逐档累加，maker 永远 0 |
| 最小下单 | `orderMinSize` 通常 5 份，`orderPriceMinTickSize` 0.001 或 0.01 |

`/fee-rate` 的 `base_fee=1000` 与官方 0.01/0.05 的表面矛盾没有解决，
所以扫描器**不猜单一值**：每个机会同时给 feeRate ∈ {0, 0.01, 0.05} 三档净利。

## 2. 完备性陷阱（本轮最重要的发现）

NegRisk 事件里，**Σ best_ask 远小于 1 通常不是套利，是结果集不完整**。

实测样本：

| 事件 | 腿数 | Σ ask | Σ mid | 判定 |
|---|---|---|---|---|
| AFC Asian Cup 2027: Group F Winner | 4 | 0.116 | 0.0955 | 缺腿 |
| Pro Football: Tyreek Hill Next Team | 32 | 0.912 | 0.6405 | 缺腿 |
| How many more Millennium Prize Problems | 6 | 0.978 | 0.9555 | 缺腿 |
| Sporting CP vs Manchester United | 2 | 1.000 | 0.985 | 完备 |
| Ljubljana Mayoral Election Winner | — | 1.000 | 0.990 | 完备 |

判据：`Σ mid ∈ [0.97, 1.03]` 才算完备集。Σ mid 显著低于 1，说明有结果没列出来
（或报价已经失效），此时买齐 YES 的期望赔付不是 $1，**表观价差全部是假的**。

扫描器把这批单独计数、单独列，**绝不进"可吃"统计**。

## 3. 两类机会怎么算

### NegRisk 完备集
1. 取事件全部市场的 YES token，批量拉盘口
2. `Σ best_ask < 1` 且 `Σ mid ∈ [0.97,1.03]`
3. 同一份数 `n` 买齐所有腿（**不是每腿买 $100**——合成 $1 要求等份数）
4. `max_deployable = n_max × Σ 均价`，`n_max` 取最短腿的深度（木桶）
5. `profit = n - Σcost - Σfee`，对 $100/$500/$1000 各解一次

### 二元 YES+NO
同一市场买 YES 和 NO 各 n 份，`ask(Y)+ask(N) < 1` 即可合成 $1，算法同上。

## 4. 存活时长怎么量

- 每轮记录机会首次/末次出现的时间戳，`duration = 末见 - 首见 + 一个扫描周期`
- 扫描周期中位数 ~1.8s（1,307 个热点 token）
- **能力边界**：低于 ~2s 的闪现无法可靠区分，所以只把 `>= 3s` 且连续多轮命中的
  判为"个人脚本可能来得及"；`<1s` 的即使存在，我们也看不见——报告里必须说明这一点，
  不能假装测到了

## 5. 热点是怎么选的（预筛 ≠ 计算）

全市场 1,200 个事件、31,778 个市场、63k token，全量轮询要 60s+，测不出秒级存活。
所以先用 Gamma 的 `bestAsk/bestBid` 粗筛出一个热点集：

- negRisk：`Σ bestAsk <= 1.02`
- 二元：`流动性 >= $5,000` 且 `ask(Y) + (1 - bid(Y)) <= 1.005`

**预筛值只决定先看谁，不参与任何套利计算**。所有净利、深度、存活都来自 CLOB 真实盘口。

实测热点约 1,300 个 token，一轮 1.5–1.8s。

## 6. 已知边界

- **REST 轮询，不是 WebSocket**：真实存活 0.2–0.5s 的机会在两次轮询之间就消失了，
  本轮报出的"没有机会"严格说是"没有持续 >= 2s 的机会"
- **不含链上执行成本**：Gas、USDC 转账、NegRiskAdapter 转换、滑点衰减都没算；
  这些是"有毛利之后"才需要讨论的
- **不含分辨率风险**：UMA 争议、规则歧义、事件被作废都不在价格里
- **不碰资金**：没有私钥、没有钱包、没有任何下单代码
