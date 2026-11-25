# S2b（BTC 1h）执行报告 — F/N 双契约 INCONCLUSIVE；OOS 双负、成本 2x 即崩

分支 `audit/v3-acceptance-fixes`。**未接实盘、未合 PR #26、未碰真实资金。**
日期 2026-11-25。用户选 A：只跑 BTC 1h 的 F/N 两份契约，跳过 BTC 4h（19 笔必 INCONCLUSIVE），
ETH/SOL 保留为未触碰的跨资产验证集。

两份契约事前锁定：F `464759f2…`、N `b4861d21…`（不同哈希=两个独立假设，各自计数），
`falsify_if` 完全一致，`check_lock` 均通过。

---

## 一、结论先行

| 变体 | 判定 | 全样本净 | IS | OOS | 成交 |
|---|---|---|---|---|---|
| **F（防拥挤开）** | **INCONCLUSIVE** | −1.67% | +3.91% | **−5.58%** | 105 |
| **N（防拥挤关）** | **INCONCLUSIVE** | +0.02% | +7.33% | **−7.31%** | 120 |

**两份都是 INCONCLUSIVE，原因是 OOS 段只有 19/22 笔（`min_fills=30` 不足）。**
这是新修的 Bug1 生效的结果——旧引擎会在样本不足时仍用网格结果判 FALSIFIED，
这次正确短路为 INCONCLUSIVE，被短路的 4/3 条触发规则保留为审计证据。

**没有通过。** IS 正 / OOS 负是典型过拟合形态，两份都呈现。

## 二、用户重点盯的两件事

### 1. 1h 成本安全垫：极薄，2x 即崩

| 成本倍数 | F 净 | N 净 |
|---|---|---|
| 1x（8bps 往返） | −1.67% | **+0.02%** |
| 2x（16bps） | **−10.07%** | **−9.58%** |
| 5x（40bps） | −35.27% | −38.38% |

**1x 勉强打平，2x 立刻 −10%。** 1h 的单笔毛利不足以覆盖双边费率上浮——
这跟 E2 的结论一致（1h 成本占 ATR 34%，压缩后 ATR 更小，占比只会更高）。
压缩突破在 1h 上没有成本安全垫。

### 2. F vs N 消融：funding 过滤的真实 lift 不显著

| 指标 | F（过滤开） | N（过滤关） | 差值 |
|---|---|---|---|
| 成交 | 105 | 120 | N 多 15 笔（过滤本身拒绝 22 笔） |
| 全样本净 | −1.67% | +0.02% | N 略好 +1.69pp |
| **IS 净** | +3.91% | **+7.33%** | N 好 +3.42pp |
| **OOS 净** | **−5.58%** | −7.31% | **F 好 +1.73pp** |
| control lift | +0.041% CI[−0.147,+0.229] | +0.057% CI[−0.115,+0.232] | 均跨 0 |

**结论：不能实锤"高费率后该顺势还是防拥挤"。** 全样本/IS 上 N 略好（支持用户指出的
"砍掉高费率顺势段"），但 OOS 上 F 反而略好——两个方向都有，且置信区间都跨 0、
OOS 样本只有 19/22 笔。**消融结果 INCONCLUSIVE，不做方向性结论。**

这正说明当初设消融开关是对的做法：文字推理两种说法都能讲通，数据也说不清。

### 稳健性补充（E5 血泪条）

| 项 | F | N |
|---|---|---|
| 剔除 top1 盈利笔 | −1.67% → **−2.8%** | +0.02% → **−1.1%** |
| 剔除 top3 | −5.1% | −3.4% |
| 正笔 | 46/105 | 53/120 |
| 出场分布 | 止损 55 笔 −48.4%；止盈 41 笔 +45.8% | 止损 60 笔 −52.8%；止盈 47 笔 +52.5% |
| 参数网格 | 6/6 全正（IS） | 6/6 全正（IS） |

N 又是"剔除一笔就转负"。网格 6/6 全正看似高原，但那全是 IS——
OOS 段两份都全负，说明这个"高原"是行情红利不是参数稳健。

## 三、判定与下一步

- **S2b BTC 1h：两份 INCONCLUSIVE，不构成通过，也不构成证伪**（OOS 样本不足 +
  IS/OOS 符号翻转）。按用户选 A 的口径，如实记录。
- **BTC 4h 跳过**（预检 19 笔 <30，不锁契约、不污染台账）。
- **ETH/SOL 保持未触碰**，作为后续 S2c 的跨资产盲测集。
- 若做 S2c（三币合并）：4h 48 笔 / 1h 303 笔够样本，但必须先立新假设 + 新契约 + 重锁。
  **注意**：1h 的成本结论（2x 即崩）不会因为换周期/换资产而消失——S2c 若仍用 1h，
  要先解决成本问题；4h 成本垫更厚（占 ATR 15%），但样本只有 48 笔。

## 四、产物与复算

| 文件 | 作用 |
|---|---|
| `research/v4/build_s2b_entries.py` | 信号生成（F/N，`--variant both`） |
| `research/v4/entries/s2b_btc_1h_{F,N}.csv` | 105 / 120 笔信号 |
| `research/v4/specs/s2b_btc_1h_{F,N}.json` | 两份事前契约 |
| `research/v4/locks/s2b_btc_1h_{F,N}.lock.json` | 两份锁 |
| `research/v4/export_candles_csv.py` | 从 `data/v3_market/` 导出 CSV（逐字节可复现，大文件不入 git） |
| `research/v4/diagnostic/btcusdt_1h_3y.csv` / `btcusdt_funding_3y.csv` | 新 3 年数据（gitignore，可从缓存重建） |
| `research/v4/s2b_btc_1h_{F,N}_report.md` / `.json` | verify 原始报告 |

复算（三步，全部离线）：
```
# 1) 从 data/v3_market 重建 CSV（大文件不入 git，此步可复现）
PYTHONPATH=. python research/v4/export_candles_csv.py --symbol BTCUSDT --tf 1h --kind both

# 2) 重建信号（F/N 两变体）
PYTHONPATH=. python research/v4/build_s2b_entries.py --variant both

# 3) 带契约锁跑证伪（N 变体把 F 换成 N）
python -m research.verify --spec research/v4/specs/s2b_btc_1h_F.json \
  --candles research/v4/diagnostic/btcusdt_1h_3y.csv \
  --signals research/v4/entries/s2b_btc_1h_F.csv \
  --lock research/v4/locks/s2b_btc_1h_F.lock.json \
  --out research/v4/s2b_btc_1h_F_report.md --json
```
重跑验证：与首次结果逐项一致（F −1.6659%/105 笔，N +0.0226%/120 笔）。

**边界**：只读本地 3 年缓存，未联网、未下单、未改现有文件、未碰实盘、未合 PR #26。
