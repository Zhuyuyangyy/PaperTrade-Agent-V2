# Pine / TradingView 策略移植前审计清单

> 读法：买家的策略来自 TradingView（Pine Script）时，**在写 spec、锁契约之前**先过这份清单。
> 这些 bug 发生在 Pine 脚本里、在引擎看到任何 CSV **之前**——引擎从导出的信号/成交 CSV 里抓不到它们。
> 上游脚本错了，下游证伪得再严谨也是在给一个错策略做尸检。

## 为什么有这份清单：三个真实的已发布脚本案例

本清单的每一条都来自真实公开仓库的 CHANGELOG（并在源码里核对过修复代码），不是假想：

1. [`mehmeterenyasar/tradingview-key-levels-indicator`](https://github.com/mehmeterenyasar/tradingview-key-levels-indicator)
   （Pine v6，MPL-2.0，已发布在 TradingView）——第 1-6 项的来源。
2. [`345-strategies/thestrat-suite`](https://github.com/345-strategies/thestrat-suite)
   （Pine v6，MPL-2.0，TradingView 开源上架，36★）——第 7/8/10 项的来源。它经历过一次外部正确性审计
   （v2.2.2 "audit release"），每个修复带 `FIX` 标签可在源码 grep，另有
   `docs/engineering/repaint-prevention.md` 把重绘 bug 提炼成 8 条规则。
3. [`casoon/pine-scripts`](https://github.com/casoon/pine-scripts)
   （Pine v6 合集，MIT，30★，108 份 per-indicator CHANGELOG）——第 8/9/10/11 项的来源。
   其 wavetrend v1.3.0 是一次外部代码审查后的合并修复，两个 critical 都是"开关语义"类 bug。

第一个 repo 的 v1.0 → v1.1 修了两个会**静默改变每一个信号**的 bug：

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

### 7. 假日粘连 bar / 会话边界（用 time_close，不用日历推算）

- **症状**：用 bar 的 open 时间戳判断"市场是否收盘/数据是否过期"，或用日历（周几/几月）推算周期边界。
- **真实案例**（[345-strategies/thestrat-suite](https://github.com/345-strategies/thestrat-suite)，MPL-2.0，
  已发布在 TradingView；FIX 标签可在源码 grep）：
  - CME 把假日半日市**粘进下一个交易日**：2026-07-03（周五半日市）活在 07-06 周一的 bar 里，
    周一实时 bar 的 open 戳是周四晚。`timenow - time` 读出 ~87 小时"过期"，preview 机制在**实时盘中**
    误触发，把正在交易的周/日降级成 C1——周线触发位变成了当天的区间（`GLUE-1`/`GLUE-2b`）。
    修复：过期与否只看 `timenow - time_close`（计划收盘时刻）——实时 bar 的计划收盘永远在未来，不可能误判。
  - 期货月 bar 的 open 戳落在上月（7 月 bar 开在 6/30 18:00 ET），按日历月比较把当前月读成上个月（`PREVIEW-CALCLOSE-1`）。
  - 粘连的 47 小时日线 bar（ES 劳动节后）让"画到 open+1 个周期"的水平线全部停在半路（`LINEEND-CLOSE-1`）。
- **必查**：所有"收盘了吗/换周期了吗/数据旧了吗"的判断，是问 `time_close`（交易所计划收盘，
  自动含假日/提前收盘/外国时段），还是在拿 open 戳或日历做算术？
- **引擎能抓吗**：**部分间接**。K 线时间戳里的粘连表现为超长 gap——引擎的数据卫生检查（见下）会披露
  异常间隔，但"上游把哪根 bar 归到哪个周期"它无法重建。
- **动作**：Pine 里全部改用 `time_close`；导出 CSV 后核对假日周的 bar 数与时间戳归属。

### 8. 条件调用 / 共享缓冲的 ta.*（历史值被静默跳算或串线）

- **症状 A（短路跳算）**：`ta.*` 调用写在 `and`/`or`/`if` 的分支里，前面的条件不满足时整个调用不执行，
  该 bar 的历史值缺失/沿用旧值。
- **真实案例**（[casoon/pine-scripts](https://github.com/casoon/pine-scripts) `wavetrend`，MIT，CHANGELOG "Fixed"）：
  WT crosses、波动率 gate 里的 ATR、止损用的 swing high/low、TP/SL 命中判断原本嵌在条件表达式里，
  "只在前置条件通过的 bar 上计算"——历史 bar 的值被跳过。修复：全部提升到顶层每 bar 无条件计算。
- **症状 B（共享缓冲串线）**：`ta.*` 的历史缓冲属于**词法调用点**，不属于循环迭代。一个函数在
  `for` 循环里对 6 个 timeframe slot 各调一次，6 个 slot 仍共享**同一个**缓冲——`[1]` 拿到的是
  上一个 slot 的值。
- **真实案例**（thestrat-suite `P0-1`，docs/engineering/repaint-prevention.md Rule 3）：
  `ta.change(time(tf))` 检测新周期，6 个 slot 共享缓冲导致"新周期"几乎每根 bar 都触发，
  不停清空 crossed 标志和 sticky stops。修复：每 slot 显式存储 `lastPeriodStart` 自己比较。
- **症状 C（islast 门内的状态）**：状态更新只写在 `if barstate.islast` 里——历史 bar 永不更新，
  reload 后锁存状态消失（thestrat `P1-a`：sticky stop 只在实时锁存，刷新后止损失踪；
  wavetrend 源码注释同样记录 decay 状态曾困在 `islast and showDashboard` 里）。
- **必查**：逐个 `ta.*` 调用问三件事——它在条件分支里吗？它在循环/多 slot 路径里吗？
  它喂的状态在历史 bar 上会重建吗（"现在刷新图表，bar 历史能重建这个锁存吗"）？
- **引擎能抓吗**：**不能**。这是 Pine 运行时语义，CSV 里只有结果。间接信号：Pine 回测成交与引擎
  path-aware 回放系统性对不上时，把它列为怀疑对象。
- **动作**：`ta.*` 全部提顶层；循环内改显式 per-slot 状态；每个 `var` 锁存过一遍 reload 测试。

### 9. 开关与计分的语义（off ≠ pass，na ≠ pass）

- **症状 A（关掉的 gate 送分）**：gate 计分写成 `not enabled or passed` 再求和——关掉的 gate 恒贡献 1 分。
- **真实案例**（casoon wavetrend v1.3.0，CHANGELOG 标 critical）：默认 8 个 gate 全关时得分恒 8/8；
  开一个 gate 且它失败，仍可能过 `minGateScore=6`。修复：只对**启用的** gate 算通过率。
- **症状 B（缺数据 fail-open）**：HTF 确认 gate 把 `na(htfWtOsc)`（暖机/数据缺口）当通过——
  "要求 HTF 确认"的 gate 在最没有东西可确认的时候恰好不设防（同 repo 修复：fail-closed，
  要求真实非 na 值且方向正确；手动 HTF 设得不高于图表周期也判无效）。
- **症状 C（开关只关显示不关效果）**：`useExtD=false` 只隐藏了仪表盘行，评分组件照算（同 repo）；
  HTF Trend Filter 算好了却只被 debug log 读——开了等于没开（critical）。
- **症状 D（na 比较静默走 else）**：Pine 里对 `na` 的任何阈值比较都是 false，三元链静默落到最后一个
  分支——比较标的无数据的 bar 上仪表盘凭空显示 'Bear'（wavetrend v1.3.1）。
- **必查**：每个 gate/toggle 三问——关掉它，分数/信号变吗？数据缺失时它放行还是拦截？
  它控制的是显示还是判定路径？对每个可能为 na 的序列找显式 `na()` 分支。
- **引擎能抓吗**：**不能**（上游语义）。但移植时把 gate 语义写错会直接改变信号 CSV——
  在 port_notes 里逐 gate 记录 on/off/na 三态的语义，再导出。
- **动作**：移植前对每个 gate 做三态真值表（enabled-passed / enabled-failed / disabled / na）。

### 10. 数据源语义（volume 类型、合成蜡烛）

- **症状 A（volume 类型误判）**：用 `syminfo.volumetype == "base"/"quote"` 判"有真实成交量"——
  多数非加密 feed 报 `n/a`，期货/股票（如 EUREX:FDAX1!）被当成无量，量 gate 要么全拦要么全放。
- **真实案例**（casoon wavetrend v1.3.3→1.3.4 + zigzag 同库惯例）：改为只拒 `tick` volume、
  `n/a` 且 feed 实际交付量时算有量；无真实量时 gate 降级为 pass-through 并**披露**（Data Contract `CFD-degraded`）。
- **症状 B（合成蜡烛喂进价格动作逻辑）**：Heikin Ashi 是平均价不是成交 OHLC，bar 分型/触发/目标全错，
  且 HTF `request.security` 请求图表自身 ticker 时**悄悄继承** HA 价格。
- **真实案例**（thestrat-suite v3.2.0 `NO-HA-1`）：直接拒绝 HA 图表并报错，而不是画着错的水平位。
- **必查**：策略依赖 volume 吗？在目标市场（期货/股票/CFD/加密）上 volume 语义各是什么？
  图表类型是真实蜡烛吗？HTF 引用会不会继承合成价格？
- **引擎能抓吗**：**部分**。引擎把 volume 当可选上下文（缺失按 0），不会拿 tick 噪音当真实量做判定；
  但上游 CSV 若从 HA 图表导出，OHLC 数值"完全合法"，引擎无法识别它是平均价。
- **动作**：port_notes 记录数据源类型与 volume 语义；HA/合成图表一律重新用真实蜡烛导出。

### 11. Pine v6 语言陷阱（移植/自检时的确定性雷区）

来自真实 CHANGELOG 的语言级 bug，每条都已发布过、修过：

| 陷阱 | 真实案例 | 后果 |
|---|---|---|
| `for i = 0 to n - 1` 在 n=0 时**倒着数** | casoon zigzag_core v2.2.2：LTF 无 intrabar 返回时运行错 | 空数据不报错反而越界 |
| 显式历史偏移 `[n]` 超过 `max_bars_back` | wavetrend v1.3.0：自动校准把 lookback 推到 48000（默认缓冲 500）；v3 500→1000 | 运行时静默断历史 |
| 多行三元 exit 文案触发编译错 CE10156 | wavetrend_v3 v3.2.1 | 改 if/else |
| 平滑参数用错指数（`beta^length`≈0） | wavetrend_v2 v2.0.1：JMA 近似等于没平滑 | 指标整个失真 |
| label/line 预算独立修剪超限 | zigzag v1.2.0：两个池合并预算才守住 500 label 上限 | 图对象被平台丢弃 |

- **引擎能抓吗**：**不能**（编译/运行时行为）。但这些 bug 的共同点是**静默改变数值**——
  移植到 Python 时逐条对照原实现，别把 bug 一起移植过来。
- **动作**：移植清单里加一行"上游 CHANGELOG 读过吗"——已修的 bug 说明该代码路径高危，优先对照测试。

---

## 引擎侧新增：数据卫生披露（本轮起）

清单第 7 项的"部分间接"现在是引擎的真实能力：`research/verify/data_hygiene.py` 在每次回放前
检查 K 线时间戳的**异常间隔**（相对 spec interval 的中位间隔），把超长 gap（假日粘连、停牌、
数据缺口）作为事实披露进报告——不改裁决，只保证"你的数据里有 47 小时的 bar"这件事不会被静默吞掉。

## 移植流程（接在 SKILL.md 的 INTAKE 之后、LOCK 之前）

```
0. PINE AUDIT  策略来自 TradingView/Pine？
               -> 过本清单 11 项；跑 pine-audit（结构层）
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