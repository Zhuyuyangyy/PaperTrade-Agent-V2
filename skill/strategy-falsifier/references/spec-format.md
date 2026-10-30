# spec JSON 完整字段文档

本文档从 `research/verify/spec.py`（`Spec` / `ExitSpec` / `SignalSpec` / `ControlSpec` / `WalkForwardSpec` / `ParamGridSpec` 及各自 `validate()`）提炼，字段名、类型、默认值与代码一致。加载入口：`Spec.load(path)` → `Spec.from_dict(dict)`，加载时即校验，错误为 `SpecError`（消息指明字段与原因）。

顶层结构（所有块缺省时取默认值；`name`、`symbol`、`interval`、`exit`、`signals` 五个键缺失直接报错 `spec 缺少必填字段 'x'`）：

```json
{
  "name": "rsi-dip-btc-1h",
  "hypothesis": "BTC 1h RSI<30 后的均值回复",
  "source": "",
  "symbol": "BTCUSDT",
  "interval": "1h",
  "venue": "futures",
  "fee_rate": 0.0004,
  "fill_model": "taker",
  "worst_case": true,
  "funding": false,
  "exit":    { ... },
  "signals": { ... },
  "control": { ... },
  "walk_forward": { ... },
  "param_grid":   { ... },
  "fee_multiples": [1, 2, 5],
  "falsify_if":   { ... }
}
```

## 1. 顶层字段

| 字段 | 类型 | 默认 | 校验 / 说明 |
|---|---|---|---|
| `name` | str | 必填 | 非空，否则 `name 不能为空` |
| `hypothesis` | str | `""` | 待检验假设，写进报告头部 |
| `source` | str | `""` | 策略来源，写进报告头部 |
| `symbol` | str | 必填 | 加载时 `.upper()`；非空 |
| `interval` | str | 必填 | 如 `"1h"`；与行情缓存的 interval 对应 |
| `venue` | str | `"spot"` | 必须 `"spot"` 或 `"futures"` |
| `fee_rate` | float | `0.0004` | 单边小数费率（0.0004 = 4bps），必须 ∈ [0, 0.01) |
| `fill_model` | str | `"taker"` | `"taker"` 或 `"maker"`。taker 入场价取信号次根开盘，maker 取信号根收盘（近似挂单价） |
| `worst_case` | bool | `true` |  headline 口径：bar 内同时触及止盈止损时判止损 |
| `funding` | bool | `false` | 仅 `venue="futures"` 允许为 true，否则报错 `funding 只对 futures 场所开放（现货没有资金费）` |
| `fee_multiples` | number[] | `[1, 2, 5]` | 成本敏感性倍数；不能为空，且必须包含 `1`（基准锚点） |
| `falsify_if` | object | `{}` | 事前证伪规则，见第 6 节。缺省时没有任何规则可触发：成交 ≥ `min_fills`(30) 判 `NOT_FALSIFIED`（报告第 7 节仅注明"未声明任何 falsify_if 规则——本报告只呈现测量，不下裁决"），成交不足判 `INCONCLUSIVE`。注意：缺省 ≠ "不可证伪"——一个亏钱的策略什么都不声明也会拿到 NOT_FALSIFIED |

## 2. `exit` 块（必填）

| 字段 | 类型 | 默认 | 校验 / 说明 |
|---|---|---|---|
| `take_profit` | float \| null | `null` | 止盈，小数；**必须 > 0**，否则 `exit.take_profit 必须是正数（小数，如 0.03 表示 +3%）` |
| `stop_loss` | float \| null | `null` | 止损，小数；**必须 < 0**，否则 `exit.stop_loss 必须是负数（如 -0.08 表示 -8%）` |
| `timeout_bars` | int \| null | `null` | 超时 K 线数；**必须 ≥ 1** |
| `trailing` | float \| null | `null` | 移动止损回撤（小数） |
| `trailing_offset` | float \| null | `null` | 移动止损偏移（小数） |
| `exit_signals_path` | str \| null | `null` | 退出信号 CSV（列 `t[,reason]`），收盘价成交；相对路径先按 spec 所在目录解析，再按 CWD |

`take_profit` / `stop_loss` / `timeout_bars` / `trailing` / `exit_signals_path` **至少声明一个**，否则 `exit 至少要声明一个退出条件（...）`。

## 3. `signals` 块（必填）

