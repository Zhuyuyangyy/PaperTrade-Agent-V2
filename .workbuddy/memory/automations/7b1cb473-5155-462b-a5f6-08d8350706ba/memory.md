# Automation memory — 夜间工作会话（7b1cb473）

## 2026-10-30 01:40 运行：完成 U3（walk-forward 仪器）

- 队列状态：U1 ✅ f9d1a69 / U2 ✅ b3a7dd1 / **U3 ✅ 466ab5f** / U4-U6 未开始
- commit 466ab5f 已推 feature/replay-engine-v1 并 ff-merge 进 main，两分支 CI 均绿（run 40922541801 main / 40922536694 feature，各约 42s）
- 测试基线更新：**287 passed, 2 skipped**（原 276 + U3 新增 11）
- 实现要点：`research/verify/spec.py` 加 WalkForwardSpec；`__init__.py` 重构出 `_battery()`（一段一电池，样本内外同仪器）+ `_walk_forward()` + `_verdict(..., wf=)`（checks 带 scope，样本外触发→整体 FALSIFIED，样本外样本不足→INCONCLUSIVE）；`signals.py` 加 `bar_bounds()`（对照组抽签区间，样本外对照组只在该段内抽）；`report.py` 新增第 5 节两段对比，判定/披露节顺延为 6/7
- 关键设计决定（后续单元别改坏）：① 样本内外两段都在全窗口 K 线上推演（入场之后的 K 线对入场仍是未来，截断会制造假"未决"）；② 样本外传播只认 spec 里**事前声明**的同一批规则，不发明新规则；③ 披露节在 WF 开启时明说"引擎无法验证参数是否用全窗口调优"
- 测试构造资产（tests/test_verify.py 内可复用）：`_dip_recovery_candles` + `_grind_down_candles` 组成种子化过拟合序列（1500 根：全窗口 +87.1% 通过、样本内 +163.5%、样本外 -76.4% 触发），是唯一能隔离"样本外单独致 FALSIFIED"的构造
- 给 U4 的提示：param_grid 应复用 `_battery` 模式（一段一电池），演示spec改完记得同步重生成 docs/verify-sample-report.md/json（本地有 runtime/audit/market.sqlite 缓存，--demo 走 29347 根全量序列，约 54s）
- 给 U6 的已知文档债（已记入 LOG.md）：docs/cases/*.md 四份案例章节号过时（5→6、6→7）但数字未变；README/article 缺 U3 仪器说明
