# V4 结构 Alpha 试验台账

规则：**每次独立试验都记，负结果永久保留**。没有"这次不算"的选项。  
累计试验次数是多重检验校正的分母——它只能增加，不能重置。

## 历史负结果（从既有报告回填，口径一致）

| # | 试验                                                                                     | 结论         | p / 关键数                | 报告                                            |
| - | -------------------------------------------------------------------------------------- | ---------- | ---------------------- | --------------------------------------------- |
| 1 | E 三组入场（legacy/multitf/AI filter）                                                       | FAIL       | 三组全亏                   | `reports/entryE_2026-11-23.md`                |
| 2 | E2 入场结构六组                                                                              | FAIL（结构定位） | 5m 成本占 ATR 134%        | `reports/entryE2_structure_2026-11-23.md`     |
| 3 | E3 4h 参数高原+置换                                                                          | FAIL       | p=0.5220               | `reports/entryE3_validation_2026-11-23.md`    |
| 4 | E4 funding level                                                                       | FAIL       | p=0.1000，样本外 −5.2558   | `reports/entryE4_funding_2026-11-23.md`       |
| 5 | E5 funding delta                                                                       | FAIL       | p=0.0833，剔除 top1 转负    | `reports/entryE5_funding_delta_2026-11-23.md` |
| 6 | R0 随机基线（1h）                                                                            | FAIL       | E5 p=0.4110，第 58.9 百分位 | `reports/random_baseline_report.md`           |
| 7 | R0-4h 随机基线                                                                  | FAIL       | p=0.6815，18/18 样本外负    | `reports/random_baseline_4h_report.md`        |

**累计独立试验：7 次，正结果 0 次。**

## V4 新增（设计已就位，尚未跑批）

| #  | 试验                           | 状态                       | 契约锁                                                                     | 结果                                                              |
| -- | ---------------------------- | ------------------------ | ----------------------------------------------------------------------- | --------------------------------------------------------------- |
| 8  | S1 拥挤洗盘反转（正式，含 OI）           | **INCONCLUSIVE — 数据不可得** | `locks/s1_crowded_flush_reversal.lock.json` sha256 `a1aa277f…`（**未改动**） | OI 历史仅 30 天，无法覆盖 90 条回看+5折                                      |
| 8b | S1 无 OI 诊断探针（**非契约，不计入家族数**） | **FALSIFIED**            | 独立 spec，5 条事前规则触发                                                       | −20.76%；随机 lift −0.09% CI 跨 0；网格 0/9                            |
| 9  | S2 压缩突破+防追高（五道过滤）            | **FALSIFIED**（统计效力不足）    | `locks/s2_squeeze_breakout.lock.json` sha256 `58ba0d4a…`                | 2 笔，网格1/9；引擎在min_fills=insufficient下仍判FALSIFIED                 |
| 9b | S2 去防追高诊断探针（**非契约，不计入家族数**）  | **INCONCLUSIVE**         | 独立spec，只去掉body过滤                                                        | 12笔+2.47%，2x+1.51%，5x−1.37%；样本外0笔                               |
| 10 | S2b-F BTC 1h（防拥挤开） | **INCONCLUSIVE** | `locks/s2b_btc_1h_F.lock.json` `464759f2…` | 105笔；IS+3.91%/OOS−5.58%；2x成本−10.07%；OOS仅19笔<30 |
| 11 | S2b-N BTC 1h（防拥挤关） | **INCONCLUSIVE** | `locks/s2b_btc_1h_N.lock.json` `b4861d21…` | 120笔；IS+7.33%/OOS−7.31%；2x成本−9.58%；OOS仅22笔<30 |
| 12 | S3 跨币种残差配对 | **草案已写，未锁未跑** | `S3_draft.md`（未锁） | 三币数据已到位，残差口径/瘸腿契约待用户审阅 |

**注意**：#8 的契约已经锁定，任何阈值修改都会被 `contract_lock.check_lock()` 检出并判契约篡改。  
#8b 是独立诊断（去掉缺失的 OI 条件），**不算独立家族、不进多重检验分母**——它只是判定"裸信号有没有信息"。  
#9/#10 故意不锁——理由见各自设计文档；这不是遗漏。

**S1 执行结论**：#8 因 Binance OI 历史端点只回溯 30 天而物理不可得（契约需 2 年）；  
#8b 裸信号已 FALSIFIED，加 OI 只会减少信号、不会把负期望翻正 → **S1 本版假设否掉，不建议投入采集 OI 历史**。  
详见 `S1_execution_report.md`。

## 多重检验口径

当前家族数 = 11（历史 7 + S1 + S2 + S2b-F + S2b-N）。  
#8b（S1 无 OI 探针）与 #9b（S2 去防追高探针）均为非契约诊断，不计入分母。  
S3 若跑，家族数 → 10。

