# 最有希望的候选（基于最近一次回测）

- 变体：**A_no_adx**
- 指标：{"n": 0, "net_bps": 0.0, "pf": null, "win": null, "dd_bps": 0.0}
- 样本：0 笔；< 100 笔时结论一律 INCONCLUSIVE
- 局限：单段历史数据、未做 purged walk-forward 的多次重复检验、未含资金费与盘口冲击
- 处理：不提升 VERIFIED、不热更新运行策略；仅记录为下一轮候选
