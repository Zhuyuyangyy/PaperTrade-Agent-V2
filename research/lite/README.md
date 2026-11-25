# research/lite — 省额度的策略验证管线

单文件、零第三方依赖、零 API Key。目的不是做产品，是用最小成本回答一个问题：
**这个策略值不值得工程化。**

一次完整验证 = 拉 3 年 15m 数据 + 3 个策略 + 4 档成本敏感性 + 2000 次随机对照，
全部本地确定性执行，**不消耗任何 LLM 额度，也不需要任何密钥**。

## 1. 依赖对比

| 依赖 | 本方案 | 说明 |
|---|---|---|
| API Key | **不需要** | Binance `/api/v3/klines` 是公开行情端点，不签名、不读 `.env` |
| 第三方库 | **不需要** | 只用 Python 标准库；无 pandas / numpy / ccxt / backtrader |
| 项目引擎 | **不需要** | 不 import `research/` 下任何模块，与 V3/V4 账本完全解耦 |
| GitHub / CI / Codex 工程 | **不需要** | 单文件可拷走，`python alpha_lite.py` 直接跑 |
| LLM 额度 | **0** | 拉数、回测、出报告全在本地 |

实测：约 110 次 HTTP 请求拿 105,999 根 15m K 线（3.02 年），零 418、零密钥。
回测 3 个策略 + 2000 次 MC 耗时 **12 秒**。

## 2. 三条防呆，做在代码里而不是写在文档里

### 2.1 只处理已收盘 K 线（双检）

Binance 返回的最后一根永远是**正在形成**的那根。实测某一时刻：

```
open 1795582800000  close_time 1795583699999  closed? False  delta +13.2min
```

两道检查，任何一道不过就报错退出，不静默丢弃：

1. **拉取时**：`close_time < serverTime`（用 Binance 自己的时钟，不用本地钟）
2. **回测时**：`close_time < 本次运行开始时刻`（防数据文件是旧的、或被人手工塞了新行）

### 2.2 下一根开盘成交，策略物理上看不到未来

- 信号在 `bar[i]` **收盘后**产生，成交价强制取 `bar[i+1].open`
- 最后一根 K 线不产生成交（它没有 `i+1`）
- 策略函数签名为 `def s(v: Bars) -> int`，`Bars` 是只读视图：
  - `v[-1]` = 当前刚收盘那根
  - 访问 `v[i+1]` → 抛 `LookaheadError`
  - **切片直接禁用**（`v[0:2]` 抛 `TypeError`），否则可以绕过下标检查

这不是约定，是运行时会炸的结构。

### 2.3 每根 K 线可独立校验

落盘格式（一行一根）：

```json
{"t":1795581900000,"r":[1795581900000,"59222.54",...12个原始字段...],"h":"9f3a1c..."}
```

- `h` = `sha256(canonical_json(原始12字段))[:16]`，逐根可复算
- manifest 里另有整份 `fingerprint` = `sha256(排序后的 "t:h" 行)`
- 缺口检测：`open_time` 不连续直接拒跑，不跳过

## 3. 用法

```bash
# 不联网自检（16 项：收盘过滤/哈希/前视/成交价/成本方向/缺口/指纹）
python research/lite/alpha_lite.py selftest

# 拉数据（增量续传，只补缺的两侧；--days 是目标窗口）
python research/lite/alpha_lite.py pull --days 1095

# 校验每根哈希 + 时间连续性 + 未收盘检查
python research/lite/alpha_lite.py verify

# 回测（默认 taker 10bps/边 + 滑点 2bps/边 = 往返 24bps）
python research/lite/alpha_lite.py run --strategies ma,donchian,mom --mc 2000

# 低费率敏感性（VIP/做市 6bps 往返）
python research/lite/alpha_lite.py run --fee-bps 2 --slip-bps 1 --mc 0

# 需要逐笔明细时
python research/lite/alpha_lite.py run --save-trades
```

## 4. 内置策略（参数写死，不扫网格）

