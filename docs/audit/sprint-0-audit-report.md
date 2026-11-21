# Sprint 0 审计报告 — PaperTrade-Agent-V2 → OpenAlpha V1.0

> 审计对象：`Zhuyuyangyy/PaperTrade-Agent-V2`
> 基线：`main@bb058e9` + 未合并 PR #4 `feature/cross-market-v2.2@3ac5c10`、PR #3 `feature/buy-and-hold-baseline@d0db908`
> 审计方式：全量读代码 + 逐项构造回归用例 + 离线运行测试（零网络）
> 交付分支：`sprint-0/data-correctness-openalpha-v1`（stacked on PR #4）
> 范围边界：只读采集、策略研究、模拟执行；不接实盘、不接私钥、不自动跟单。

---

## 0. 结论先说

1. **底层资产可用，但 PR #4 的"研究结论链"目前不可信**。13 项数据正确性缺陷里，
   有 3 项会让"命中率"和"A/C 增量"在数学上失去意义（标签写成价格、A 臂恒做多、
   A/C 按索引截断比较）。这些不是风格问题，是**结论会反号**的问题。
2. **PR #4 不应按原样合并**。数据层骨架（事件库/回放/映射）值得保留，
   特征、标签、实验三层需要重写；本次 Sprint 0 已完成重写并给出可复现证据。
3. **PR #3（买入持有基线）方向正确，建议优先合并**，否则后续策略验证会把
   beta 当 alpha，重复踩坑。
4. **跨市场实验本身有一个设计级问题**（见 §4.1）：当前 `config/market_mapping.yaml`
   里三个市场都是"2026 年底前 BTC 到 X"的长期事件，与预注册的 5 分钟主视界
   没有因果关系。即使把代码全修对，A/B/C 实验也大概率得到"无增量"。
   这是 Sprint 3 之前必须解决的研究设计问题，不是代码问题。
5. **测试把 bug 固化成了契约**。`test_label_from_store_future_only` 断言
   `future_return == 67500`（价格），`test_align_after_ingest_includes_poly`
   断言 `bid` 字段——修复必须同步改测试，否则"全绿"是假绿。

---

## 1. 仓库与 PR 现状

| 项 | 状态 |
|---|---|
| `main` | `bb058e9`，318 个受版本控制文件，CI 跑 `tests/` + `legacy_v1` + `worker demo` |
| PR #4 `feature/cross-market-v2.2` | OPEN，+2644/−0，33 文件；含 P0 数据骨架 + P1 采集/对齐/标签/成本/A-B-C |
| PR #3 `feature/buy-and-hold-baseline` | OPEN (draft)，+1106/−32；给 verify 电池加买入持有基线 |
| PR #1/#2/#5/#6 | 已合并（open-core 发布、文案同步、Pine 审计、CHANGELOG 挖掘） |
| CI | Python 3.11，`compileall` + `safety_check.py` + `pytest -q tests` + legacy unittest + `research.worker demo` |

**处置建议**：
- PR #4：不单独合并。以本 Sprint 0 分支为后继（stacked），修复后一起进 main。
- PR #3：先合并或 rebase 到修复分支，避免 verify 与 cross_market 各写一套基准。
- 合并顺序：PR #3 → Sprint 0（修复 PR #4）→ 后续 Sprint。

---

## 2. 缺陷清单（S0-01 … S0-13）

严重度定义：
- **P0**：会让研究结论方向错误或凭空产生显著性；
- **P1**：会让样本/覆盖率被高估，或让系统在异常数据上继续交易；
- **P2**：可解释性/可维护性问题，不直接影响结论。

