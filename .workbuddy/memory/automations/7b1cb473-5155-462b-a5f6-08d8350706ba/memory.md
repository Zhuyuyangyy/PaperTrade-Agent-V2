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

## 2026-10-30 08:05-08:50 运行：U5（落地页，Pages 受阻）+ U6（文档同步，推送待网络）

- 队列状态：U1 ✅ f9d1a69 / U2 ✅ b3a7dd1 / U3 ✅ 466ab5f / U4 ✅ 221f86e / **U5 🟡 117d4cc（实现完成并已推，Pages 托管受阻）** / **U6 ✅ 0fad806（本地完成，推送被网络中断卡住）**
- U5：新增 `docs/index.html` 自包含单页落地页（pitch/6 仪器/30s demo/案例矩阵/价格/边界+联系占位，无外部资源），README「现在：策略证伪服务」节加链接。117d4cc 已推 feature + ff-merge main + 推 main
- **U5 阻塞（外部，非代码）**：`gh api POST repos/Zhuyuyangyy/PaperTrade-Agent-V2/pages` → 422「current plan does not support GitHub Pages」；`gh api repos/...` 确认 `private: true`。私有仓库启用 Pages 需 GitHub Pro/Team。未擅自公开仓库（=公开全部研究档案，属用户级决策）。启用命令已存 LOG.md，用户三选一：转 public / 升级 plan / 保持 README 相对链接
- U6：README 产品化章节 4→6 仪器表（walk-forward/参数网格两行）、样例报告描述补样本外 -265.31% 与网格 0/9×0/9、测试数 36→60（全套 297 passed, 2 skipped）、落地页链接；article §三 六件仪器（加 5/6 条目）、新增 §五「过拟合检测」专节、原五六七顺延、测试数刷新；四案例用当前引擎重生成（`python -m research.verify --spec research/verify/samples/cases/*.json --out docs/cases/*.md --json`）
- **四案例回归核对结果（关键证据）**：金融数字逐位一致——case1 -505.65%/-610.45%、case2 +34.35%/-96.69%/CI[-0.27,+0.35]、case3 -1144.10%/-1257.62%、case4 -453.20%/-505.24%；diff 仅：节号 5→7、6→8（固定编号，WF/grid 关闭时缺席）、判定表新增「范围」列（U3 引入）、JSON spec 回显新增 walk_forward/param_grid enabled:false 块、时间戳。证明 U3/U4 未污染非 WF/非网格路径
- 案例报告 §4→§7 跳号的处理：report.py 固定编号是 U3 测试钉住的有意设计（tests/test_verify.py:566-567 注释 "criteria renumbered, still last"），今夜未改代码；在 cases/README.md 补了节号说明避免读者误以为缺页
- **遗留待办（网络恢复后第一件事）**：`git push origin feature/replay-engine-v1 && git push origin main`（0fad806，本地 main 已 ff-merge到该 commit，两分支各差 1 个 commit）；推完查两分支 CI（gh run list）；U5 Pages 决策留给用户
- 环境注记：GitHub 网络在 08:40 左右环境级瞬断（git schannel handshake 失败、gh api EOF、curl 无响应），重试两次仍断，按规则停止重试；本地仓库状态干净（feature 分支 HEAD=0fad806，main=0fad806）

## 2026-10-30 08:50 运行：增量打磨轮（U2-U6 已完成后的验证单元）

- 队列状态：U1-U6 全部完成（U5 🟡 Pages 待用户三选一）；0fad806 已被 08:37 收尾会话推送成功，两分支同步在 8477d14
- 本轮动作（PLAN 增量规则，未开新功能）：① 复跑全量测试 **297 passed, 2 skipped**（两次执行一致）② 两分支 CI 绿（8477d14：main 40980839869 ✅37s / feature 40980835986 ✅46s）③ reviewer 视角通读夜间 10 commit 全量 diff（f9d1a69~1..8477d14，30 文件 ~5900 行）：spec 校验、grid 每格口径、WF 切分与段内裁决重算、_verdict 样本外传播规则、report 渲染、U1 死区修复——未发现逻辑或口径问题 ④ 四案例用当前引擎重跑：md 报告除生成时间戳外**逐字节一致**，JSON 除 generated_at 外**完全一致**；头条数字与 U6 记录逐项吻合（case1 -505.65%/CI[-0.70,-0.03]、case2 +34.35%/CI[-0.27,+0.35]、case3 -1144.10%、case4 -453.20%，四案全 FALSIFIED）
- **发现并修复（本轮唯一问题）**：样例报告 docs/verify-sample-report.md/json 生成于 U4 开发中段（时间戳 02:57），0 正格专用措辞定稿（__init__.py:398 / report.py:85「edge 在这个参数空间里不存在，更谈不上稳健」）后未重生成，报告 4 处呈现引擎已不再输出的旧措辞（裁决理由 ×1、热力表标记 ×2、判定表明细 ×2）——与 U2 win-log 同类的文档-代码不一致，本项目零容忍。修法=用当前引擎从同一行情缓存重生成（commit 081eafa），**金融数字逐位不变，零代码零规则改动**（规则先于数字）
- 验证方法沉淀（下次直接用）：案例回归用 `--out <tmp>/x.md --json` 重生成为临时文件再 diff（`--json` 是布尔开关，JSON 自动与 out 同名输出）；JSON 深比对用 python `json.dumps(sort_keys=True)` 忽略 generated_at；Windows python 不认 MSYS /tmp 路径，`python -c` 里必须用 D:/... 真实路径
- 081eafa 已推两分支并 ff-merge，CI 双绿（main 40982134113 ✅41s / feature 40982130014 ✅44s）
- 给下一个运行的提示：U5 Pages 三选一仍待用户（转 public 后启用命令在 LOG.md 与 night-report 里，一条命令）；若用户选转 public，启用后验证落地页可访问即补完 U5；除此之外队列已空，按 PLAN 只做验证/打磨轮