| 字段 | 类型 | 默认 | 校验 / 说明 |
|---|---|---|---|
| `kind` | str | `"rule"` | `"csv"` / `"random"` / `"rule"` 之一 |
| `path` | str \| null | `null` | `kind="csv"` 时必填，否则 `signals.kind='csv' 时必须提供 signals.path`；信号 CSV 列 `t[,side]`，side ∈ long/short |
| `side` | str | `"long"` | `"long"` 或 `"short"` |
| `n_random` | int | `200` | `kind="random"` 时的随机入场次数 |
| `seed` | int | `42` | 随机种子 |
| `rule` | str \| null | `null` | `kind="rule"` 时必填：`"rsi_below"` 或 `"rsi_above"`（Wilder RSI，纯 Python 实现） |
| `period` | int | `14` | RSI 周期，**必须 ≥ 2** |
| `threshold` | float | `30.0` | RSI 阈值；`rsi_below` 在值 < threshold 时触发，`rsi_above` 在值 > threshold 时触发 |

信号在 bar i 收盘可知，成交在 bar i+1（taker 取次根开盘），无前视。

## 4. `control` 块

| 字段 | 类型 | 默认 | 校验 / 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 随机入场对照组开关 |
| `n_random` | int | `200` | **必须 ≥ 10**，否则 `control.n_random 至少为 10，否则 bootstrap 没有意义` |
| `seed` | int | `42` | 随机种子 |

对照组与策略同窗口、同退出规则、同成交模型、同成本；随机入场在 `[30, len-2]`  bar 区间均匀抽取、允许重复；对照组镜像策略主方向（long-only 策略配 long-only 对照）。样本外段的对照只在该段内抽取。

## 5. `walk_forward` 块

| 字段 | 类型 | 默认 | 校验 / 说明 |
|---|---|---|---|
| `enabled` | bool | 块缺省 `false`；**块存在而未写 enabled 时默认 `true`** | 时间切分样本外验证 |
| `folds` | int | `3` | **必须 ≥ 2**，否则 `walk_forward.folds 至少为 2（1 折没有样本外段，walk-forward 失去意义）` |

窗口按 `len(candles) // folds` 等分；样本内 = 前 N-1 折的入场，样本外 = 最后 1 折的入场；两段各跑完整电池（回放 + 成本敏感性 + 障碍可达性 + 随机对照组），两段的成交都在全窗口上推演（入场之后的 K 线对入场而言仍是未来）。样本外段触雷 → 整体判 `FALSIFIED`。窗口 K 线数 < folds 时报错。

## 6. `param_grid` 块与 `falsify_if`

### param_grid

| 字段 | 类型 | 默认 | 校验 / 说明 |
|---|---|---|---|
| `enabled` | bool | 块缺省 `false`；**块存在而未写 enabled 时默认 `true`** | 参数网格开关 |
| `axes` | object | — | 参数轴 → 取值数组；**至少 1 轴、最多 2 轴**（报告渲染为二维热力表）；每轴 ≥ 2 个值且不许重复 |
| `max_cells` | int | `64` | 网格总格数上限，超过报错并建议先缩小轴范围 |

可网格化的参数仅四个（`GRIDABLE_PARAMS`）：`take_profit`、`stop_loss`、`timeout_bars`、`trailing`。信号参数**不可**网格化——改动信号参数会改变入场集本身，那是另一个实验，不是敏感性。取值符号约束与 `exit` 一致：`take_profit`/`trailing` 必须为正，`stop_loss` 必须为负，`timeout_bars` 必须为 ≥1 的整数。每格用同一入场集、spec 声明的成交口径、基准费率 ×1 回放，只动声明了的轴。

### falsify_if（事前证伪规则，键全部可选）

| 键 | 类型 | 默认 | 触发条件 |
|---|---|---|---|
| `min_fills` | int | `30` | 不是触发器：最坏口径成交 < 此数 → `INCONCLUSIVE`（样本不足，既不能证实也不能证伪） |
| `worst_case_net_negative` | bool | `false` | 最坏优先口径净收益 ≤ 0 → 触发 |
| `net_negative_at_fee_multiple` | number | 缺省不检 | 该成本倍数下净收益 ≤ 0 → 触发（"edge 付不起 toll"）；该倍数须在 `fee_multiples` 中才有对应行 |
| `control_lift_ci_includes_zero` | bool | `false` | 策略 − 随机对照组均值的 95% bootstrap 区间跨 0 → 触发（未能证明选择性） |
| `grid_positive_ratio_min` | number | 缺省不检 | 参数网格正收益格占比 < 此值 → 触发；**必须在 (0, 1] 内**；全部无成交 → `INCONCLUSIVE` |

裁决汇总逻辑：任一规则触发 → `FALSIFIED`；否则有样本不足 → `INCONCLUSIVE`；否则 → `NOT_FALSIFIED`（报告会附注"未被证伪 ≠ 被证实"）。

## 7. 最小可用示例

```json
{
  "name": "rsi-dip-btc-1h",
  "symbol": "BTCUSDT",
  "interval": "1h",
  "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
  "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30},
  "falsify_if": {"worst_case_net_negative": true, "min_fills": 30}
}
```

完整可运行示例（含全部可选块与中文注释）见 [../examples/demo_spec.json](../examples/demo_spec.json)。