| 校正         | 公式                  | 门槛         |
| ---------- | ------------------- | ---------- |
| Bonferroni | p_raw × 家族数         | ≤0.05 才算显著 |
| BH-FDR     | 按 p 排序逐项 p×家族数/rank | ≤0.05 才算显著 |

**单一未校正 p 值一律不作为通过依据。** E5 的 0.0833 就是这样被否的。

## 停手条件（V4.1 已定，写在这里防止漂移）

1. S1/S2/S3 全部 FAIL → 转持续数据收集与低频研究，**不再调参、不再加信号**
2. 任一试验触发 `insufficient` → 记为 INCONCLUSIVE，不算通过，也不算失败
3. 任何"先跑一下看看再改阈值" → 违反契约锁，本次试验作废，计数仍保留

**当前进度（2026-11-25）**：累计 11 家族，正结果 0。  
S1=INCONCLUSIVE（OI 数据不可得）+ 裸信号探针 FALSIFIED。  
S2=FALSIFIED（但仅 2 笔样本，统计效力不足）；去防追高诊断 12 笔 +2.47%，INCONCLUSIVE。  
**S2b-F/N（BTC 1h）双 INCONCLUSIVE**：105/120 笔，IS 正但 OOS 负（−5.58%/−7.31%），
OOS 仅 19/22 笔 <30；1x 勉强打平、**2x 成本 −10%**，1h 无成本安全垫。
F/N 消融未实锤 funding 方向（全样本 N 略好、OOS F 略好，CI 均跨 0）。  
ETH/SOL 保留为未触碰跨资产验证集。S3 草案已写（`S3_draft.md`），未锁未跑。
距停手条件还差 S3 两组未跑。

**引擎缺陷记录**：S2 报告在 `min_fills` 触发 insufficient（"其余规则未评估"）的同时，  
仍用 2 笔样本的网格结果判 FALSIFIED。这是引擎逻辑矛盾，已如实保留原始判定并在  
`S2_execution_report.md` 说明。**样本 <min_fills 时的 FALSIFIED 不应作为证伪证据。**

**2026-11-25 用户决策链**：S3 初记 `BLOCKED/NO_DATA`（无数据）→ 先修引擎两个防呆 Bug
（已完成）→ 补三币 3 年数据（已完成）→ 用户选 A：只跑 BTC 1h 的 S2b F/N（已完成，
双 INCONCLUSIVE）+ 起草 S3 草案（已完成）→ ETH/SOL 保留为跨资产盲测集，S2c 待定。  
详见 `S2b_draft.md` 与 `data_backfill_plan.md`。

**引擎修复记录（同日，先于 S2b）**：

1. `insufficient` 硬短路 INCONCLUSIVE——`_verdict()` 里把 insufficient 判断提到 triggered 之前，  
   triggered checks 保留为证据、reason 注明"不作为证伪依据"。  
   回归 `tests/test_verify_guardrails.py`（5 例）。agensi demo 期望随之从 FALSIFIED 改 INCONCLUSIVE  
   （合成 demo 样本外 fills<30，旧行为是假证伪）。
2. 永续 + 外部 K 线缺 `funding_path` → fail-fast（CLI 返回 2），新增 `--no-funding` 显式豁免。  
   S1/S2 连续两次漏配的坑从工具链堵死。
3. 引擎改动后重建 agensi 单文件构建物 + 更新 SHA256SUMS（zip 发布包未重打包，本轮不动发布物）。

## 禁止事项

- 禁止复用样本外数据当独立测试集
- 禁止未经验证组合多个弱信号
- 禁止为压低 p 值延长数据窗口或改参数
- 禁止实盘执行、真实交易密钥
- 禁止合并 PR #26

### 待处理项：agensi 发行包落后于源码（本轮不动，需用户决定）

两个 guardrail 修改后已重建 `agensi/scripts/verify.py`（4724 行，sha256 `fc1e7c49…`）  
并同步 `SHA256SUMS.txt`，全套 962 passed 全绿。  
但发行包 `strategy-falsifier-agensi-v1.0.0.zip` 内仍是旧 `verify.py`  
（sha256 `85f8b327…`，无 `no_funding`），`docs/agensi-release-report.md` 的验收记录  
（"ZIP 解压运行 demo = FALSIFIED"）也基于旧版。

**本轮未重打包、未改发行报告**——重打包属发布行为，超出"修引擎 Bug"授权范围。  
需要用户决定是否发 v1.0.1（重打包 + 更新报告 + 重验 demo 判定从 FALSIFIED 变 INCONCLUSIVE）。
