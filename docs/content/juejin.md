# 我把策略回测引擎改成了"证伪引擎"：六件仪器，全部来自一次失败的六 agent 实验

> 先说结论：这篇文章不教你怎么写出赚钱的回测曲线，教你怎么把一条曲线放到仪器上测出它是不是幻觉。引擎开源（MIT），297 个测试全绿，离线可跑。所有数字来自仓库真实报告，可复现。

## 0. 背景：一个负期望值系统留下的遗产

几个月前我做了个六 agent 纸面交易系统（搜索/风控/聪明钱/LLM 研判/FOMO/退出），36 小时后自测出期望值为负，停机。停机前留下的东西里，最有价值的不是系统，是那套"证明它不行"的测量代码。

后来我把它产品化成了一个引擎：`research/verify/`。设计目标只有一句话——

> 输入一份策略 spec，输出一份**试图杀死它**的报告。

裁决不是打分，是**事前证伪规则**的判定：`falsify_if` 写在 spec 里，先于任何数字存在；规则触发 → `FALSIFIED`；样本不足 → `INCONCLUSIVE`（永不放过）；否则 `NOT_FALSIFIED`，并附赠一句"未被证伪 ≠ 被证实"。

```json
{
  "name": "rsi-dip-btc-1h-demo",
  "hypothesis": "BTC 1 小时 RSI(14) 低于 30 后做多，捕捉均值回复",
  "symbol": "BTCUSDT",
  "interval": "1h",
  "venue": "futures",
  "fee_rate": 0.0004,
  "fill_model": "taker",
  "funding": true,
  "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
  "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30, "side": "long"},
  "control": {"enabled": true, "n_random": 200, "seed": 42},
  "walk_forward": {"enabled": true, "folds": 3},
  "param_grid": {"axes": {"take_profit": [0.02, 0.03, 0.04], "stop_loss": [-0.06, -0.08, -0.10]}},
  "fee_multiples": [1, 2, 5],
  "falsify_if": {
    "control_lift_ci_includes_zero": true,
    "net_negative_at_fee_multiple": 2,
    "worst_case_net_negative": true,
    "grid_positive_ratio_min": 0.5,
    "min_fills": 30
  }
}
```

跑法：

```bash
python -m research.verify --demo --out report.md                    # 内置演示
python -m research.verify --spec your.json --candles klines.csv \
    --signals sig.csv --out report.md --json                        # 自己的策略
```

下面按工程实现讲六件仪器。代码都在 `research/verify/` 和 `research/audit/pathsim.py`，行号对应当前 main。

## 1. Path-aware 回放：逐 bar 走价，冲突显式裁决

核心问题：大多数回测框架在同一根 K 线里同时触及止盈和止损时，悄悄选了对你有利的那个。`pathsim.py` 的 `_one_trade()` 把这件事变成了显式开关：

```python
if tp_hit and sl_hit:
    # The whole point of this module: disclose which one you assume.
    if worst_case:
        reason, price = "stop_loss", sl_price
    else:
        reason, price = "take_profit", tp_price
```

实现要点：

- **入场价约定按成交模型分流**：taker 取信号**次根**开盘价（信号只在 bar 收盘可知，次根开盘是第一个可成交价）；maker 取信号根收盘价作为近似挂单价，再用穿越测试决定是否成交。两个约定都写进报告披露。
- **maker 模型的止盈方向**：多单的止盈是挂在市场上方的卖单，需要 high 穿越才成交——早期版本复用入场的穿越方向，把整个 maker 模型悄悄反了，现在 `tp_hit` 对 maker 单独判定。
- **退出优先级**：bar 内触发的退出（止盈/止损/跟踪止损）优先于超时；信号退出在收盘确认，排在最后。
- **最坏/最好并列输出**：`_battery()` 对同一批 entries 用 `worst_case=True/False` 各跑一遍，报告同时给出两个口径，而不是替你选一个。

这仪器的杀伤力有据可查：CascadeFader 仓库用"是否触及过止盈价"分析得到 +469%/91% 胜率，改成路径回放后是 +132%/PF 1.62。**同一批信号，差 3.5 倍。**

## 2. 成本敏感性：edge 付不付得起自己的 toll

`_battery()` 里对 `spec.fee_multiples`（默认 `[1, 2, 5]`）逐个重放，其余参数不动，输出每档的净收益/PF/胜率：

