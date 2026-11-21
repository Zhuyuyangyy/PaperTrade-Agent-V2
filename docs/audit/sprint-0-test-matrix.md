# Sprint 0 测试矩阵

## 汇总（最终）

| 口径 | 结果 |
|---|---|
| 全量 `python -m pytest tests -q` | **510 passed, 1 skipped, 0 failed**（51s，一次跑完） |
| 跳过 | 1（`test_migrate_ledgers.py`，环境缺 symlink 权限，与本次改动无关） |
| 本次新增回归 | `test_data_correctness.py` 19 项 + `test_time_semantics.py` 11 项 + `test_ledger_snapshot.py` 3 项 + 5 项既有测试新增用例 |
| 核心改动面 | `tests/cross_market` 51 → **82 passed**；`tests/test_verify.py` 116 → **118 passed** |
| 修复前无法得到的数字 | 全量测试在 `test_roundlock.py` 处进程自杀（Windows `os.kill(pid,0)` = `TerminateProcess`），从来没有 summary |

### 修复前后对照

| 项 | 修复前 | 修复后 |
|---|---|---|
| 全量 `pytest tests` | 跑到第 31 个文件进程自杀，无 summary | 510 passed, 1 skipped |
| `tests/cross_market` | 51 passed（其中多个断言把 bug 当契约） | 82 passed |
| `tests/test_roundlock.py` | 4 项未完成，进程被杀 | **5 passed**（含新增 `_alive` 用例） |
| `tests/test_skill_standalone.py` | 5 passed / 1 failed（GBK U+2212） | **6 passed** |
| `tests/test_pro_package.py` / `test_agensi_single_file.py` | 通过但子进程解码告警 | 通过，告警消除 |

### Round 2（合并前复审）

| 项 | 修复前 | 修复后 |
|---|---|---|
| `test_time_semantics.py` | **8 failed / 3 passed** | 11 passed |
| 全量 | 481 passed | 492 → **510 passed**（含 C-20 整点边界 5 项） |
| 标签 | 迟到旧价可当未来收益 | 事件时间与可用时间都须晚于 horizon |
| 回放 | 窗口结束后到达的数据进入窗口状态 | 以 `end` 为可用性截止并计数 |
| PIT 状态 | 到达顺序覆盖（旧价覆盖新价） | 事件时间取最新 |
| 市场可研究性 | 过期市场仍可研究；坏到期时间 fail-open | 两者都 fail-closed |
| A/B/C 门槛 | 按重叠点数 | 按 `block = ceil(horizon/step)` 折算 + 非重叠稳健性 |

## 1. 本次新增/修改的用例

