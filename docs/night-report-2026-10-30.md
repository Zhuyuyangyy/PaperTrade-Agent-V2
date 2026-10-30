# 夜间收尾报告（2026-10-30 08:34）

> 会话类型：早间收尾。数据来源：`.workbuddy/night/LOG.md` 全量记录 + 本会话重跑验证。
> 所有数字来自真实执行，非估算。

## 一、昨晚完成了什么

| 单元 | 内容 | commit | 验证数字 |
|---|---|---|---|
| U1 | 整点死区修复：staleness → `StaleFeedError`，`run_once` 跳 tick，`run_demo` 锚定 60s | `f9d1a69` | 276 passed, 2 skipped |
| U2 | 补 `docs/dist/win-log.md`（SOP 引用但文件不存在——内部不一致） | `b3a7dd1` | 276 passed, 2 skipped |
| U3 | **walk-forward 仪器**：`walk_forward:{folds:N}`，样本内/样本外两段各跑完整电池，样本外触发事前规则 → 整体 FALSIFIED；报告新增第 5 节两段对比 | `466ab5f` | 287 passed, 2 skipped，两分支 CI 绿 |
| U4 | **参数敏感性网格**：`param_grid`（≤2 个退出参数轴、事前声明），新模块 `research/verify/grid.py`，新规则 `falsify_if.grid_positive_ratio_min`，报告新增第 6 节热力表 | `221f86e` | 297 passed, 2 skipped，两分支 CI 绿；另修一个存量 flaky 测试（每小时必挂，与当晚改动无关，已在 commit message 披露） |
| U5 | 落地页 `docs/index.html`（pitch + 6 仪器 + 30s demo + 案例矩阵 + 价格 + 联系占位，自包含离线可用），README 已链接 | `117d4cc` | 297 passed, 2 skipped，两分支 CI 绿 |
| U6 | 文档同步：README 四件→六件仪器、article 补「过拟合检测」专节、四案例用当前引擎重跑回归核对 | `0fad806` | 四案例金融数字逐位一致（-505.65%/-610.45%、+34.35%/-96.69%、-1144.10%/-1257.62%、-453.20%/-505.24%），仅节号/范围列/spec 回显变化 |

**本会话收尾动作**（08:34）：U6 commit `0fad806` 昨晚因 GitHub 网络瞬断未推送，现已补推；两分支 CI 重跑均 **success**（main `40980660750`、feature `40980655260`）；全量测试本会话重跑 **297 passed, 2 skipped**（26.76s）。

## 二、没完成 / 卡在哪

| 事项 | 状态 | 原因 |
|---|---|---|
| U5 GitHub Pages 启用 | 🟡 **未完成，等你决策** | `gh api POST pages` 返回 422：仓库为 private，GitHub Pages 私有仓库需付费 plan。未擅自转 public（=公开全部研究档案，属用户级决策） |

三个选项（启用命令已备好，转 public 后一条命令即可）：
1. 仓库转 public → 重跑 `gh api --method POST repos/Zhuyuyangyy/PaperTrade-Agent-V2/pages -f build_type=legacy -f 'source[branch]=main' -f 'source[path]=/docs'`
2. 升级 GitHub plan
3. 保持 README 相对链接（仓库内 / 克隆可用，落地页本身完整）

其余单元全部完成，无未修失败。

## 三、当前产品状态

**引擎现在能做什么**（`research/verify/`，输入 spec → 输出试图杀死它的报告）：

- 六件仪器：path-aware 回放（bar 内 SL/TP 同现判 SL）、成本敏感性（×1/×2/×5）、障碍可达性、随机入场对照组（bootstrap CI）、**walk-forward 样本内/样本外**（U3 新增）、**参数敏感性网格**（U4 新增）
- 裁决是事前证伪规则的判定，不是分数：`falsify_if` 先于数字存在；样本不足 → INCONCLUSIVE 永不放过
- 样例成绩（真实 BTC 三年 1310 笔 RSI 抄底策略）：裁决 FALSIFIED——基线净 -505.65%、×2 成本 -610.45%、显著差于随机入场（差值 -0.37%，95% CI [-0.70%, -0.03%]）、样本外段 -265.31%、9 格参数网格样本内外 0/9 全负

**README / 案例 / CI 一致性**：✅ 一致。README 产品章节已是六件仪器表、样例描述含样本外 + 网格数字、测试数 297、落地页链接；四案例报告用当前引擎重生成为回归核对过（数字逐位一致，仅节号 5→7/6→8 与「范围」列变化，`docs/cases/README.md` 已说明）；两分支 CI 绿。

已知小口径（非矛盾，无需动作）：README「仪器：这个项目真正的资产」一节描述研究档案期的四件仪器，与产品章节「六件仪器」是不同范围，各有上下文。

## 四、下一步建议（按优先级）

1. **决定 GitHub Pages 方案**（三选一，见上）——这是 U5 唯一缺口，也是产品对外门面
2. **用新引擎跑一个外部策略**当公开 demo 素材：四案例全是 RSI 系内部策略，对外说服力不如拿一个 GitHub 真实仓库策略过一遍六件仪器
3. 市场分发动作（`docs/dist/` 作战包已备好）：Reddit/X/闲鱼/Toku 文案 + gate 规则，按「首 10 单免费换授权」节奏启动