```python
for mult in spec.fee_multiples:
    sim = simulate(candles, entries, exit_rule,
                   fee_rate=spec.fee_rate * mult, ...)
```

本项目实测（case1，RSI 抄底 BTC，1310 笔）：基线费率（4bps/边）净收益 **-505.65%**；×2（8bps 往返）**-610.45%**。事前规则 `net_negative_at_fee_multiple=2` 直接触发。

顺带一个尺子教训：配置里写的往返 120bps 是给 $1,000 单定的价；$100 单在池深中位 $799k 的池子里实测往返 60bps，其中 50bps 是 LP 地板，**和池深无关**。参数值没错，用错了地方——这类错误只有把费率当自变量重放才能暴露。

## 3. 随机入场对照组：打不过抛硬币，绝对收益没有意义

`control.py` 的设计原则：**同窗口、同退出规则、同成交模型、同成本，只有入场时机不同。**

```python
def random_control(candles, rule, spec, entries, *, n_random, seed, ...):
    sides = list(entries.values())
    side = max(set(sides), key=sides.count) if sides else "long"
    rand_entries = random_signals(candles, n_random, seed, side=side, bar_range=bar_range)
```

实现要点：

- **对照组镜像策略的主方向**：纯多头策略配纯多头对照组。对照组回答的是入场时机问题，不是方向问题，假装否则就是放水——这一点写进报告披露。
- **`bar_range` 约束**：随机入场只允许落在指定 bar 区间。walk-forward 的样本外段用它，保证样本外对照组同段抽取——不同段的对照组不是对照。
- **bootstrap CI**：对（策略均值 − 对照组均值）做 2000 次 paired-independent 重采样（`BOOTSTRAP = 2000`，`seed=42`），取 2.5%/97.5% 分位。
- **三值判定**，不是二值：`proven`（CI 下界 > 0）/ `refuted`（CI 上界 < 0）/ `unproven`（跨 0）。CI 不可得时输出 `unknown` 而不是编一个。

case1 实测：差值 -0.37%/笔，95% CI **[-0.70, -0.03]**，不跨 0 → `refuted`，显著差于抛硬币。case2（RSI 追涨 BTC，三年 +34.35%）差值 +0.03%，CI 跨 0 → `unproven`：统计上无法区分于运气，牛市 beta 包装成 alpha，照样触发 `control_lift_ci_includes_zero` 判 FALSIFIED。

## 4. Walk-forward：样本外段跑完整电池，规则同判

`__init__.py` 的 `_walk_forward()`：

```python
fold_bars = len(candles) // folds
boundary = (folds - 1) * fold_bars
is_entries  = {i: s for i, s in entries.items() if i < boundary}
oos_entries = {i: s for i, s in entries.items() if i >= boundary}
```

三个容易做错的工程细节：

1. **两段都模拟完整 K 线序列，不截断。** 最后一折入场的交易，其退出发生在折尾之后——截断数据会制造大量"未平仓"交易，那是仪器造出来的假样本。折尾之后的 bar 相对该入场依然是未来。
2. **样本外对照组同段抽取**：`control_range=(boundary, len(candles)-2)`，样本外段的随机入场只能落在样本外段。
3. **样本外裁决权重更高**：同一批事前规则在样本外复判，**在样本外触发 → 整体 FALSIFIED**。只在看得见的地方成立的 edge 不是 edge；样本外信号不足 → INCONCLUSIVE，永不放过。

另外 `_battery()` 被设计成"对单一份入场集跑全套装置"，样本内/样本外/全窗口复用同一函数——**用不同方式测量样本外的仪器不是对照组**。

demo 报告实测（29,347 根 K 线切 3 折）：样本内 -240.34% → 样本外 **-265.31%**（变化 -24.98pp）。这个案例里不是衰减，是稳定地亏；样本外段触发事前规则，整体 FALSIFIED。

## 5. 参数敏感性网格：看分布，不看最好的格子

`grid.py` 的纪律写在自己 docstring 里：网格值**事前声明**在 spec 里，每格用**同一批 entries、同一口径**（spec 的 worst/best 标记、基准费率）重放，只有声明的轴在动。

刻意**不做**的三件事，每一件都是行业里常见的自欺：

- 不按格重定价成本——那是第三个维度，且费率电池（第 2 节）已经回答；
- 不按格重跑对照组——对照组问题是入场时机，一个诚实对照组好过 N 个欠采样对照组；
- 不动信号参数——改信号 = 改入场集 = 另一个实验，不是敏感性。