| ID | 严重度 | 位置（修复前） | 问题 | 后果 |
|---|---|---|---|---|
| S0-01 | **P0** | `research/cross_market/label_engine.py:12-22,25-32` | `future_return` 字段写入的是**价格**（`future_price`），不是收益 | `hit_rate` 用符号判方向，价格恒正 → 所有信号"永远命中"。方向准确率失去意义 |
| S0-02 | P1 | `label_engine.py:25-32` | 未取 origin 时刻价格，无法算收益；未来窗口取 `rows[0]`，不校验 horizon 内/外 | 标签不可追溯，horizon 语义模糊 |
| S0-03 | **P0** | `experiment.py:150-152` | A/C 两臂各自压缩命中序列后按 `min(len)` **索引截断**再相减 | 比较的是不同决策时刻的收益。两臂出手时点不同 → 伪造增量（构造用例：真实 0，旧实现 +0.5） |
| S0-04 | **P0** | `experiment.py:55-60` | A 臂：`price > 0 and oi > 0 → +1` | 价格与 OI 都是正数 → A 臂在**所有时刻恒做多**。B 臂 `mid > 0.5 → ±1` 也只是概率水平，不是方向 |
| S0-05 | **P0** | `experiment.py:61-67` + `market_mapping.py` | 没有"事件比较符 → 标的方向"的映射 | "BTC 跌破 60k"市场：YES 概率上升代表**看跌**，旧实现按看涨处理，方向系统性反了 |
| S0-06 | **P0** | `feature_engine.py:46-61` | `decision = decision_now_ms or t`：实时模式下所有历史采样点共用同一个决策时刻 | 用窗口末尾的最新数据回填全部历史点 = 经典前视。`scripts/p1_integration_smoke.py:46-49` 用 `now+10s` 正是这种用法 |
| S0-07 | **P0** | `feature_engine.py:49-54,75-83` | "任一 token 的最新一本"直接当概率；YES/NO 角色未声明 | NO token 的报价被当 YES 概率；两 token 一价关系从未校验，盘口错乱时继续使用 |
| S0-08 | P1 | `collectors/polymarket_rest.py:129-133` | `_best` 取 `levels[0]`，不排序 | CLOB `/book` 不保证首元素是 best；bid/ask 可能取到最差价，spread 被放大数倍 |
| S0-09 | P1 | `collectors/polymarket_rest.py:168-171` | 服务端无 `timestamp` 时 `event_ts = recv_ms` | 本地接收时间冒充事件时间；可用性门禁退化为恒真，迟到数据变成"准时" |
| S0-10 | P1 | `collectors/polymarket_rest.py:150-162` | 每轮都 append，`book_hash` 只存不判 | 同一盘口重复入库；覆盖率/样本量虚高，回放里同一状态被多次计数 |
| S0-11 | P1 | `storage.py:51-73` + `replay.py:52-57` | 查询按 `event_ts` 排序，回放要求可用时间单调 | 真实迟到数据会触发 `乱序事件` 异常（可用性门禁把系统自己搞崩） |
| S0-12 | P1 | `collectors/binance_microstructure.py:134-149` | CVD 取 `klines[-1]`（当前未收盘 K 线） | 用了决策时刻之后的成交，CVD 前视；同一分钟内 CVD 反复跳变 |
| S0-13 | P1 | `cost_model.py:29-40` | `EV_net` 不含资金费（只在独立函数里算） | 多为正的资金费时 EV 被系统性高估 |

另外 4 项设计/工程问题：

| ID | 严重度 | 位置 | 问题 |
|---|---|---|---|
| A1 | P1 | `timestamp.py:29-31` | `is_stale` 用 `event_ts` 判陈旧：迟到到达的旧事件被判"过期"，而真正的新数据可能因本地时钟被判"新鲜" |
| A2 | P1 | `market_mapping.py:46-51` | 过期时间解析失败 → `expired=False`（fail-open），应为 fail-closed |
| A3 | P1 | `config/market_mapping.yaml` | 映射市场是 2026 年底到期的长期事件，与 5m 主视界无因果（见 §4.1） |
| A4 | P2 | `collectors/*.py` | 24hr ticker 的 `volume` 是 24h 累计量，字段名 `volume` 易被当成当根 K 线量；需在 payload 标注口径 |

---

## 3. 修复内容（本次分支）

### 3.1 数据层

