---
name: strategy-falsifier
description: 策略证伪引擎——把交易策略写成 spec JSON，用六件仪器（path-aware 回放、成本敏感性、障碍可达性、随机入场对照组、walk-forward、参数网格）尝试杀死它，输出由事前证伪规则驱动的裁决报告（FALSIFIED / INCONCLUSIVE / NOT_FALSIFIED）。当用户要求证伪策略、回测审计、过拟合检测、曲线拟合检测、walk-forward 验证、随机对照检验，或问"这个交易策略是否有效/这个回测能不能信"时使用。
---

# strategy-falsifier · 策略证伪引擎

引擎不做预测，只回答一个问题：**一个声称有效的策略，能不能在诚实的执行、诚实的成本和诚实的对照组面前活下来。**

工作方式：策略以 spec（JSON）形式事前声明——包括证伪规则 `falsify_if`——然后引擎跑完整电池尝试触发这些规则。规则先于数字存在；看完报告再定的标准是营销，不是测量。

## 何时用

- 有一个回测曲线很漂亮的策略，想知道它是不是过拟合 / 曲线拟合；
- 要审计别人的回测结论（换成本、换对照组、换样本外段再测一遍）；
- 策略参数是调出来的，想量化它对精确参数组合的依赖（参数网格）；
- 想验证策略是否真的优于随机入场（随机对照组 + bootstrap 置信区间）；
- 上线前做最后一道关卡：成本翻倍后 edge 是否还活着。

何时**不**用：寻找新策略（引擎只有 `rsi_below` / `rsi_above` 两条内置规则，它是证伪器不是搜索器）；预测未来（它不预测任何东西）；需要逐 tick 撮合、滑点、资金容量建模（见报告第 8 节「未建模事项」）。

## 快速开始

在仓库根目录（`research/verify/` 所在层）执行：

```bash
# 内置演示：BTC 1h RSI(14)<30 做多，完整跑六件仪器，无需任何外部文件
python -m research.verify --demo

# 审计自己的策略：spec JSON + 可选 K 线 CSV（列 t,o,h,l,c）
python -m research.verify --spec path/to/spec.json --candles path/to/klines.csv --out report.md --json

# 查看本地行情缓存里有什么（无缓存时演示自动回退到内置 5000 根 K 线）
python -m research.verify --list-market
```

本 Skill 提供一层包装（自动定位仓库根目录并转发全部参数）：

```bash
python skill/strategy-falsifier/scripts/verify.py --demo
python skill/strategy-falsifier/scripts/verify.py path/to/spec.json --candles klines.csv --out report.md
```

本机演示所用 Python：`C:/Users/联想/.workbuddy/binaries/python/versions/3.13.12/python.exe`（脚本默认用当前解释器 `sys.executable`，可用环境变量 `STRATEGY_FALSIFIER_PYTHON` 覆盖）。

演示输出为一份 markdown 证伪报告：裁决置顶，随后依次是回放、成本敏感性、障碍可达性、随机对照组、walk-forward、参数网格、事前证伪规则判定表、未建模事项。

## 六件仪器一览

| # | 仪器 | 回答的问题 | 声明位置 |
|---|---|---|---|
| 1 | path-aware 回放 | 逐 K 线推演（非抽样），bar 内先到止损判止损（最坏优先），最好/最坏口径并列 | `worst_case`、`fill_model` |
| 2 | 成本敏感性 | 费率 ×1/×2/×5 重定价后 edge 是否还活着 | `fee_multiples` |
| 3 | 障碍可达性 | 止盈/止损位是不是持仓窗口内价格真的会去的水平（MFE/MAE 实测） | `exit` |
| 4 | 随机入场对照组 | 同窗口、同退出规则、同成本，入场改为随机——策略能把自己与掷硬币分开吗 | `control` |
| 5 | walk-forward | 窗口切 N 折，最后一折作样本外重跑全电池；样本外触雷整体证伪 | `walk_forward` |
| 6 | 参数网格 | 退出参数网格逐格回放，看分布不看最佳格——只在一格活着的是曲线拟合 | `param_grid` |

裁决三态：`FALSIFIED`（有事前规则被触发）、`INCONCLUSIVE`（样本不足，既不能证实也不能证伪）、`NOT_FALSIFIED`（本窗口本成本下未触雷——注意，未被证伪 ≠ 被证实）。各仪器的真实数字案例见 [references/instruments.md](references/instruments.md)。

## spec 格式要点

spec 是一份 JSON：`name`、`symbol`、`interval`、`exit`、`signals` 为必填，其余全部有默认值。核心纪律：

- **证伪规则事前声明**：`falsify_if` 支持 `min_fills`（默认 30）、`worst_case_net_negative`、`net_negative_at_fee_multiple`、`control_lift_ci_includes_zero`、`grid_positive_ratio_min`（(0,1]）。注意：没有 `falsify_if` 的 spec 不会被判"不可证伪"——没有任何规则可触发时，只要成交够，裁决是 `NOT_FALSIFIED`（报告第 7 节仅注明"未声明任何 falsify_if 规则"）。不写证伪规则的审计等于没审计。
- **信号三种来源**：`kind` = `rule`（内置 `rsi_below`/`rsi_above`，Wilder RSI）、`csv`（外部信号文件）、`random`（对照组本身）。
- **可网格化的只有退出参数**：`take_profit`、`stop_loss`、`timeout_bars`、`trailing`，最多 2 轴、默认上限 64 格；动信号参数等于换了个实验。
- 块存在即启用：写了 `walk_forward` / `param_grid` 块但没写 `enabled: false` 时默认启用。

完整字段表（类型、默认值、校验规则、错误消息）见 [references/spec-format.md](references/spec-format.md)。

## 示例与参考

- [examples/demo_spec.json](examples/demo_spec.json)——离线演示 spec（复制自引擎 samples/demo_spec_csv.json），每个字段带中文注释，可直接 `--spec` 运行
- [references/spec-format.md](references/spec-format.md)——spec JSON 完整字段文档（与 research/verify/spec.py 一一对应）
- [references/instruments.md](references/instruments.md)——六件仪器说明，各配一个本次演示跑出的真实数字案例
- [scripts/verify.py](scripts/verify.py)——一层包装：接收 spec 路径并调用 `python -m research.verify`

## 诚实声明（继承自引擎）

引擎不建模：滑点与部分成交、资金容量冲击、同时持仓相关性、交易所/链路风险；walk-forward 只能切时间，无法验证参数是否曾用全窗口数据调优。这些写在其报告的第 8 节，读报告前先读它。