实现细节：网格最多 2 个轴（热力表是二维的），超过 `max_cells` 直接 SpecError 拒绝；**无成交的格保留在结果里但不计入占比分母**——没交易的格子不构成任何方向的证据，计它为亏是放水，计它为赚是放水。

判定读事前声明的 `grid_positive_ratio_min`：占比低于阈值 → FALSIFIED。demo 报告 3×3 网格（止盈 × 止损）实测**样本内 0/9、样本外 0/9 全负**——这个参数空间里没有 edge，更谈不上稳健。如果 edge 只存在于某一格，那是刀刃上的曲线拟合；9 格全负，是连刀刃都没有。

## 6. 裁决层：只读规则，不发明规则

`_evaluate_criteria()` 的约束是结构性的：criteria 全部来自 spec，这个函数只读规则和数字，**从不发明新规则**：

```python
triggered = [c for c in checks if c.get("triggered")]
insufficient = [c for c in checks if c.get("insufficient")]
if triggered:      outcome = "FALSIFIED"
elif insufficient: outcome = "INCONCLUSIVE"
else:              outcome = "NOT_FALSIFIED"  # 附"未被证伪 ≠ 被证实"
```

配套的诚实条款：

- `min_fills` 不足 → INCONCLUSIVE，不是 FAILED，更不是 PASS；
- 每份报告有固定的"未建模事项"一节——**不知道仪器边界的人会滥用仪器**。引擎看不到你的参数搜索过程：walk-forward 和网格是必要不充分条件，不是免罪金牌。

## 实测：四个事前声明的策略，四种死法

统一设置：Binance 1h futures，29,347 根/币，2023-06-24 → 2026-10-28 本地缓存，taker 4bps/边含资金费，止盈 +3%/止损 -8%/超时 24h，随机对照 200 笔（seed=42）。

| 案例 | 成交 | 净收益 | 对照组 | 裁决 |
|---|---:|---:|---|---|
| RSI 超卖抄底 BTC | 1310 | -505.65% | refuted（-0.37%，CI[-0.70,-0.03]） | FALSIFIED |
| RSI 强势追涨 BTC | 1638 | +34.35% | unproven（+0.03%，CI 跨 0） | FALSIFIED |
| RSI 超卖抄底 ETH | 1419 | -1144.10% | refuted（-0.80%） | FALSIFIED |
| RSI 抄底 BTC（maker） | 1301 | -453.20% | refuted（-0.40%） | FALSIFIED |

两个附带发现，比主表更有工程意义：

- **障碍可达性**（`excursions.py`）：case1 的止损位 -8%，持仓窗口内只有 **4.9%** 被摸到。止损是个"价格会走到这里"的断言，直接量它——大部分时间没在工作，亏的时候全是超时钝刀割肉。
- **maker 改不了符号**：2bps/边 + 穿越成交，把亏损从 -505.65% 收窄到 -453.20%，仍然被对照组击败。费率减半改变量级，不改变符号。

诚实的边界案例：本项目 FundingRate 审计（12 币 15m，2095 信号）是仪器历史上唯一输出 `proven` 的案例——费率信号在每种执行假设下都赢随机对照，但最终 PF 1.00 恰好打平。**有信息的信号也可能薄于摩擦。** 引擎能输出 proven，这一次的四个候选都不配。

## 复现与边界

```bash
for c in research/verify/samples/cases/*.json; do
  python -m research.verify --spec "$c" --out "docs/cases/$(basename "$c" .json).md" --json
done
```

- 行情数据解析顺序：`--candles` CSV > 本地缓存 `runtime/audit/market.sqlite` > 内置演示包（fresh clone 无缓存时 demo 仍可跑，报告披露差异）。
- 信号三种来源：`kind=rule`（从 K 线算）/ `kind=csv`（外部信号文件）/ `kind=random`（对照组自身）。
- 测试：`test_verify.py` 60 个 + 全仓 297 个全绿。

最后说句超出工程的：这套仪器的本质是**内部逻辑一致性优先于外部叙事**——规则先于数字，对照组先于结论，披露先于推销。事后改规则是营销，不是研究。

---

*仓库：github.com/Zhuyuyangyy/PaperTrade-Agent-V2（`research/verify/`）。本文所有数字可用仓库命令复现。不构成投资建议。*
