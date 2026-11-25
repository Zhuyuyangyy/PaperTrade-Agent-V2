# S2 执行报告 — 五道过滤过严（2 笔）FALSIFIED；去防追高后 +2.47% 但 INCONCLUSIVE

分支 `audit/v3-acceptance-fixes`。**未接实盘、未合 PR #26、未碰真实资金。**
日期 2026-11-25。契约锁 sha256 `58ba0d4a5795fc4e…`（执行细节修正两次，均已留痕；
`falsify_if` 零变动，`check_lock` 通过）。

---

## 一、结论先行

| 项 | 结果 |
|---|---|
| 正式 S2（五道过滤，契约口径） | **FALSIFIED — 但理由是 2 笔样本，统计上无意义** |
| 诊断探针（去掉防追高） | **INCONCLUSIVE — 12 笔净 +2.47%，但 12<30 且样本外 0 笔** |
| 真正发现 | **不是信号无效，是过滤叠加过严**——防追高一道把 16 笔砍到 2 笔 |

这与 S1 完全不同：S1 是裸信号就没 edge；S2 的裸信号（去防追高）基准 +2.47%、2x 成本 +1.51%，
只是样本不足以确认。**不应据此说 S2 策略无效，也不应据此说它有效。**

## 二、正式 S2：为什么这个 FALSIFIED 不能全信

### 过滤漏斗（两年 4h，4380 根，零缺口）

| 阶段 | 剩余 |
|---|---|
| ATR14/ATR60 ≤ 0.75（压缩） | 631 |
| 放量突破（量 z≥1.5）后 | 16 |
| **防追高（实体≤1.0×ATR）后** | **2** |
| 防拥挤 + 持效确认后 | 2 |

**防追高一道砍掉 87.5%。** 原因很直白：放量突破（z≥1.5）天然伴随大实体，
"放量突破 + 小实体"本身近乎矛盾——这两个条件是互相打架的。

### 引擎判定（诚实记录矛盾）

引擎输出 `FALSIFIED`，理由："样本内参数网格 1/9 格为正（11%）< 50%"。

但同一份报告的 checks 里：

| 规则 | scope | 状态 |
|---|---|---|
| `min_fills` | 全窗口 | **insufficient：成交 2 笔 < 30** |
| `min_fills` | 样本外 | **insufficient：样本外没有任何入场信号** |
| `grid_positive_ratio_min` | 样本内 | triggered（1/9 < 50%） |

引擎在 `min_fills` 触发 insufficient 的同时，仍用 **2 笔样本**的网格结果判了 FALSIFIED，
而报告文字却写着"其余规则未评估"。**2 笔的网格 1/9 没有任何统计意义**——
按契约精神应是 INCONCLUSIVE。

引擎输出我如实保留（不改判定），但结论写清楚：**这个 FALSIFIED 是样本量导致的伪判定，
不能作为"策略被证伪"的证据。** 全窗口 2 笔净 −2.14%、胜率 0%，同样不足以定论。

## 三、诊断探针：去掉防追高（非契约，不计入验收/家族数）

只去掉把 16→2 的那道防追高，其余（退出/成本/资金费/防拥挤/持效/证伪规则）与契约完全一致：
`funding_applied=True`，12 笔。

| 项 | 值 |
|---|---|
| 成交 | 12 笔，胜率 **58.33%**，PF **1.46** |
| 净收益（最坏口径） | **+2.47%** |
| 成本 ×1 / ×2 / ×5 | **+2.47% / +1.51%** / −1.37% |
| 随机对照（2000 次） | 均值 −0.108%/笔，中位 −1.08% |
| 出场分布 | 止盈 7 笔 +7.85%；止损 5 笔 −5.38% |
| `min_fills` | 12 < 30，样本外 0 笔 → **INCONCLUSIVE** |

**基准和 2x 成本都为正，5x 转负。** 方向上是"信号可能有东西，但扛不住 5x 成本压力"。
样本外 0 笔是硬伤——12 个信号全落在样本内段，无法排除是那段行情的偶然。

**这不是通过。** 按契约 `min_fills=30`，12 笔只能 INCONCLUSIVE。

## 四、判定与下一步

- **正式 S2：本版过滤组合否掉**（2 笔不可交易，FALSIFIED 的统计效力不足但结论方向一致：
  当前形态不可用）。
- **真实发现：防追高条件设计错误**，不是波动率压缩突破这个思路无效。
- **不建议**直接把诊断版当 S2b 上线——12 笔 + 样本外 0 笔，任何"看起来正"都可能是运气。
- 若要救，正确路径是另立 **S2b**：重新设计防追高（例如用"实体相对近期分位"而非固定
  1.0×ATR，或改为突破后回踩确认），**新假设 + 新契约 + 重新锁**，不得在本轮改阈值凑结果。

## 五、执行细节修正留痕（两次，均不改契约判定）

1. 首锁后引擎报 `signals.side='both'` 非法 + `signals.path` 相对解析错误 → 改为
   `side='long'`（占位，真实方向由 CSV 每行决定）+ `../entries/...`，留痕 `locks/s2_relock_note.md`。
2. 首跑 `funding_applied=False`（`--candles` 模式不自动加载资金费，S1 已踩过同一坑）→
   补 `funding_path`，重跑后 True。

两次修正都不在 `contract_payload()` 保护范围（`name/symbol/interval/venue/falsify_if`），
契约哈希始终 `58ba0d4a…`。留痕的意义是区分"格式修正"与"看完结果偷改阈值"。

## 六、产物与复算

| 文件 | 作用 |
|---|---|
| `research/v4/specs/s2_squeeze_breakout.json` | 锁定契约（`falsify_if` 未改） |
| `research/v4/locks/s2_squeeze_breakout.lock.json` | 事前锁 + `s2_relock_note.md` 留痕 |
| `research/v4/build_s2_entries.py` | 信号生成（可复算；`--mode full/no_body`） |
| `research/v4/entries/s2_btc_4h_entries.csv` | 正式 2 笔 |
| `research/v4/entries/s2_btc_4h_entries_NOBODY.csv` | 诊断 12 笔 |
| `research/v4/s2_report.md` / `.json` | 正式 verify 报告 |
| `research/v4/diagnostic/s2_no_body_report.md` / `.json` | 诊断报告 |

复算：
```
PYTHONPATH=. python research/v4/build_s2_entries.py --mode full
python -m research.verify --spec research/v4/specs/s2_squeeze_breakout.json \
  --candles research/v4/diagnostic/btc_4h.csv \
  --signals research/v4/entries/s2_btc_4h_entries.csv \
  --lock research/v4/locks/s2_squeeze_breakout.lock.json --out research/v4/s2_report.md --json
```

**边界**：只读本地缓存，未联网、未下单、未改现有文件（只新增 `research/v4/`）、
未碰真实资金、未合 PR #26、未开 Demo、未接 AI。
