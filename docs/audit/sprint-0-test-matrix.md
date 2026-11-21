# Sprint 0 测试矩阵（本机分段执行）

> 目的：本机 Windows 上 `pytest -q tests` 在约 61% 处进程退出（既有环境问题），
> 无法一次拿到汇总。这里按文件隔离执行，记录每一项的真实结果，避免"全绿"是靠
> 一次没跑完的测试得来的假象。
>
> 命令模板：`python -m pytest tests/<file> -q -p no:cacheprovider`
> 每文件 180–300s 超时；失败/超时都保留日志尾部。

## 汇总

| 口径 | 结果 |
|---|---|
| 逐文件通过 | **468 passed** |
| 失败 | 1（`test_skill_standalone.py::test_repo_mode_env_still_works`，既有 GBK 控制台 U+2212 问题） |
| 跳过 | 1（`test_migrate_ledgers.py`，缺 symlink 权限） |
| 未执行 | `test_roundlock.py`（4 项，既有偶发挂起，见 §3） |
| 本次改动核心 | `tests/cross_market` **71 passed**（修复前 51 passed，新增 19 项回归 + 更新 5 项旧断言） |

## 1. 逐文件结果

| 文件 | 结果 |
|---|---|
| `tests/cross_market/` | 71 passed |
| `test_agensi_single_file.py` | 6 passed |
| `test_agent_failures.py` | 6 passed |
| `test_arms.py` | 10 passed |
| `test_arms_report.py` | 3 passed |
| `test_audit.py` | 12 passed |
| `test_bridge_safety.py` | 4 passed |
| `test_calibration.py` | 7 passed |
| `test_contract_lock.py` | 4 passed |
| `test_convertible.py` | 7 passed |
| `test_copywatch.py` | 17 passed |
| `test_core.py` | 19 passed |
| `test_costs.py` | 4 passed |
| `test_csv_auditor.py` | 7 passed |
| `test_dashboard.py` | 8 passed |
| `test_dashboard_server.py` | 1 passed |
| `test_demote.py` | 7 passed |
| `test_equity_sampler.py` | 2 passed |
| `test_exit_loop.py` | 8 passed |
| `test_export_experiment.py` | 1 passed |
| `test_guards.py` | 6 passed |
| `test_judge.py` | 7 passed |
| `test_ladder.py` | 12 passed |
| `test_memehunter.py` | 27 passed |
| `test_migrate_ledgers.py` | 4 passed, 1 skipped |
| `test_moneyflow.py` | 3 passed |
| `test_pipeline.py` | 15 passed |
| `test_pro_package.py` | 3 passed |
| `test_replay.py` | 7 passed |
| `test_report_history.py` | 9 passed |
| `test_room.py` | 8 passed |
| `test_selectivity.py` | 7 passed |
| `test_skill_standalone.py` | 5 passed, **1 failed**（既有） |
| `test_source_retry.py` | 3 passed |
| `test_state.py` | 7 passed |
| `test_strategies.py` | 16 passed |
| `test_timeout_replay.py` | 4 passed |
| `test_verify.py` | 116 passed |
| `test_worker_integration.py` | 5 passed |
| `test_roundlock.py` | 未执行（挂起） |

## 2. 唯一失败项的证据

```
FAILED tests/test_skill_standalone.py::test_repo_mode_env_still_works
```

原因（既有，`docs/cross-market-v2.2.md` 已记载）：`research.verify --demo` 在
Windows GBK 控制台无法打印 U+2212（−），抛 `UnicodeEncodeError`。
本次改动**没有触碰** `research/verify/` 与 `skill/`（见 `git diff --stat`），
CI（Ubuntu / UTF-8）不受影响。

## 3. `test_roundlock.py` 的处理

该文件在隔离执行时会让父守护进程一起退出（`research.pipeline.roundlock.acquire`
会用 `os.kill(pid, 0)` 探测 pid；本机表现为不可控挂起/退出）。它与本次改动无关：
`git diff` 不含 `research/pipeline/roundlock.py` 与 `tests/test_roundlock.py`。
不在本分支修，避免把 Sprint 0 变成"顺手修一切"。

## 4. 回归用例先证伪的证据

修复前（旧实现）：

```
$ python -m pytest tests/cross_market/test_data_correctness.py -q
17 failed, 1 passed
```

修复后：

```
$ python -m pytest tests/cross_market -q
71 passed
```

这一对数字是本次 Sprint 0 的核心证据：测试不是跟着实现写的，
而是先写出"现实应该是什么"，再让旧实现失败。

## 5. CI 口径

CI 只跑 `tests/`（Ubuntu 3.11），不跑本机分段逻辑。新增文件会被自动收集，
不需要改 workflow。若 CI 出现与本机不同的结果，以 CI 为准，并把差异贴进 PR。