# 最有希望的候选（基于最近一次回测）

- 变体：**B_adx25**
- 指标：{"n": 19, "net_bps": 41.35, "pf": 1.57, "win": 0.526, "dd_bps": 458.03, "reasons": {"STOP": 9, "TP": 9, "EARLY": 0, "TIMEOUT": 1}}
- 样本：19 笔；< 100 笔时结论一律 INCONCLUSIVE
- 局限：单段历史数据、未做 purged walk-forward 的多次重复检验、未含资金费与盘口冲击
- 处理：不提升 VERIFIED、不热更新运行策略；仅记录为下一轮候选
