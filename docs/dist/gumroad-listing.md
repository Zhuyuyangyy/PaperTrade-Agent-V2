# Gumroad / Lemon Squeezy 上架文案 · PaperTrade Falsifier Pro（$29）

## 标题（≤ 60 字符）

```
PaperTrade Falsifier Pro — the AI skill that kills bad strategies
```

## 摘要（listing 卡片）

```
Your agent writes a trading strategy. This skill makes it PROVE the strategy
first: rules locked with sha256 before any number, six-instrument battery,
one of seven diagnostic outcomes, and a hard stop after three kills. Works
inside Claude Code and Cursor. Python stdlib only — nothing to install.
```

## 长描述（listing 页）

```
WHAT THIS IS

A falsification skill for AI agents that write trading strategies. It does
not find profitable strategies. It kills unfounded ones — loudly, before
they cost anyone money.

A backtest curve is a claim. This skill tests the claim with six
instruments: path-aware replay (a bar containing both your stop and your
target resolves to the stop by default), cost sensitivity at 1x/2x/5x your
declared fee, barrier reachability, a random-entry control group with
bootstrap confidence intervals, out-of-sample walk-forward, and a parameter
grid. Then it names the failure: one of seven outcomes — insufficient /
control-failed / cost-sensitive / unverified edge / overfitting / barrier /
not-falsified — each with the evidence from YOUR run and a next experiment
with an acceptance criterion.

THE PROBLEM IT SOLVES

Ask an agent for a trading strategy and you get plausible code with a
plausible backtest — and no way to know if the edge is real. The median
retail backtest-to-live gap is roughly -14 percentage points of return.
This skill makes "write me a strategy" a two-step conversation: the code,
and then the verdict. And when the verdict is bad three times in a row, the
skill's iron rules force the honest sentence: "this direction is falsified;
further iteration would be curve-fitting noise."

THE ANTI-CHEAT LOCK

Agents under pressure quietly move the goalposts: the rule that just fired
gets relaxed, the threshold slides. The contract lock seals your verdict
rules with sha256 BEFORE the first run; any later change exits 99
(CONTRACT_TAMPERING_DETECTED) and produces no report. The lock protects the
judge, not the defendant — signals and exit rules stay freely tunable,
because that is the iteration the protocol exists to support.

THE EDIT LICENCE

Every verdict carries agent_directive: what the agent MAY change, what it
MUST NOT (parameter fiddling, exit tuning, moving the evidence bar — each a
real way to make a dead strategy look alive), and the next experiment
verbatim. Iteration happens inside the licence, not around it.

ALSO INCLUDED

- Trade-records mode: TradingView "List of Trades" CSV in, autopsy out —
  cost stress, resample bootstrap, tail-trim — for buyers who never write
  Python. The limits are stated first (no candles: no path replay, no
  control group — the verdict vocabulary is honestly smaller).
- Outcome playbooks: per-outcome prescriptions, forbidden-move catalogue,
  acceptance criteria.
- Two contract presets (crypto perp strict / equity swing standard).
- One-command installer for Claude Code and Cursor.

REQUIREMENTS

Python 3.10+. The engine is stdlib-only: no installs, no network, no
accounts. 30-second demo included.

HONEST LIMITS

Offline replay: no slippage, partial fills, capacity impact, correlated
positions, or exchange risk. The engine cannot see your parameter search
history. NOT_FALSIFIED means "not disproven" — never "trade this". Every
report prints these limits on its face.

LICENCE & REFUND

Personal licence, unlimited use. The engine itself is MIT and public on
GitHub — you are buying the workflow (SKILL.md iron rules), the contract
presets, the playbooks, and the installer, not an algorithm black box.
14-day refund, no questions.
```

## 定价与产品结构（全渠道一致）

| 产品 | 价格 | 内容 |
|---|---|---|
| GitHub 仓库（MIT） | 免费 | 完整引擎：六仪器 + 诊断层；CI 实测 396 passed / 1 skipped |
| **Falsifier Pro（Gumroad）** | **$29** | SKILL.md 三铁律 + 四条 guardrails + 契约锁 + agent_directive + 剧本 + presets + 安装器 + TradingView 模式 |
| 人工审计（Toku/issue） | $99 | 人工跑 + 数据核验 + 人工复核诊断 + 变体矩阵 + 定制实验 + 录屏解读 |
| 深度审计（人工） | $299 | $99 全部 + ×10 成本压力 + 数据刷新复跑 + 60 分钟解读 |

$99 已含原 $129 档的全部人工内容。五档改三档：档位越少，决策越快；"人工复核"本该是这个价位的标配。

## 上架清单

1. ZIP：`skill/strategy-falsifier-pro/papertrade-falsifier-pro-v1.0.0.zip`（204KB，32 项）
2. SHA256：同目录 `SHA256SUMS.txt`
3. 安装验证记录：解压 → `python install_skill.py` → `python -m engine --demo` → FALSIFIED + 诊断正常（已实测）
4. 退款政策写进 listing（14 天无理由）
5. 定价 $29 USD
