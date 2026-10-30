# 案例矩阵：同一台引擎，四种死法

2026-10-29，`research/verify/` 对四个**事前声明**的策略变体跑完整证伪电池。四个 spec 的假设、证伪规则、退出规则、成本假设、对照组参数全部先于结果写死在 `research/verify/samples/cases/*.json`，跑完不分好坏全部发布——包括看起来很赚钱的那个。
统一设置：Binance 1h futures（BTCUSDT/ETHUSDT，29,347 根/币，2023-06-24 → 2026-10-28，本地缓存离线回放）· taker 4bps/边 · 含资金费 · 止盈 +3% / 止损 -8% / 超时 24h · 随机对照 200 笔（seed=42）· 证伪规则：基线亏损 / ×2 成本亏损 / 对照组 CI 跨 0。

| 案例 | 假设 | 成交 | 净收益 | vs 随机对照 | 裁决 |
|---|---|---:|---:|---|---|
| case1 RSI 超卖抄底 BTC | RSI<30 做多，均值回复 | 1310 | -505.65% | **refuted**（-0.37%，CI[-0.70,-0.03]） | FALSIFIED |
| case2 RSI 强势追涨 BTC | RSI>70 做多，动量延续 | 1638 | **+34.35%** | **unproven**（+0.03%，CI 跨 0） | FALSIFIED |
| case3 RSI 超卖抄底 ETH | case1 的跨币种复现 | 1419 | -1144.10% | **refuted**（-0.80%） | FALSIFIED |
| case4 RSI 抄底 BTC（maker） | case1 改限价成交，2bps/边 | 1301 | -453.20% | **refuted**（-0.40%） | FALSIFIED |

## 四种死法，四种教训

**case1：又亏钱又打不过抛硬币。** 止损位 -8% 在持仓窗口内只有 4.9% 被摸到——这个策略的"止损"大部分时间根本没在工作，亏的时候全是超时钝刀割肉。

**case2（重点）：赚钱，但钱不是它的。** 三年 +34%，看着能发朋友圈。但同一窗口随机入场 200 笔，均值只差 +0.03%/笔且 95% 区间跨 0——**统计上无法区分于抛硬币**。2023-06 到 2026-10 是加密大牛市，随机入场也赚钱。这条曲线卖的是 beta，包装成 alpha。这正是行业第一痛点： Broker Tested Reviews 实测 22 个零售 bot 里 17 个与指数相关性 >0.8。

**case3：换个币种就不复现。** 同一个规则、同一个参数、同一窗口，ETH 上 -1144%。在 BTC 上"好像有效"的信号，跨一个币种就死——样本内巧合的典型指纹。

**case4：成交模型救不了基本面。** maker 限价（2bps/边、只在价格穿越时成交）把亏损从 -505% 收窄到 -453%，仍然被对照组击败。费率减半改变的是量级，不是符号。

## 矩阵之外：一个历史存在证明

本项目的 FundingRate 审计（`research/audit/`，12 币 15m，2095 信号）是仪器历史上唯一输出 **proven** 选择性的案例：费率信号在每一种执行假设下都赢随机对照（maker 真实成交模式下 +0.027%/笔）——**信号里确有价格之外的信息，但信息刚好薄于执行摩擦**（PF 1.00 恰好打平）。完整数据见 README「FundingRate：三档费率 × 两种成交模型」一节。

引擎能输出 `proven`，只是这一次的四个候选都不配。

## 复现

```bash
for c in research/verify/samples/cases/*.json; do
  python -m research.verify --spec "$c" --out "docs/cases/$(basename "$c" .json).md" --json
done
```

完整报告（含退出结构、成本敏感性表、障碍可达性、对照组明细、事前证伪规则判定）：`docs/cases/case*.md`，机器可读版为同名 `.json`。

> 节号说明：报告节号固定（判定恒为 §7、披露恒为 §8）。§5（walk-forward）与 §6（参数网格）只在 spec 声明对应仪器时出现——四个案例 spec 均未声明，故直接 §4 → §7。样本报告（demo spec 启用了两台仪器）是完整的 §1-§8。

## 外部真实策略（客户旅程盲测产出）

| 案例 | 来源 | 成交 | 净收益 | vs 随机对照 | 裁决 |
|---|---|---:|---|---|---|
| [external-cascade-fader](external-cascade-fader.md) | Master-Trader（旧审计线老朋友） | 3 | +3.23% | 样本不足 | INCONCLUSIVE（8% 深跌规则在 BTC 1h 上 3.3 年仅 3 次） |
| [external-freqbot-fundingrate](external-freqbot-fundingrate.md) | FreqBot FundingRateEnhanced | 325 | -27.22% | unproven [-0.05,+0.17] | FALSIFIED（2026-10-30 复核修正后） |
| [external-grid-freqbot](external-grid-freqbot.md) | FreqBot GridTradingStrategy（零售最常跑的网格类） | 360 | **+61.06%** | unproven [-0.40,+0.44] | FALSIFIED（又一条“赚钱但打不过随机”的曲线） |

三个外部案例共用同一条信息：**能赚钱的策略很多，能证明自己不是运气的策略很少**。grid 案例额外是一次完整客户旅程盲测——从客户格式 CSV（`date` 列名）到参考导出器（`skill/strategy-falsifier/examples/export_grid_signals.py`）到证伪报告，全程只用公开文档；旅程中踩到的六个摩擦点已全部修复并配回归测试。

## 诊断层：每份报告现在回答“怎么死的，怎么办”

每份报告第 8 节是**诊断**：主死因（样本不足 / 随机性杀死 / 成本杀死 / 无 edge / 过拟合杀死 / 结构杀死 / 未被证伪）+ 附带问题 + 证据（全部来自本次测量）+ 处方（每条引用本次运行的真实数字）。归类规则事前声明于 `research/verify/diagnosis.py`，先于任何策略被分类——与证伪规则同一条纪律。多策略对比矩阵（`docs/compare-sample.md`）每行带死因列，一眼看清一组变体各自死在哪。
