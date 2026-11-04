# PaperTrade Falsifier Pro · $29

**一个会杀死交易策略的 AI Skill。** 你的 agent 每次写完策略代码，必须先过这台仪器；规则用 sha256 锁死，迭代在许可范围内进行，连续三次证伪必须诚实认输。

## 30 秒体验

```bash
python install_skill.py          # 装进当前项目的 .claude/skills 和 .cursor/rules
python -m engine --demo          # 看一份真实裁决（合成数据，无需行情缓存）
```

## 这个包装了什么

MIT 引擎（六仪器 + 诊断层）在 GitHub 公开可查——**算法不卖，卖的是工作流**：

| 资产 | 作用 |
|---|---|
| `SKILL.md` | 三铁律状态机：禁止裸交卷 / 契约先行 / 三次证伪必须停。装进 Claude Code 或 Cursor 后，agent 写策略前会被强制走证伪闭环 |
| `engine/` | 完整引擎（与 GitHub 版同源同测），包成 `engine` 包，`python -m engine` 一把跑 |
| `playbooks/outcome_playbooks.md` | 七种诊断结局各自的处方、禁令、验收标准——包括“严禁微调参数凑数字”这类真实作弊手法清单 |
| `presets/contracts/` | 两个判决契约起点（加密合约严苛档 / 股票波段标准档）。复制、改、**然后锁** |
| `install_skill.py` | 纯标准库一键安装，自动探测 Claude Code / Cursor 目录 |

## 契约锁（Pro 的核心机制）

```bash
# 1. 迭代开始前：锁死判决规则
python -m engine --lock-contract --spec strategy.json
#    -> strategy.verdict_lock.json（falsify_if + 策略身份的 sha256）

# 2. 每次审计都带锁
python -m engine --spec strategy.json --lock strategy.verdict_lock.json \
    --candles klines.csv --out report.md --json
#    -> 规则被改过：EXIT 99 CONTRACT_TAMPERING_DETECTED，不出报告
```

锁保护的是**裁判**（falsify_if 全部阈值 + 策略身份），不是**被告**（信号、退出规则随便调——那是实验协议要支持的迭代）。想改规则？允许——那是新实验：重新 lock，从实验 1 重新计数，并对用户明说。

## agent_directive：AI 的编辑许可

每次审计的 JSON 里都带 `diagnosis.agent_directive`：

```json
{
  "outcome": "randomness",
  "allowed_edits": ["重构入场触发逻辑", "增加一个正交过滤条件（趋势/时段/量能任选其一）"],
  "forbidden_edits": ["严禁微调现有均线/指标周期参数凑数字", "严禁修改止盈止损来掩盖入场缺陷", "严禁降低 min_fills 让结论翻盘"],
  "next_experiment_prompt": "保持出场结构完全不变，只改入口：加一个正交过滤器，预期信号数降到约 1/5，重跑后检查对照组差值 95% CI 下界是否 > 0。"
}
```

附带诊断结论的禁令同样生效——同时中两条的策略，按主处方修改时不得违反附带的禁令。

## 没有 K 线？TradingView 用户模式

```bash
python -m engine --trades-csv "List of Trades.csv" --out autopsy.md
```

成本 ×2 压力 + 1000 次 bootstrap 重抽样 + 尾部截断。结论词表诚实收窄（SURVIVED_STRESS / FAILED_STRESS / INSUFFICIENT，**永远不输出 FALSIFIED**——序列级检验不够格），报告第一句就是边界声明。

## 文件清单

```
SKILL.md                     三铁律 + 工作流（装给 agent 读的）
install_skill.py             一键安装
engine/                      引擎（cli.py 入口，_pathsim/_compat 为扁平包兼容层）
playbooks/                   七结局处方手册
presets/contracts/           两个契约起点
references/spec-format.md    spec 契约格式
```

## 诚实的边界（每份报告也印着）

- 离线回放：不建模滑点/部分成交/容量冲击/相关性/交易所风险
- 引擎看不见你的调参历史——全窗口调过的参数，“样本外”只是标签不是事实
- NOT_FALSIFIED 是“没有反证”，不是“已证实”，更不是“去实盘”

## 许可与退款

个人许可，无限次自用。引擎本体 MIT 开源（GitHub: Zhuyuyangyy/PaperTrade-Agent-V2）——你买的是 SKILL.md 工作流、契约 preset、剧本与安装便利，不是算法黑箱。14 天内不满意直接退款，不用解释。
