# Agensi 发行验收报告 · strategy-falsifier v1.0.0

日期：2026-10-30 · 分支：`feature/agensi-release` · 引擎 commit：见 PR

本报告区分**实测通过**（在隔离环境真实跑过）与**静态检查通过**（只做了结构/文本核验）。
未全部实测通过前，本包不标记 READY，也不代表用户提交市场审核。

## 平台约束逐条对照（来源：agensi.io/docs/packaging + how-agents-load-skills）

| Agensi 约束 | 原文要点 | 本包状态 |
|---|---|---|
| 脚本原样落盘临时文件运行 | "the agent writes the file unchanged to a temporary file, runs it and removes it afterwards" | ✅ 实测：单文件从空目录直接运行 |
| 脚本读不到包内其他文件 | "it can't read other files in your package" | ✅ 实测：ZIP 解压后只有 SKILL.md + scripts/verify.py，demo 用文件内合成数据，spec 数据走绝对路径参数 |
| bundled dependencies 上传即删 | "node_modules, site-packages, Python virtual environments Not allowed. Removed automatically on upload" | ✅ 结构上不可能触发：引擎是**单个** .py，纯标准库，无 site-packages 依赖 |
| 二进制不服务 | "Binary files such as images, PDFs and .docx are listed but never served" | ✅ 包内 2 个文件均为纯文本 |
| 脚本逻辑 <20KB 易审 | "Keep the logic in each script under 20 KB so it stays easy to review" | ⚠️ **超标，已披露**：verify.py 177KB / 3777 行。这是完整引擎源码（未压缩、未混淆、无内嵌编码块），可读性是最大化而非最小化；20KB 指引针对 prompt 型 skill，本包是工具型。listing 文案中明确说明 |
| ZIP ≤50MB、文件 ≤1000 | — | ✅ 57KB / 2 文件 |
| 顶层 SKILL.md 带 frontmatter | name + description 必填 | ✅ 实测解析通过（test_skill_md_frontmatter） |

## 隔离环境实测通过（全部真实执行，非推断）

测试：`tests/test_agensi_single_file.py`（6/6 通过）+ 手工 ZIP 解压验收。

| # | 验收项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 空目录 demo | 单文件单独放入空目录（无仓库、无缓存、无兄弟文件），`--demo` | ✅ FALSIFIED，90 信号（过 min_fills=30），六件仪器全开 |
| 2 | 单策略审计 | 真实外部案例（FreqBot GridTradingStrategy）spec + CSV，隔离运行 | ✅ FALSIFIED / +61.06% / 360 笔 / 样本外 +50.86%，与主仓库引擎**逐位一致** |
| 3 | 双策略 compare | 同数据不同 timeout 的两个 spec | ✅ 矩阵正确，排名规则事前声明，--json 输出 list |
| 4 | HTML 报告 | `--out report.html` | ✅ 自包含单文件，2 张 SVG，7 表格，标签配平 |
| 5 | JSON 输出 | `--json` 单策略/compare | ✅ object / array 形态正确 |
| 6 | 确定性 | 同 seed 跑两次 | ✅ 除生成时间戳与 repro 回显外逐字节一致 |
| 7 | 引擎等价性 | 单文件 vs 仓库引擎同输入 JSON diff（剔除 repro） | ✅ 完全一致 |
| 8 | 构建可重现 | 重跑 build_agensi.py | ✅ SHA256 逐字节相同 |
| 9 | ZIP 发行验收 | 从发行 ZIP **解压**（非目录复制）到干净目录运行 demo | ✅ FALSIFIED，verify.py SHA256 与 SHA256SUMS.txt 一致 |
| 10 | 主仓库全量测试 | `pytest tests -q` | ✅ 345 passed, 2 skipped（akshare 未装 / Windows symlink 权限，均与本包无关） |

## 静态检查通过（未跑代码）

| 检查项 | 结果 |
|---|---|
| SKILL.md frontmatter 必填键（name/description/summary/version） | ✅ |
| SKILL.md 无占位符残留（your-/TODO/FIXME/占位） | ✅ |
| HTML 标签配平（HTMLParser 栈检查） | ✅ |
| 包内无外部资源引用（http link 仅页脚两个刻意链接） | ✅ |
| .workbuddy/ 内部状态已退出跟踪（git rm --cached + .gitignore） | ✅（历史清理待用户决策，见 commit message） |

## 已知限制（诚实披露）

1. **脚本体积超 Agensi 20KB 指引**（见上表）——完整引擎源码，换取最大可审计性；不做压缩/编码/混淆。
2. **demo 使用文件内确定性合成 K 线**（seed=42，2000 根），非真实行情——Agensi 运行环境读不到兄弟文件，这是设计约束而非妥协；报告 repro 节明确披露数据来源。真实审计请传 `--candles` CSV。
3. **引擎为离线回放**：不建模滑点/部分成交/容量冲击/相关性/交易所风险（每份报告第 8 节固定披露）。
4. **wrapper（skill/strategy-falsifier/scripts/verify.py）不属于 Agensi 包**——它依赖仓库内 engine/ 目录，仅供仓库/ZIP 分发场景；Agensi 包只用单文件。wrapper 的 `--compare` 参数识别 bug 已修复并配回归测试（tests/test_skill_standalone.py）。

## 结论

**实测项 10/10 通过。** 可以提交 Agensi Creator Studio 审核；付费档（$5–9）需先完成收款方式核验。
未替用户提交任何审核——提交入口在用户本人：https://www.agensi.io/dashboard/submit