- `storage.py`
  - 新增 `query_available()`：窗口按 `event_ts` 界定，**排序/过滤按 `max(event_ts, ingest_ts)`**，
    可传 `as_of_ms` 做实时门禁；包含"窗口前发生、窗口内到达"的迟到事件。
  - 新增 `last_available()` / `first_available_after()`：point-in-time 取值与标签未来窗口的统一入口。
  - `append(dedupe=True)`：完全相同的记录只落一条，返回 `None`。
- `replay.py`
  - `run()` 改用 `query_available()`；同一可用时刻允许并列（按 event_ts 再排序），
    只有严格递减才抛"乱序事件"。
- `timestamp.py`
  - `is_stale` 按可用时间判定；新增 `freshness_ms()`、`clock_offset_ms()`、`align_gap_ms()`。

### 3.2 特征/标签/实验

- `feature_engine.py`（重写）
  - 每个决策点 `t` 用两个独立 point-in-time 游标：`t` 与 `t−lookback`，分别算水平值与变化量；
  - `decision_now_ms` 只作"实时天花板"（只生成 `t <= now` 的点），不再改写每个点的 `t`；
  - YES/NO 归一化：`yes_mid` / `no_mid` / `consistent` / `prob_usable`；
    `|yes_mid + no_mid − 1| > 0.05` → `prob_usable=False`；
  - 两侧都输出 `*_delta_bps`、`stale`、`*_age_ms`；无有效报价不产生"空点"。
- `label_engine.py`（重写）
  - `make_label(origin_ts, horizon, origin_price, future_price)` → `future/origin − 1`；
  - `label_from_store` 取 origin 可用价格与 `available >= origin+H` 的第一条快照，
    带 `origin_price`、`label_ts_ms` 便于追溯；`with_mae=True` 可算最大不利偏移。
- `experiment.py`（重写）
  - 信号改为变化量 + deadband；B 臂按 `event_comparator` 映射方向（ABOVE/BELOW）；
  - 主指标：**全样本逐点 payoff 差**（弃权=0，同长同点配对，Block Bootstrap CI）；
  - 诊断指标：`aligned_n`（所有臂都出手的共同决策点数）、`common_hit_ci`；
  - 门槛：`aligned_n < min_aligned`（默认 30）→ INCONCLUSIVE；
  - 报告文案明确写出"共同点命中率差恒为 0 是 C⊆A 的定义后果，不作为证据"。
- `cost_model.py`：`CostInput.funding_bps`（默认 0）计入 `EV_net`。

### 3.3 采集器

- `polymarket_rest.py`
  - best bid = `max(bids)`、best ask = `min(asks)`；校验 `bid < ask`，交叉盘口丢弃并计数；
  - 缺服务端时间戳 → 丢弃 + `skipped_missing_server_ts`（禁止 recv 冒充）；
  - `dedupe=True` + `duplicates_skipped`；记录 `skew_ms_last`（服务端−本地）；
  - payload 增加 `token_role`（YES/NO/UNKNOWN），供特征层归一化。
- `binance_microstructure.py`
  - `event_ts` 取 `openInterest.time`，其次 `premiumIndex.time`；都缺 → 抛错；
    **不再使用 24hr ticker 的 `closeTime`**；
  - CVD 只用 `closeTime <= event_ts` 的已收盘 K 线；无已收盘 K 线 → `None` 并计数；
  - `oi_deltas_from_store` 走可用性门禁。

### 3.4 脚本

- `scripts/p1_integration_smoke.py`：去掉 `decision=now+10s` 的前视用法；
  点位按 5s 采样；标签来自 `label_from_store`；输出采集诊断与 `aligned_n`。

### 3.5 顺带修掉的既有 P0/P1（不在 cross_market 内，但阻断验收）

这些是审计既有资产时发现的、会让"全量测试通过"或"研究结论成立"变成假象的问题。
它们不进 cross_market，但进本分支，因为 Sprint 0 的验收本身就依赖它们。

