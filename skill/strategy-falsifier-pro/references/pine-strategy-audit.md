# Pine / TradingView 策略移植前审计清单

> 读法：买家的策略来自 TradingView（Pine Script）时，**在写 spec、锁契约之前**先过这份清单。
> 这些 bug 发生在 Pine 脚本里、在引擎看到任何 CSV **之前**——引擎从导出的信号/成交 CSV 里抓不到它们。
> 上游脚本错了，下游证伪得再严谨也是在给一个错策略做尸检。

## 为什么有这份清单：一个真实的已发布脚本案例

本清单的每一条都来自一个真实公开仓库的 CHANGELOG，不是假想：
[`mehmeterenyasar/tradingview-key-levels-indicator`](https://github.com/mehmeterenyasar/tradingview-key-levels-indicator)
（Pine v6，MPL-2.0，已发布在 TradingView）。它的 v1.0 → v1.1 修了两个会**静默改变每一个信号**的 bug：

- **时区 bug**：会话时间按纽约时间填写（ICT 惯例），却用 `Europe/Istanbul`（固定 UTC+3）读取。
  结果每个会话框**提前约 7 小时**画出，美国夏令时期间再漂 1 小时。图表上看着"差不多对"，
  但所有"伦敦开盘""纽约 kill zone"信号都在错误的时间触发。v1.1 改为 `America/New_York`（跟随 DST）。
- **无界状态**：已填充的 gap 留在数组里每根 bar 都被遍历，低周期图表上工作量随历史增长。v1.1 给数组加上限。

一个**已发布、有人用**的脚本带着这两个 bug 活到了 v1.0。这就是为什么"图表看着对"不能当作"策略对"。

## 两层防御，各管一段（诚实边界）

| 层 | 工具 | 抓什么 | 抓不到什么 |
|---|---|---|---|
| 上游·结构 | `pine-audit`（Pine 静态扫描器）+ 本清单 | 重绘、lookahead、时区、House Rules | 策略有没有 edge |
| 下游·统计 | 本引擎六仪器 + 诊断层 | 随机对照、成本、过拟合、障碍可达 | 上游脚本的逻辑/时区 bug |

该仓库用 `pine-audit` 自检，结论"no critical or high findings"。**结构干净 ≠ 有 edge**——
结构审计通过之后，仍然要跑引擎的统计证伪。反过来，引擎判 `survived` 也不代表上游脚本没有时区 bug。
两层都过，才谈得上"这个策略值得继续"。

---

## 审计项（逐条过，每条标注引擎能不能帮你）

### 1. 时区与夏令时（最高危，引擎抓不到）

- **症状**：会话/kill zone 时间按某地时间填写，却用另一个时区读取；或用了固定 UTC 偏移而非命名时区。
- **真实案例**：上面那个 repo，纽约时间 + Istanbul 读取 = 提前 7 小时 + DST 漂 1 小时。
- **必查**：
  - 会话时间字符串（如 `"0930-1600"`）是在哪个时区下解释的？`input.string` 的 timezone 选项与默认值是否和填写时间一致？
  - 用的是命名时区（`America/New_York`，自动跟随 DST）还是固定偏移（`UTC+3`，不跟随）？跨 DST 边界的日期要专门验。
- **引擎能抓吗**：**不能**。引擎把 naive 时间戳一律按 UTC 解析（`data.py`），它不知道你的 Pine 脚本把会话画错了 7 小时。
  导出的信号 CSV 时间戳"看着合法"，但触发时机是错的。
- **动作**：移植前在 Pine 里把时区改对、重跑、重新导出信号；用几个已知日期（含 DST 切换日）人工核对会话框位置。

### 2. lookahead / 未来数据（引擎有纪律，但上游需要单独验）

- **症状**：`request.security` 读了当前未完成 HTF bar 的 high/low/close，信号"提前知道"了未来。
- **该 repo 的正确写法（值得抄的纪律）**：
  - **opens** 用 `lookahead_on` 且无 offset 是**安全**的——当前周/月 bar 的 open 从首 tick 就已知，不泄露未来。
  - **highs/lows/ADR** 一律 `[1]` offset，只读**已完成** bar。
  - 会话高低点逐 bar 从图表本身累加，不向未来要数据。
- **必查**：逐个 `request.security` 调用，确认每个被引用的序列要么是当前 bar 首 tick 已知量（open），要么带 `[1]`。
  任何对未完成 bar 的 high/low/close 引用都是未来数据。
- **引擎能抓吗**：**部分**。引擎自己的纪律是"信号 bar i 收盘可知、成交 bar i+1（taker 取次根开盘）"，
  无前视；但如果上游 Pine 信号本身用了未来数据，导出的 CSV 已经"作弊"，引擎只会忠实地回放一个作弊信号。
- **动作**：用 `pine-audit` 扫 lookahead；人工核对每个 security 调用的 offset。

### 3. 重绘（repaint）

- **症状**：信号在历史 bar 上"事后才出现"，或实时 bar 上闪烁出现又消失。回测看着完美，实盘对不上。
- **必查**：`calc_on_every_tick`、`process_orders_on_close`、`varip` 的使用；信号是否依赖实时 bar 的未收盘值。
- **引擎能抓吗**：**不能直接**。引擎回放的是已确定的历史 bar；重绘是 Pine 运行时行为。
  但有一个间接信号：如果 Pine 回测成交记录与"信号次根开盘成交"的引擎回放系统性对不上，要怀疑重绘或成交假设。
- **动作**：`pine-audit` 扫 repaint；对比 Pine 自带回测与引擎 path-aware 回放的成交差异。

### 4. alert() 在历史 bar 上不触发

- **该 repo 的记录**：v1.1 去掉了 level alert 上的 `isrealtime` 门——因为 `alert()` 在历史 bar 上**从不触发**，
  那个门只会让条件路径在历史和实时之间不一致。
- **必查**：策略的"信号"到底是 `alert()` 触发的，还是 `plotshape`/`strategy.entry` 画的？
  如果买家拿的是"alert 日志"当信号源，历史段会是空的——样本被静默截断。
- **引擎能抓吗**：**能间接**。如果信号 CSV 的时间分布明显只覆盖近期（历史段缺失），
  引擎的 `insufficient`（成交不足）或窗口披露会暴露样本问题——但你要主动问"历史信号去哪了"。
- **动作**：确认信号源是策略成交记录或 plotshape，不是 alert 日志；核对信号时间覆盖整个回测窗口。

### 5. 无界状态 / 性能漂移

- **症状**：gap/流动性/会话数组无上限，每根 bar 遍历全部历史，低周期上越来越慢甚至截断。
- **真实案例**：该 repo v1.0 已填充 gap 不清理，v1.1 加上限。
- **引擎能抓吗**：**不能**。这是 Pine 运行时性能问题，不影响导出 CSV 的数值。
- **动作**：确认所有 `array`/`box`/`line`/`label` 状态有用户可设上限，旧条目被删除。

### 6. 周期感知（timeframe-aware levels）

- **该 repo 的做法**：daily open 在日线图上隐藏、weekly open 在周线图上隐藏——因为它们在那些周期上没有信息。
- **必查**：移植到引擎时，spec 的 `interval` 与策略实际依赖的 HTF 是否一致？
  一个依赖"周线开盘"的策略，你拿 1h K 线回放，引擎不会自动帮你合成周线开盘——信号定义就丢了。
- **引擎能抓吗**：**不能**。引擎按 spec 的 interval 回放，不会重建 Pine 里的 HTF 引用。
- **动作**：移植时把 HTF 依赖显式写进信号定义（或导出已含 HTF 信息的信号 CSV），别让它在移植中静默丢失。

---

## 移植流程（接在 SKILL.md 的 INTAKE 之后、LOCK 之前）

```
0. PINE AUDIT  策略来自 TradingView/Pine？
               -> 过本清单 6 项；跑 pine-audit（结构层）
               -> 时区/lookahead/重绘有任何一项不干净：先修 Pine、重新导出，再来
               -> 干净了，才进入 INTAKE 写 spec

1. INTAKE      写 strategy.json。HTF 依赖、会话时区写进 port_notes（引擎会渲染成报告第 0 节，
               再生成报告也不会丢）。无 K 线？用 --trades-csv 走成交记录尸检。

2. LOCK        python -m engine.cli --lock-contract --spec strategy.json
               ...（后续按 SKILL.md 工作流）
```

`port_notes` 是引擎已有的字段（`spec.py`），专门给"从别处移植来的策略"记录：
什么被原样复制、什么被近似、什么**没有**被移植。把本清单的审计结论写进去，
让每一份再生成的报告都带着"上游脚本审计过/没审计过"的诚实声明。

## 一句话边界

这份清单抓的是**上游脚本对不对**，引擎抓的是**这个规则有没有 edge**。
一个时区画错 7 小时的脚本，引擎可以给它一份严谨的证伪报告——但那份报告证伪的是一个错策略。
先修脚本，再谈 edge。结构干净不等于有 edge，有 edge 也不等于脚本干净。两层都过才算数。