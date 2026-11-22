
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

## 研究循环 02:18:45

### 账本诊断

- deepseek_5m: orders=0 closed=0 realized=0 win=None reasons={}
- dual_5m: orders=0 closed=0 realized=0 win=None reasons={}
- rules_5m: orders=0 closed=0 realized=0 win=None reasons={}

### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |
|---|---|---|---|---|---|---|
| A_no_adx | 32 | 41.05 | 1.68 | 0.531 | 456.2 | {'STOP': 15, 'TP': 16, 'EARLY': 0, 'TIMEOUT': 1} |
| B_adx25 | 19 | 43.38 | 1.6 | 0.526 | 456.2 | {'STOP': 9, 'TP': 9, 'EARLY': 0, 'TIMEOUT': 1} |
| C_adx25_early | 41 | 25.23 | 1.63 | 0.293 | 428.19 | {'STOP': 2, 'TP': 11, 'EARLY': 27, 'TIMEOUT': 1} |

结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。

## 研究循环 03:49:07

### 账本诊断

- deepseek_5m: orders=0 closed=0 realized=0 win=None reasons={}
- dual_5m: orders=0 closed=0 realized=0 win=None reasons={}
- rules_5m: orders=0 closed=0 realized=0 win=None reasons={}

### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |
|---|---|---|---|---|---|---|
| A_no_adx | 32 | 40.72 | 1.68 | 0.531 | 456.2 | {'STOP': 15, 'TP': 16, 'EARLY': 0, 'TIMEOUT': 1} |
| B_adx25 | 19 | 42.82 | 1.59 | 0.526 | 456.2 | {'STOP': 9, 'TP': 9, 'EARLY': 0, 'TIMEOUT': 1} |
| C_adx25_early | 42 | 23.02 | 1.57 | 0.286 | 478.35 | {'STOP': 2, 'TP': 11, 'EARLY': 28, 'TIMEOUT': 1} |

结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。

## 研究循环 05:19:28

### 账本诊断

- deepseek_5m: orders=0 closed=0 realized=0 win=None reasons={}
- dual_5m: orders=0 closed=0 realized=0 win=None reasons={}
- rules_5m: orders=0 closed=0 realized=0 win=None reasons={}

### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |
|---|---|---|---|---|---|---|
| A_no_adx | 32 | 38.65 | 1.62 | 0.5 | 494.39 | {'STOP': 15, 'TP': 16, 'EARLY': 0, 'TIMEOUT': 1} |
| B_adx25 | 19 | 39.33 | 1.52 | 0.474 | 494.39 | {'STOP': 9, 'TP': 9, 'EARLY': 0, 'TIMEOUT': 1} |
| C_adx25_early | 43 | 19.88 | 1.49 | 0.256 | 542.11 | {'STOP': 2, 'TP': 11, 'EARLY': 29, 'TIMEOUT': 1} |

结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。

## 研究循环 06:49:43

### 账本诊断

- deepseek_5m: orders=0 closed=0 realized=0 win=None reasons={}
- dual_5m: orders=0 closed=0 realized=0 win=None reasons={}
- rules_5m: orders=0 closed=0 realized=0 win=None reasons={}

### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |
|---|---|---|---|---|---|---|
| A_no_adx | 32 | 38.36 | 1.62 | 0.5 | 498.54 | {'STOP': 15, 'TP': 16, 'EARLY': 0, 'TIMEOUT': 1} |
| B_adx25 | 19 | 38.85 | 1.52 | 0.474 | 498.54 | {'STOP': 9, 'TP': 9, 'EARLY': 0, 'TIMEOUT': 1} |
| C_adx25_early | 41 | 23.49 | 1.58 | 0.268 | 435.99 | {'STOP': 2, 'TP': 11, 'EARLY': 27, 'TIMEOUT': 1} |

结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。

## 研究循环 08:19:54

### 账本诊断

- deepseek_5m: orders=0 closed=0 realized=0 win=None reasons={}
- dual_5m: orders=0 closed=0 realized=0 win=None reasons={}
- rules_5m: orders=0 closed=0 realized=0 win=None reasons={}

### 回测（真实 1h × 500，含 10bps 双边成本，70/30 时间切分仅作参考）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps | 退出分布 |
|---|---|---|---|---|---|---|
| A_no_adx | 32 | 39.85 | 1.66 | 0.531 | 457.1 | {'STOP': 15, 'TP': 16, 'EARLY': 0, 'TIMEOUT': 1} |
| B_adx25 | 19 | 41.35 | 1.57 | 0.526 | 458.03 | {'STOP': 9, 'TP': 9, 'EARLY': 0, 'TIMEOUT': 1} |
| C_adx25_early | 42 | 22.17 | 1.54 | 0.286 | 504.11 | {'STOP': 2, 'TP': 11, 'EARLY': 28, 'TIMEOUT': 1} |

结论：仅记录，未修改任何运行中的策略；样本 <100 笔时一律 INCONCLUSIVE。