| 名称 | 规则 | 持有 |
|---|---|---|
| `ma` | 20/60 双均线金叉死叉 | 16 根（4h） |
| `donchian` | 32 根通道突破 | 32 根（8h） |
| `mom` | 24 根（6h）动量 | 24 根（6h） |

参数都是教科书默认值，**没有做任何网格搜索**。改参数 = 新实验，必须重跑重报，
不许"扫一圈挑最好的那个"。

## 5. 实测结论（2026-11-25）

数据 `d3952769…`（105,999 根，2023-11-17 → 2026-11-25，零缺口）。

| 策略 | 笔数 | 毛/笔 | 净/笔 | 全样本 | 样本外 | 毛/成本 |
|---|---|---|---|---|---|---|
| ma | 1,784 | **−0.99bps** | −24.99bps | −98.94% | −83.92% | −0.04 |
| donchian | 2,063 | **+0.31bps** | −23.69bps | −99.40% | −89.79% | 0.01 |
| mom | 4,239 | **+1.05bps** | −22.95bps | −100.00% | −98.47% | 0.04 |

**三个策略的毛收益 95% 置信区间全部跨 0**（t = −0.44 / +0.10 / +0.58）。
也就是说：不是"有微弱 edge 被成本吃掉"，而是**毛 edge 与 0 无法区分**。

成本侧：15m 的 ATR(14) 中位数只有 **27.3bps**，往返 24bps = **ATR 的 87.9%**。
把费率压到 6bps（做市商级别）重跑，三个策略仍分别 **−73.57% / −75.25% / −90.89%**。

详细报告见 [`reports/15m_baseline_verdict_2026-11-25.md`](reports/15m_baseline_verdict_2026-11-25.md)。

## 6. Phase 1：Funding Carry（`carry.py`）

同一套防前视与成本口径，验证"现货多 + 永续空"的 Delta 中性费率套利。
规则事前锁定（滚动 9 期年化 > 15% 开仓、连续 3 期 ≤ 0 或年化 < 3% 平仓、4 腿 32bps）。

结果（2023-11 → 2026-11，三币各 26,298 根 1h 交集，零缺口）：

| 币 | 笔数 | 年化 | 均持仓 | funding/笔 | 基差/笔 |
|---|---|---|---|---|---|
| BTC | 2 | 3.40% | 2,932h | 547bps | 13bps |
| ETH | 3 | 3.93% | 2,301h | 428bps | 13bps |
| SOL | 3 | 3.96% | 1,653h | 425bps | 18bps |
| 组合 | 8 | **3.77%** | 2,216h | 152bps | 5bps |

判定 **INCONCLUSIVE**（8 笔 < 10），但实质否定：全样本年化 3.77% < 5% 门槛；
样本外 0 笔（2025-2026 滚动年化最高只有 10.95%，从未触及 15%）；
**无条件 carry 年化 5.2-7.2%，反而跑赢择时的 3.8%**，且 2026 年已降到 1.6-2.7%。

详见 [`reports/carry_phase1_verdict_2026-11-25.md`](reports/carry_phase1_verdict_2026-11-25.md)，
Phase 2 草案见 [`PHASE2_draft.md`](PHASE2_draft.md)（未跑）。

```bash
python research/lite/alpha_lite.py pull --symbol BTCUSDT --interval 1h --days 1095 --tag spot
python research/lite/carry.py
```

> 注意：`--tag spot` 落 `spot_btcusdt_1h.jsonl`，与永续数据分开；`--interval` 会影响缺口检测步长。

## 7. Phase 2：Top-12 动态池截面动量（`cross_section.py`）

契约 `phase2_spec.json`（sha256 `4402817e…`），跑前锁定、启动时校验，哈希失配拒跑。
25 个 USDT 永续 × 1,151 根 1d + 3,452 条 funding，每个调仓日按 trailing 30 日成交额取 Top 12。

