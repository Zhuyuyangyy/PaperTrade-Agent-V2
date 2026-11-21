
## 研究循环 00:48:24

### 账本诊断

- deepseek_5m: orders=0 closed=0 realized=0 win=None reasons={}
- dual_5m: orders=0 closed=0 realized=0 win=None reasons={}
- rules_5m: orders=0 closed=0 realized=0 win=None reasons={}

### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |
|---|---|---|---|---|---|---|
| A_no_adx | 33 | 36.61 | 1.59 | 0.515 | 570.55 | {'STOP': 16, 'TP': 16, 'EARLY': 0, 'TIMEOUT': 1} |
| B_adx25 | 20 | 35.94 | 1.48 | 0.5 | 571.48 | {'STOP': 10, 'TP': 9, 'EARLY': 0, 'TIMEOUT': 1} |
| C_adx25_early | 43 | 21.31 | 1.52 | 0.279 | 565.21 | {'STOP': 2, 'TP': 11, 'EARLY': 29, 'TIMEOUT': 1} |

结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。
