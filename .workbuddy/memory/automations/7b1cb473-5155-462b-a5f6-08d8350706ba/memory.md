# Automation memory — 夜间工作会话（7b1cb473）

## 2026-10-30 03:05 运行：完成 U4（参数敏感性网格）

- 队列状态：U1 ✅ f9d1a69 / U2 ✅ b3a7dd1 / U3 ✅ 466ab5f / **U4 ✅ 221f86e** / U5-U6 未开始
- commit 221f86e 已推 feature/replay-engine-v1 并 ff-merge 进 main，两分支 CI 均绿（run 40936858644 feature / 40936862833 main，各约 42s）
- 测试基线更新：**297 passed, 2 skipped**（287 + U4 新增 11；另修复 1 个存量 flaky 测试，见下）
- 实现要点：`research/verify/spec.py` 加 ParamGridSpec（axes ≤2 个退出参数、每轴≥2 值、去重、符号校验、max_cells 默认 64；声明 block 即启用，同 walk_forward 约定）；falsify_if.grid_positive_ratio_min 在 spec 加载时校验 (0,1]。新模块 `research/verify/grid.py`：itertools 笛卡尔积，每格一次 simulate（spec.worst_case 口径 + 基准费率），汇总 positive_ratio/median/best/worst，无成交格不计入占比。`__init__.py`：verify() 产 result["param_grid"]={axes, axis_order, max_cells, scopes[]}；WF 开时网格在 `_walk_forward` 内按段算（in_sample_grid/out_sample_grid）并回填段内裁决；`_grid_checks()` 实现声明规则；`_verdict(..., grids=)`。`report.py`：第 6 节（声明行+分布行+每范围热力表+声明规则触发时的标记），判定顺延 7、披露顺延 8，披露新增 grid-on 变体
- 关键设计决定（后续单元别改坏）：① 每格只跑一次回放，不重复造成本敏感性（第 2 节已回答）也不每格跑对照组（对照组问题是入场时机不是退出几何）；② 信号参数不可网格化——改信号=改入场集=另一个实验；③ 0 正格与"刀刃"是两种不同陈述，报告和 check detail 分别措辞（0 正格："edge 在这个参数空间里不存在"）；④ demo 三处 spec（__main__.py 内嵌 DEMO_SPEC + samples/demo_spec.json + demo_spec_csv.json）必须同步改，3×3 TP×SL + grid_positive_ratio_min=0.5 事前声明
- 样本报告已重生成（docs/verify-sample-report.md/json，~95s）：核心数字与 U3 逐位一致（-505.65% 最坏 / -265.31% 样本外 / 对照组差值 -0.37% CI [-0.70,-0.03]），新增网格样本内 0/9、样本外 0/9 全负（RSI 抄底 BTC 在声明的 9 个参数组合下没有一个是赚钱的）
- **存量 flaky 测试修复**（tests/test_strategies.py::test_hybrid_approves_only_last_matching_candle）：信号锚 now-60s，整点后 60s 内锚点跨整点→signal candle_open_ms 与 df 末根对不上→candle_mismatch，每小时必挂一次；在 pristine 树（6b747f1）复现确认与当晚改动无关。修法：锚点改 boundary+30s（上一小时 K 线恰好沉淀、且与 df 末根同小时），整点后 30s 死区内 pytest.skip（U1 pinned 的行为）。原断言零削弱
- 给 U5 的提示：落地页可写"6 仪器"（回放/成本/障碍/对照/walk-forward/参数网格），样本报告第 6 节热力表 + demo（缓存 ~95s / 离线包 ~12s）是现成 demo 素材；GitHub Pages 用 main /docs 路径
- 给 U6 的文档债（已记 LOG.md）：docs/cases/*.md 四份案例章节号再次过时（判定 6→7、披露 7→8；案例 spec 未启用网格，案例报告不会有第 6 节，数字不受影响，重跑时可当"U4 未污染非网格路径"的回归证据）；README 产品化章节与 article 缺 U3/U4 仪器说明；U6 重跑四案例时注意 cases spec 没有 walk_forward/param_grid block（默认关）
