# Alpha Discovery V1 — 真实情报接线与增量验证

> 状态：**WIRING DONE / ADVISORY ONLY**。情报已经能真实采集并写进决策日志，
> 但**不改变** `Selection`、`RiskDecision` 或 `VERIFIED`。唯一入口仍是消融实验的
> `SUPPORTED` 结论。

---

## 1. 已接线的真实数据源

| 源 | 端点 | 状态 | 实测（2026-11-21） |
|---|---|---|---|
| 新闻 RSS | cointelegraph / decrypt / theblock | 可用 | 一次采集 27 条（去重后），首次可见时间=采集时刻 |
| Polymarket 市场 | Gamma `/markets` | 可用 | 9 个 BTC 短周期市场，ABOVE/BELOW 方向映射正确 |
| Polymarket 盘口 | CLOB `/book` | 可用但慢 | 部分市场点差 800–8000bps → `WIDE_SPREAD`，不研究 |
| DeepSeek | `/chat/completions` | **无 key** | 回退规则抽取，warnings 写 `LLM_UNAVAILABLE` |
| 聪明钱 | 历史钱包 trades | **源未接** | warnings 写 `WALLET_SOURCE_UNAVAILABLE`，契约与可复制性已就绪 |

所有外部调用都可注入（`fetch` / `gamma_fetch` / `book_fetch` / `llm_fetch`），
测试零网络；单源失败只记 warning，不阻塞其它源。

---

## 2. 采集管线

```
feeds (RSS) ──► FeedItem ──► LLM 抽取（无 key → 规则回退）──► NewsStore（去重/时间/注入标记）
Gamma ──► select_btc_markets（BTC、1..7 天、active、有 YES/NO）
      ──► CLOB book（可选）──► PredictionMarketEvent（方向/点差/质量/可研究性）
钱包 trades ──► WalletSignal（可复制性）  [源待接]
                    │
                    ▼
               IntelDigest ──► 决策日志 intel 段（advisory_only=True）
```

`python -m research.runtime live` 默认每轮采集并写入日志；`--no-intel` 关闭，
`--intel-books` 额外拉盘口（更慢）。

---

## 3. 五类特征（T / N / P / W / L）

`research/intel/features.py`，全部归一化到 [-1, 1] 或 [0, 1]：

| 类别 | 特征 | 说明 |
|---|---|---|
| T 技术 | trend / momentum / adx / rsi / atr_pct | 来自 `MarketState.features`，不重算 |
| N 新闻 | bias / count / confidence / injection | 方向×置信度加权；注入标记单独一维 |
| P 预测市场 | bias / researchable / spread / delta | YES 概率变化按 ABOVE/BELOW 映射到标的方向 |
| W 聪明钱 | bias / copyable / count | 仅用可复制信号；不可复制降权 |
| L 微观结构 | bias / volume_z / atr_pct | 当前只有成交量与波动，L2/OI/资金费待接 |

综合得分 `S = Σ w·F`。**权重必须由训练数据学习并在未见数据上验证**，
当前不提供任何"看起来合理"的默认权重用于交易。

---

## 4. 消融实验（唯一验收）

`research/intel/ablation.py`：

- 基线 `T`（纯技术）；变体 `T+N`、`T+P`、`T+W`、`T+L`、`T+ALL`；
- 同一批决策点、同一标签，逐点配对；
- 指标：净 bps、命中率、Profit Factor、最大回撤；
- 与基线的 Block Bootstrap 95% CI；样本 < 30 → INCONCLUSIVE；
- 结论只对这批决策点成立，**不外推**。

没有 `SUPPORTED`，情报就只是公示信息；不允许为了增加交易次数把它接进决策。

---

## 5. 真实运行截图（2026-11-21 22:24 CST）

```
intel @ ...: news=27, markets=9, wallets=0, advisory_only=True
warnings: LLM_UNAVAILABLE / WALLET_SOURCE_UNAVAILABLE / MARKETS_NOT_RESEARCHABLE
selection: LONG breakout_confirm (shadow)
```

- 27 条新闻全部来自真实 RSS，规则抽取方向（LLM 无 key）；
- 9 个市场方向映射正确，但盘口宽点差 → 当前没有可研究的 P 特征；
- 决策与无情报时完全一致（有测试保证）；实验盘影子成交 1 笔。

---

## 6. 必须说清的限制

1. **没有 DeepSeek key**：LLM 抽取未真实验证；规则回退只按关键词，置信度低。
   配好 key 后先跑 `extract` 的单元测试与 20 条抽样人工核对。
2. **聪明钱源未接**：`data-api.polymarket.com/trades` 返回 410；需要新的历史 trades
   端点或链上索引。契约与可复制性逻辑已就绪。
3. **P 特征当前为空**：短期 BTC 市场深度价外、点差极宽；需要筛更活跃/平值的市场。
4. **样本不足**：目前没有足够样本跑消融，任何"情报有用"的说法都不成立。
5. **L2 盘口/OI/资金费**尚未进特征；`cross_market` 有采集器，接线后再加 `L` 维度。

---

## 7. 下一步（Spark 上）

```bash
export DEEPSEEK_API_KEY=...
python -m research.runtime live --rounds 73 --interval-ms 3600000 \
  --root data/openalpha_forward_v1 --intel-books
```

- 每轮日志记录 `intel`（news/markets/warnings）；
- 累积 ≥ 数百决策点后，用 `ablation` 跑 T 与 T+N/P/W/L 的净收益对比；
- 只有 `SUPPORTED` 的类别才进入 "候选策略" 讨论，且仍需独立复现；
- 在此之前，Forward Test 继续用纯技术策略 + 双账本记录，不用情报改方向。