| 用例 | 覆盖缺陷 |
|---|---|
| `test_data_correctness.py::test_label_is_relative_return_not_price` 等 2 项 | S0-01 标签写成价格 |
| `test_label_requires_future_beyond_horizon` | S0-02 标签窗口 |
| `test_abc_compares_only_common_decision_points` | S0-03 A/C 索引截断 |
| `test_arm_a_uses_price_change_not_level` / `..._abstains_inside_deadband` | S0-04 A 臂恒做多 |
| `test_arm_b_maps_probability_change_to_underlying_direction` / `..._abstains_when_books_inconsistent` | S0-05 / S0-07 方向映射 |
| `test_align_each_point_uses_data_available_then` / `test_align_stops_at_realtime_ceiling` | S0-06 实时模式回填历史 |
| `test_poly_yes_no_tokens_are_normalized` / `test_poly_inconsistent_books_are_flagged` | S0-07 YES/NO 归一化 |
| `test_best_bid_ask_not_level_zero` | S0-08 盘口排序 |
| `test_missing_server_timestamp_is_rejected` | S0-09 recv 冒充事件时间 |
| `test_duplicate_book_hash_not_stored_twice` | S0-10 重复采集 |
| `test_replay_orders_by_availability_not_event_ts` / `test_store_query_by_availability` / `test_query_available_keeps_late_events_before_window` | S0-11 可用性排序 |
| `test_cvd_uses_closed_kline_only` | S0-12 CVD 用了未收盘 K 线 |
| `test_cost_model_includes_funding` | S0-13 资金费未计入 |
| `test_roundlock.py::test_alive_detects_our_own_process` | C-01 Windows liveness 探测杀进程 |
| `test_verify.py::TestControl::test_random_control_samples_without_replacement` / `..._reports_requested_and_effective_size` | C-02 对照组有放回抽样缩水 |
| `test_timeout_replay.py::test_a_stop_after_the_deadline_is_priced_at_the_deadline` | C-04 未来止损回填短期期限 |
| `test_ledger_snapshot.py`（3 项） | C-10 WAL 账本复制丢末端 |
| `test_memehunter.py::test_health_halt_blocks_new_positions` | C-14 停手线未接线 |
| `test_skill_standalone.py` 断言改为 `engine_mode=vendored/repo` | C-18 编码相关断言脆性 |
| `test_time_semantics.py::test_late_old_snapshot_is_not_a_future_label` / `test_future_label_requires_both_event_and_availability_after_horizon` | R2-01 标签事件时间 |
| `test_replay_does_not_apply_events_available_after_window_end` / `test_replay_window_end_is_inclusive_for_availability` | R2-02 回放可用性截止 |
| `test_pit_state_prefers_newer_event_over_late_arrival` / `test_replay_state_keeps_the_newer_event` | R2-03 迟到旧数据覆盖 |
| `test_expired_market_is_not_researchable` / `test_unparseable_expiry_is_fail_closed` / `test_unregistered_market_stays_unresearchable` | R2-04 市场 fail-closed |
| `test_abc_block_scales_with_horizon_over_step` / `test_abc_disjoint_windows_still_supported` | R2-05 重叠采样与块长 |
| `test_core.py::test_demo_survives_the_hour_boundary`（5 个固定时钟参数） | C-20 demo 整点死区随机失败 |

## 2. 同步修改的旧测试（它们把 bug 固化成契约）

| 旧测试 | 旧断言 | 新断言 |
|---|---|---|
| `test_experiment.py::test_label_from_store_future_only` | `future_return == 67500`（价格） | 相对收益 |
| `test_experiment.py::test_align_after_ingest_includes_poly` | `polymarket["bid"]` | `polymarket["yes_bid"]` |
| `test_experiment.py::test_align_clock_skew_is_conservative` | 时钟超前时用 `now` 回填 | 按 t 逐点取数：`t=base` 无数据，`t=base+4s` 可用 |
| `test_experiment.py::test_abc_supported_when_c_beats_a` | 无 delta 的伪信号 | A 恒看涨、B 区分方向，断言 payoff 增量 CI > 0 |
| `test_collectors.py::test_recorder_captures_missing_server_timestamp` | 退化为 recv | 丢弃 + 计数 |
| `test_verify.py::TestControl::test_control_book_keeps_the_accounting_identity` | 依赖"必然有 1 笔未解决" | 恒等式 + 用尾部窗口构造未解决入场 |
| `test_skill_standalone.py` 两处 | 断言中文"独立引擎/仓库引擎" | 断言 ASCII `engine_mode=*` |

## 3. 逐文件结果（最终口径，由全量一次跑完得出）

全量汇总已替代分段执行；分段方式保留在 git 历史里（第一版 matrix）。CI 口径为
`python -m pytest -q tests`，新增文件会被自动收集，workflow 无需改动。

## 4. CI 建议

- 当前 CI 只跑 Ubuntu（`tests.yml`），因此 **C-01 这类 Windows 专有缺陷永远不可见**。
  建议加 `windows-latest` matrix，至少跑 `tests/test_roundlock.py`、
  `tests/test_skill_standalone.py`、`tests/cross_market`。
- 加一条 `python -m research.verify --demo > /dev/null` 的烟雾步骤，
  防止 CLI 编码类回归再次只在用户机器上爆。