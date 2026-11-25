# S1 执行报告 — 数据不可得 + 无 OI 诊断 FALSIFIED

分支 `audit/v3-acceptance-fixes`。**未接实盘、未合 PR #26、未碰真实资金。**
日期 2026-11-25。锁定的契约 `research/v4/locks/s1_crowded_flush_reversal.lock.json` 一字未改，
校验仍通过（sha256 `a1aa277fda436081…`）。

---

## 一、结论先行

| 项 | 结果 |
|---|---|
| 正式 S1（锁定契约，含 OI 四条件） | **INCONCLUSIVE — 数据不可得，无法执行** |
| 无 OI 诊断（非契约探针，不计入验收） | **FALSIFIED — 5 条事前规则同时触发** |
| 判定强度 | 诊断结论足以否掉本版假设；补 OI 只会让信号更少，不会翻盘 |

## 二、为什么正式 S1 无法执行

S1 契约第 4 个入场条件是 `oi_delta_bps ≤ -300`（OI 急降 = 杠杆已清洗）。核查结果：

| 检查 | 结果 |
|---|---|
| 仓库全仓 OI 历史数据 | **0 条**（`oi_snapshot` / `openInterest` 在数据文件中零命中） |
| 采集器代码 | 存在（`cross_market/collectors/binance_microstructure.py`），但从未采过历史 |
| Binance OI 历史端点回溯上限 | **约 30 天**（2026-10-26 起；31 天前报 `code=-1130 startTime invalid`） |
| 契约要求 | 90 条 funding 回看 + 5 折 walk-forward + `min_fills=30`，需 2 年 |

30 天 OI 连 90 条 funding 的 warmup 都覆盖不了（90×8h = 30 天刚好吃完，剩下 0 天可交易）。
**这不是策略失败，是数据物理不可得。**按锁定契约的 `min_fills=30`，只能判 INCONCLUSIVE。

我上一轮说"S1 数据现成、无需新增采集器"是错的——采集器代码现成≠历史数据现成。这里更正。

## 三、无 OI 诊断（明确不计入验收）

为判断"值不值得去搞 OI 历史数据"，我跑了前三条件（费率高分位 + 企稳 + 实体约束）的裸信号：

```
lookback=90, q_high=0.95, body≤1.5×ATR14(1h), cooldown=6×4h, direction=SHORT
退出/成本/资金费/证伪规则：完全复用锁定契约（TP+1.5% / SL−1.2% / timeout6根 / trail0.8%
                            / fee 4bps边 / taker / funding逐次 / fee_mult [1,2,5]）
```

| 项 | 值 |
|---|---|
| 数据窗口 | 2024-11-23 → 2026-11-23，4380 根 4h，零缺口 |
| 两年高分位事件 | 308 |
| 过企稳 + 实体后候选 | 101（2025:53 / 2026:48） |
| `funding_applied` | **True**（资金费逐次结算已生效） |
| 成交 | 101 笔，胜率 **38.61%**，PF **0.705** |
| 净收益（最坏口径） | **−20.76%** |
| 成本 ×2 / ×5 | −28.84% / −53.08% |

### 5 条事前规则全部触发

| 规则 | 证据 |
|---|---|
| `worst_case_net_negative` | −20.76% ≤ 0 |
| `net_negative_at_fee_multiple=2` | −28.84% ≤ 0 |
| `control_lift_ci_includes_zero` | 策略 − 随机 = **−0.09%/笔**，95% CI **[−0.34, +0.16]** 跨 0 |
| `grid_positive_ratio_min`（样本内） | **0/9 格为正** |
| `grid_positive_ratio_min`（样本外） | 3/9（33%）< 50% |

另有样本外 `min_fills=28 < 30` 触发 INCONCLUSIVE（次要，主线已被上面 5 条判死）。

### E5 血泪条补充验收（人工执行）

| 项 | 值 |
|---|---|
| 总净收益 | −20.76% |
| 剔除 top1 / top2 / top3 | −22.2% / −23.64% / −25.08% |
| 正笔 | 39/101（38.61%） |
| 出场分布 | 止盈 32 笔 +45.61%；**止损 51 笔 −64.89%**；超时 18 笔 −1.48% |

**这不是"靠几笔撑着"的脆弱，是整体负期望**——剔除盈利笔只会更负。诊断标签 `primary=randomness`：入场无法与随机区分。

### 一个反直觉的点

做空策略在全窗口亏 −20.76%，而同敞口做空持有基线是 **+4.34%**（样本外 −0.35%）。
"费率高分位后做空"在这两年不仅没有 edge，还**跑输了无脑做空持有 25.10pp**。
高费率之后价格更倾向于继续涨（挤空），与假设方向相反——这与 E4/E5 的"顺势优于反向"发现一致。

## 四、判定

- **S1 本版假设：否掉。** 裸信号已 FALSIFIED，加 OI 过滤器只能减少信号数（101 → 更少），
  在整体负期望上做减法不可能翻成正期望。
- **是否继续投资源搞 OI 历史数据：不建议。** 没有数据能改变这个结论的方向。
- **若要救 S1，唯一有理论依据的改法是翻方向**（高费率后顺势做多），但那等于开新策略 S1b，
  必须另立假设、另锁契约、重新的事前判定——不能在这里改方向凑结果。

## 五、产物与复算

| 文件 | 作用 |
|---|---|
| `research/v4/specs/s1_crowded_flush_reversal.json` | 锁定契约（**未改动**） |
| `research/v4/locks/s1_crowded_flush_reversal.lock.json` | 事前锁（**未改动**，校验通过） |
| `research/v4/diagnostic/s1_no_oi_spec.json` | 独立诊断 spec（标注 DIAGNOSTIC，非契约试验） |
| `research/v4/diagnostic/btc_4h.csv` | 4h K线（本地 1h 聚合，零缺口） |
| `research/v4/diagnostic/btc_funding.csv` | 资金费序列（本地缓存导出） |
| `research/v4/entries/s1_btc_4h_entries_NOOI.csv` | 101 个前三条件信号 |
| `research/v4/diagnostic/s1_no_oi_report.md` / `.json` | verify 引擎原始报告 |

复算：
```
python -m research.verify \
  --spec research/v4/diagnostic/s1_no_oi_spec.json \
  --candles research/v4/diagnostic/btc_4h.csv \
  --signals research/v4/entries/s1_btc_4h_entries_NOOI.csv \
  --out research/v4/diagnostic/s1_no_oi_report.md --json
```

**边界**：本轮只读本地缓存 + 3 次 fapi 只读探测（确认解封/OI 回溯上限，无下单、无写交易所状态），
未改锁定契约、未跑正式 S1（数据不可得）、未接 AI、未开 Demo、未合 PR #26、未碰真实资金。