| 基线 | 成本 | 全样本年化 | 夏普 | 样本外 | OOS 夏普 |
|---|---|---|---|---|---|
| A 纯动量 | 1x | +21.59% | 0.863 | −5.42% | −0.169 |
| A 纯动量 | 2x | +13.65% | 0.610 | −11.52% | −0.491 |
| B 动量+funding | 1x | +25.01% | **1.072** | +5.92% | 0.418 |
| B 动量+funding | 2x | +16.55% | 0.771 | −1.70% | −0.010 |

**A/B 双 FALSIFIED**，但全样本 alpha 显著（随机对照 p=0.0155 / 0.0045）。
逐年 A：2024 +66.0% → 2025 +19.6% → 2026 −7.9%，与 Phase 1 的 carry 衰减同形。
B 的 funding 复合有效（空头腿 +5.3% → +26.2%，MDD 29.6% → 20.0%），但没能救活样本外。

详见 [`reports/phase2_verdict_2026-11-25.md`](reports/phase2_verdict_2026-11-25.md)。

```bash
python research/lite/cross_section.py pull     # 125 次请求
python research/lite/cross_section.py verify
python research/lite/cross_section.py run --mc 2000
```

## 8. 待审草案（未锁、未跑）

| 文档 | 内容 | 状态 |
|---|---|---|
| [`PHASE3_draft.md`](PHASE3_draft.md) | 日线波动率目标 + 风险平价，对比 B&H 的夏普/卡玛/回撤 | 待确认 3 个参数后锁跑 |
| [`PHASE2B_draft.md`](PHASE2B_draft.md) | 离散度 FLAT 门控 + 纯 funding 截面，机会型卫星 | 备选，等 Phase 3 结论再议 |

Phase 3 数据现成（现货 1h 聚合日线，三币各 1,151 天零缺口），确认后无需新拉。

## 9. Phase 3：波动率目标 + 风险平价（`vol_target.py`）

契约 `phase3_spec.json` sha256 `7ae39d11…`，跑前锁定。现货日线，目标年化波动 30%，
20 日窗口，不加杠杆不做空，11bps/边。

| 组合 | 全样本年化 | 夏普 | 最大回撤 | 卡玛 |
|---|---|---|---|---|
| BTC B&H | +12.97% | 0.496 | 55.21% | 0.235 |
| 基线 1 BTC vol-target | **+19.10%** | **0.683** | **39.98%** | **0.478** |
| 基线 2 三币 RP+VT | +19.83% | 0.677 | 42.84% | 0.463 |
| 静态 BTC @73.3%（对照） | +11.74% | 0.496 | 43.17% | 0.272 |

**双基线 FALSIFIED，但差 1.33pp**：Q1（夏普 +37.7%）、Q3（卡玛翻倍）通过；
Q2 要求回撤压缩 30%，实际 27.6%（39.98% vs 门槛 38.65%）；Q4 差 0.56pp。
Q6 是我把"成本可忽略"错译成"调仓 ≤10 次"（实际 32.4 次/年，但成本只有 22bps/年）。

**关键对照**：动态 vs 静态同仓位，收益 +7.36pp、夏普 +0.187、回撤 −3.19pp，
排除了"只是买得少"的解释。

详见 [`reports/phase3_verdict_2026-11-25.md`](reports/phase3_verdict_2026-11-25.md)。

```bash
python research/lite/vol_target.py
```

## 10. 边界

- **单标的、单周期**：只验证 BTCUSDT 15m。结论不能外推到 1h/4h 或其他币
- **无止损、无仓位管理、无资金费**：故意减少自由度。现货不计资金费；合约需另加
- **非重叠单仓位**：同一时刻只有一笔，不复利加仓
- **零成本结果是上界，不是可交易结果**：`0x` 只用来分离"信号有没有信息"和"成本吃掉多少"
- **数据不入库**：`data/*.jsonl` 23MB 被 gitignore，用 `pull` 重建；manifest 保留，作为本轮结论的数据锚点