| ID | 位置 | 问题 | 修复 |
|---|---|---|---|
| C-01 | `research/pipeline/roundlock.py:29-40` | `os.kill(pid, 0)` 在 Windows 上是 `TerminateProcess`，不是探测——读到自己的 pid 就把流水线自己杀掉。本机全量 `pytest` 跑到 `test_roundlock.py` 无 traceback 退出，就是这个 | 改用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess`；`ACCESS_DENIED` 视为存活。新增 `test_alive_detects_our_own_process` |
| C-02 | `research/verify/signals.py:94-104` + `control.py:44` | 随机入场**有放回**抽样写进 `{bar: side}` 字典，重复 bar 被静默合并：400 根窗口抽 200 次只剩 153 个（−23.5%）。对照组样本被缩小，而报告仍按请求数统计 | 改为**无放回** `rng.sample`；`random_control` 同时输出 `n_random_requested` 与 `n_random`（实际） |
| C-04 | `research/replay/timeout_replay.py:101-106` | 两个分支代码完全相同：200 分钟才触发的止损，被直接抄进 45/90/180 分钟的反事实结果——用未来结果回填短期期限 | 止损发生在期限之后时，按期限价格重算并计入 `censored_risk_exits`；报告说明口径 |
| C-10 | `selectivity.py:88`、`signals.py:129/257`、`timeout_replay.py:68`、`evaluators.py:130` | 用 `shutil.copy` 复制 WAL 模式的账本，只拷主文件，未 checkpoint 的最新事务全部丢失——审计窗口末端正好是最新结论依赖的部分 | 新增 `research/ledger.snapshot()`（`sqlite3.Connection.backup`），5 处全部替换；新增 `tests/test_ledger_snapshot.py` |
| C-14 | `research/memehunter.py:668-678` | `health()` 算出 `halt`（最近 30 笔期望 < −0.5%/笔 停手），但 `can_open` 从不消费它——自述风控从未生效 | `can_open` 第一步检查 `health().halt`，返回 `HEALTH_HALT`；新增测试证明 halt 后开仓被拒 |
| C-15 | `research/pipeline/orchestrator.py:192` | `summary["proposals"]` 里 `summary` 在该作用域不存在 → `NameError` 被下面的 `except` 吞掉，evaluator 提案永远不显示 | 写入 `self.evaluator_proposals`，并在 `__init__` 初始化 `evaluator_error` |
| C-18 | `research/verify/__main__.py:416` | 报告含 U+2212，Windows GBK 控制台 `UnicodeEncodeError`，CLI 在 Windows 上直接崩（`test_skill_standalone` 唯一失败项） | `main()` 开头 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；测试子进程捕获改为 UTF-8 解码；重建 vendored `engine/`、agensi 单文件、Pro zip |

修完后 **C-01 的直接后果消失**：本机全量 `pytest tests -q` 可以一次跑完并产出 summary
（此前会在第 31 个文件处进程自杀）。这是"全量测试通过"第一次成为可验证的陈述。

### 3.6 Round 2 复审（合并前最后一轮正确性审查）

复审提出 5 项，全部成立；先写反例（`tests/cross_market/test_time_semantics.py`，11 项，
修复前 8 failed / 3 passed），再修。

| ID | 问题 | 反例 | 修复 |
|---|---|---|---|
| R2-01 | 未来标签按 `available >= origin+H` 取价格：一条**发生在 origin 之前、但 horizon 之后才到达**的旧行情会被当成未来价格 | 事件 `origin−10s`、到达 `origin+H+30s`，旧实现算出 −10% 的"未来收益" | `first_available_after(..., event_min_ms=target)`：**事件时间与可用时间都必须晚于 origin+H** |
| R2-02 | `ReplayEngine.run(start,end)` 只按事件发生时间限定窗口，未限定可用时间：`end` 之后才到达的数据会进入窗口内状态 | 事件 1000、到达 5000，回放 [0,2000) 旧实现应用了它 | 未给 `decision_now_ms` 时以 `end_ms` 为可用性截止；被挡事件计入 `skipped_not_yet_available`（可见，而不是消失） |
| R2-03 | `_PointInTime` / `ReplayState` 按**到达顺序**覆盖状态：迟到旧行情覆盖更新行情 | 事件 0s/30s(迟到)/60s，在 120s 时刻旧实现取到 30s 的 150 而非 60s 的 200 | 状态按**事件时间**取最新（同 event_ts 才允许后到覆盖）；`ReplayState` 增加 `*_event_ts` |
| R2-04 | `is_researchable()` 只查映射表是否存在；`load_mapping` 到期时间解析失败 fail-open | 过期市场仍可研究；`event_expiry: '不是日期'` 被当作永不过期 | `is_researchable` 要求 `not expired and not expiry_unknown`；解析失败置 `expiry_unknown=True`；`researchable_markets` 同步 |
| R2-05 | 5 秒采样 / 5 分钟视界时样本高度重叠，固定 20 点块不够；点数会虚高显著性 | 300 个 5s 点（150 个共同点）旧实现判 SUPPORTED，实际只有 2 个独立窗口 | `block = ceil(horizon/step)`；门槛用**有效独立共同点** `aligned_n/block`；另报非重叠子样本 CI，全样本显著但非重叠不显著 → 降级 INCONCLUSIVE |

R2-05 的边界说明：这一版做的是**块长与有效样本量**的修正，不是完整的时间序列推断
（没有 HAC/Newey-West，也没有对多假设做校正）。它解决的是"用重叠点冒充独立证据"，
不解决"多次实验后的选择性偏差"——后者在 Sprint 2 与 FDR 一起做。

修复后：`tests/cross_market` **82 passed**；全量 **492 passed, 1 skipped**。

---

## 4. 研究设计问题（代码修不好，必须改设计）

### 4.1 映射市场与主视界不匹配（P0，设计级）

`config/market_mapping.yaml` 三个市场：
- `will-bitcoin-dip-to-60k-by-december-31-2026`（2027-01-01 到期）
- `will-bitcoin-reach-1000000-by-december-31-2026`
- `will-bitcoin-dip-to-50000-by-december-31-2026`

而 `primary_horizon: 5m`。这些市场的 YES 概率对 5 分钟价格几乎不敏感（delta 接近 0），
"概率变化 → 5m 方向"的因果链不存在。后果：
- B 臂几乎恒弃权 → `aligned_n` 永远到不了门槛 → 实验永远 INCONCLUSIVE；
- 若为了"跑出结果"去放宽 deadband，就会把噪声当信号。

**修法**：换成与主视界同量级的市场（小时/日级 "BTC up or down"、"BTC above X by EOD"），
并在 mapping 里记录 `horizon_match: true/false`；不匹配的市场只允许做展示，不允许进实验。

### 4.2 C 臂定义与"增量"指标

C = A 与 B 同向才出手，则 C 的方向集合是 A 的子集。在"共同点命中率"这个指标上，
C−A 数学上恒为 0。本次修复把主指标改成逐点 payoff（弃权=0），这才可能反映
"B 帮 C 躲过亏损"的价值。后续 Sprint 2 需要进一步区分：
- **选择性价值**（少做但更准）：看 coverage 与单位交易收益；
- **信息价值**（同样出手但更准）：需要 C 允许 B 修正方向，而不是只做否决。
两者必须分开报告，否则永远说不清 Polymarket 到底有没有用。

### 4.3 Polymarket WS 仍是未验证状态

`docs/cross-market-v2.2.md` 记载：WS 握手成功但订阅后 0 数据帧，
`WS_STREAM_UNVERIFIED`。REST 轮询是当前唯一可用链路。Sprint 1 的 WS 任务必须
先拿到真实消息样本（含 `event_type=book` 与 `last_trade_price`），再谈延迟优势；
在拿到之前，任何"WS 比 REST 快"的说法都不成立。

### 4.4 网络链路可信度

部署节点 DNS 落在 `198.18.0.0/15`（fake-ip），本地代理对 TLS 做 MITM。
`polymarket_rest.py` 为跑通链路加载了 Windows ROOT 证书库并关闭吊销检查。
这解决了"能连"，但没有解决"连到的是不是真的 Polymarket"。
研究结论落地前，至少要做一次**带签名的响应校验**（例如用官方 Gamma 的
`umaResolutionStatus` 与链上结算结果交叉核对），并在 `network_probe.json` 里
保留 `SUSPECTED_FAKE_IP_OR_PROXY` 判定。

---

## 5. 修复验证（可复现）

```bash
# 1) 新增回归用例：修复前 17 failed / 1 passed
python -m pytest tests/cross_market/test_data_correctness.py -q
# 修复后：18 passed

# 2) cross_market 全量
python -m pytest tests/cross_market -q
# 修复后：69 passed

# 3) 全量测试（CI 口径）
python -m pytest -q tests
```

新增用例与缺陷一一对应：

| 用例 | 覆盖 |
|---|---|
| `test_label_is_relative_return_not_price` | S0-01 |
| `test_label_from_store_computes_forward_return` | S0-01 |
| `test_label_requires_future_beyond_horizon` | S0-02 |
| `test_abc_compares_only_common_decision_points` | S0-03 |
| `test_arm_a_uses_price_change_not_level` / `test_arm_a_abstains_inside_deadband` | S0-04 |
| `test_arm_b_maps_probability_change_to_underlying_direction` | S0-05 |
| `test_align_each_point_uses_data_available_then` / `test_align_stops_at_realtime_ceiling` | S0-06 |
| `test_poly_yes_no_tokens_are_normalized` / `test_poly_inconsistent_books_are_flagged` | S0-07 |
| `test_best_bid_ask_not_level_zero` | S0-08 |
| `test_missing_server_timestamp_is_rejected` | S0-09 |
| `test_duplicate_book_hash_not_stored_twice` | S0-10 |
| `test_replay_orders_by_availability_not_event_ts` / `test_store_query_by_availability` | S0-11 |
| `test_cvd_uses_closed_kline_only` | S0-12 |
| `test_cost_model_includes_funding` | S0-13 |

同步修改的旧测试（它们把 bug 固化了）：

| 旧测试 | 旧断言 | 新断言 |
|---|---|---|
| `test_experiment.py::test_label_from_store_future_only` | `future_return == 67500` | 相对收益 |
| `test_experiment.py::test_align_after_ingest_includes_poly` | `polymarket["bid"]` | `polymarket["yes_bid"]` |
| `test_experiment.py::test_align_clock_skew_is_conservative` | 时钟超前时用 `now` 回填 | 按 t 逐点取数，`t=base` 无数据、`t=base+4s` 可用 |
| `test_experiment.py::test_abc_supported_when_c_beats_a` | 无 delta 的"伪 SIGNAL" | A 恒看涨、B 区分方向，断言 payoff 增量 CI > 0 |
| `test_collectors.py::test_recorder_captures_missing_server_timestamp` | 退化为 recv | 丢弃 + 计数 |

---

## 6. 遗留风险与后续任务

| 优先级 | 任务 | 归属 |
|---|---|---|
| P0 | 替换 mapping 为与主视界匹配的市场；加 `horizon_match` 校验 | Sprint 3 前置 |
| P0 | WS 消息流验证（拿到真实帧后再谈延迟） | Sprint 1 |
| P1 | 多周期 K 线（5m/15m/1h/4h）与指标层；当前 cross_market 只有快照 | Sprint 1 |
| P1 | `market_mapping` 解析失败 fail-closed | Sprint 1 |
| P1 | 时钟偏移滚动估计 + `CLOCK_SUSPECT` 状态 | Sprint 1 |
| P1 | ticker `volume` 口径标注 / 改用 kline 成交量 | Sprint 1 |
| P2 | `align_features` 增加市场级过滤（多市场共用 store 时） | Sprint 2 |
| P2 | 旧测试中仍有"测试跟随实现"的痕迹，加断言前先问"这个值在现实里应该是什么" | 持续 |

### 6.1 既有资产中已识别、但本 Sprint 不修的缺陷（按 Sprint 分配）

| 编号 | 位置 | 问题摘要 | 归属 |
|---|---|---|---|
| C-03 | `orchestrator.py:87-88` vs `counterpart.py:32-34` | E 与 C 的止损/止盈参数不同，却被当成"六 agent 增量"的比较；E vs C 的差值包含退出参数差异 | Sprint 2（先统一 exit policy，再重跑历史结论） |
| C-05 | `replay/engine.py:314`、`judge.py:213` | 按 mint 反查"最新 candidate"，影子价/LLM 分挂到另一轮 | Sprint 2（持仓行写入 candidate_id） |
| C-06 | `replay/engine.py:222-227` | 冷却闸门查活账本，回放结果随时间漂移、不可复现 | Sprint 2 |
| C-07 | `replay/engine.py:255,260-271` | 退出只在 10 分钟窗口内评估；`TRAILING_STOP` 分支不可达 | Sprint 2 |
| C-08 | `verify/__init__.py:236-298` | 名为 walk-forward，实为 holdout split；不重拟合、按 bar 等分 | Sprint 1（改名或真滚动） |
| C-09 | `audit/binance.py:133`、`verify/data.py:113` | K 线缓存只看首尾，内部缺口检测不到 | Sprint 1 |
| C-11 | `replay/signals.py:59-72` | 三个独立 `MAX()` 拼出一行，价格与时间戳不同源 | Sprint 1 |
| C-12/13 | `replay/signals.py:280`、`memehunter.py:714` | Jupiter 端点字段/包裹层未统一，异常被吞 | Sprint 1 |
| C-16 | `verify/metrics.py:43-55` | `sum_pct` 线性、`max_dd` 复利；`nets` 被排序后当原始序列；重叠仓位 | Sprint 1 |
| C-17 | `replay/calibration.py:68` | 同一 candidate 多轮研判重复计入 | Sprint 2 |
| C-19 | `verify/__init__.py:315` 等 | `fromtimestamp` 无 tz，报告时间整体偏移 | Sprint 1 |
| C-22 | 全仓库 | 无多重检验校正（网格 ≤64 格、selectivity 42 对比） | Sprint 2（FDR 列进 verdict） |
| C-28 | `scripts/dashboard.py:48` | 60 秒采样配小时级年化因子，Sharpe 差 √60 | Sprint 4（看板重做时一起） |

这些没有被忘掉：它们被明确排除在 Sprint 0 之外，原因是**修复它们需要改实验口径或数据模型，
会把"数据正确性"这一个交付变成三个**。Sprint 0 的边界是：让现有结论不再是被 bug 造出来的。

---

## 7. 验收口径（Sprint 0 部分）

- [x] 数据正确性缺陷全部有回归用例，且用例在修复前失败、修复后通过；
- [x] `tests/cross_market` 全绿（71 passed）；
- [x] 采集器不再用本地时间冒充事件时间；重复盘口不重复入库；
- [x] 标签是相对收益，未来价格严格晚于 horizon；
- [x] A/C 比较有共同决策点、有样本量、有配对 payoff 口径；
- [x] 设计文档给出五层架构、数据契约、风控与 14 天路线（`docs/design/openalpha-v1-architecture.md`）；
- [x] 全量 `pytest tests` 一次跑完：**492 passed, 1 skipped, 0 failed**（本机 62s；
      `roundlock` 修复前会进程自杀，`test_skill_standalone` 因 GBK 必失败）。
- [x] Round 2 复审的 5 项（R2-01..R2-05）全部有先失败后通过的反例测试；
      时间语义（事件时间 / 可用时间 / 未来标签）三处定义已用测试钉死。

---

## 8. 本机测试执行备注

修复 `roundlock` 之前，本机（Windows / Python 3.12）全量 `pytest -q tests` 会在
`test_roundlock.py` 处**进程自杀**（无 traceback、无 summary）——那不是"环境玄学"，
是 `os.kill(pid, 0)` 在 Windows 上的语义（见 §3.5 C-01）。修完后一次跑完：

```
481 passed, 1 skipped in 35.41s
```

1 个 skip 是 `test_migrate_ledgers.py` 的环境相关跳过（缺 symlink 权限），
与本次改动无关。CI（Ubuntu / Python 3.11）不受 Windows 特有缺陷影响，
但也因此**永远发现不了 C-01**——这是建议给 CI 加 Windows runner 的直接理由。

---

## 9. 附录：既有资产复用清单（Sprint 1 起按此接线）

| 模块 | 文件 | 能力 | 处置 |
|---|---|---|---|
| 证伪引擎门面 | `research/verify/__init__.py` | `verify(spec, candles, entries, funding, exit_signals)` → 回放/成本/障碍/对照/裁决/诊断 | 改造复用：V1 的策略验证入口 |
| Spec 契约 | `research/verify/spec.py` | 声明式 `falsify_if`、walk-forward、参数网格校验 | 直接复用：假设契约载体 |
| 随机对照 | `research/verify/control.py` | bootstrap CI + selectivity | 已修 C-02 后复用 |
| 诊断 | `research/verify/diagnosis.py` | 七类诊断 + 可验收的下一步实验 | 直接复用（最干净的一块） |
| 报告 | `report.py` / `html_report.py` | Markdown / 自包含 HTML（曲线、成本柱、热图） | 直接复用：看板雏形 |
| 契约锁 | `research/verify/contract_lock.py` | `falsify_if` 的 sha256 锁，改规则即失败 | 直接复用 |
| 路径回放 | `research/audit/pathsim.py` | bar 内 TP/SL 冲突按最坏解、maker 需穿越、funding 折算 | **V1 回放内核** |
| 影子标签 | `research/replay/labels.py` | 5/15/30/60m 前向曲线，无回填 | 直接复用（口径最严） |
| 成本 | `research/replay/costs.py` | AMM 冲击 + LP 费 + 链上费 | 复用公式，并入统一成本模型 |
| 选择性审计 | `research/replay/selectivity.py` | 14 队列 lift + CI + 尾部占比 | 复用范式 |
| 决策链 | `research/pipeline/agents.py` | SEARCH→RISK→GMGN→JUDGE→FOMO→EXIT | 骨架复用为 L3 |
| LLM 研判 | `research/pipeline/judge.py` | 只重排不放行 + prompt hash + 原始响应留痕 | 直接复用（合规形态正确） |
| 提案器 | `research/pipeline/evaluators.py` | 只提案不落地 | 直接复用（反自动过拟合） |
| 晋级阶梯 | `research/live/ladder.py` | replay→dryrun→probe→pilot→scale，降级自动、升级需人 | 直接复用为准入闸门 |
| 风控闸门 | `research/copywatch/risk.py` | 11 项确定性检查 + 稳定机器码 | **L4 蓝本** |
| 去重范式 | `research/copywatch/store.py` | `event_id` 主键 + `INSERT OR IGNORE` | 直接复用 |
| 仓位缩放 | `research/copywatch/sizing.py` | 按目标钱包规模缩放 + floor/cap | 直接复用 |
| 数据纪律 | `research/core.py` | 已收盘 K 线校验、原子写、幂等落盘 | 纪律范本 |
| 纸面执行 | `research/memehunter.py`（`Desk`） | 滑点/双边费/peak/权益/健康度 | 改造为统一 PaperBroker |
| 信号信任边界 | `user_data/strategies/signal_gate.py` | 外部信号 schema/来源/时效白名单 | 直接复用 |
| 安全基线 | `scripts/safety_check.py` | 启动前 20+ 断言（dry_run/无凭证/只读挂载） | 直接复用，扩展风控断言 |
| 快照导出 | `scripts/export_experiment.py` | SQLite backup + 凭证脱敏 + git rev | 直接复用（本次的 `ledger.snapshot` 同源） |
| 旧账本 | `legacy_v1/paper_agent.py` | 极简纸面账本（成交价/费/滑点/权益） | 候选 PaperBroker 记账层 |

**重复实现要收敛**（同一能力多份，详见审计过程记录）：成本模型 5 份、前向收益 5 份、
绩效统计 6 份、纸面账本 4 份、风控闸门 3 份。V1 的规则：一个能力只留一份实现，
其余改为调用；留哪份在上表"处置"列已标明。

---

## 10. 给下一个 Sprint 的一句话

先把"每个决策点上，我到底知道什么"钉死（数据契约），再谈策略。
这一层只要漏一个前视，后面所有漂亮的曲线都是假的。