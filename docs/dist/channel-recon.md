# 分发渠道调研报告 · 2026-10-31

调研方法：平台规则页实查（awesome-quant CONTRIBUTING/AGENTS.md、Show HN 多源交叉、Toku 首页实测）。
结论先行：**所有渠道的物料都已就绪，全部剩余动作都只能在用户账号侧完成**（本环境无用户社交账号凭据）。
下表按建议执行顺序排列。

## 渠道总表

| # | 渠道 | 现状（实测） | 下一步具体动作 | 预计耗时 | 负责 |
|---|---|---|---|---|---|
| 1 | **GitHub Topics** | ✅ 已完成：10 个 topics 生效，description 已改产品描述，homepage 指向落地页 | 无 | — | 我（已做） |
| 2 | **Show HN** | 物料就绪（`submissions.md` 含标题+首评文案）；demo 30 秒可跑、无注册墙——符合 Show HN“一分钟内能试用”硬规则 | 周二至周四 8-11am ET（北京时间 20-23 点）发帖；标题格式 `Show HN: StrategyFalsifier – falsifies trading backtests with pre-declared rules`；**发帖后立即发第一条评论**（文案已备）；第一个小时回每条评论，不防御；绝不求 upvote（HN 投票环检测会杀帖） | 发帖 10 分钟 + 值守 2 小时 | **用户** |
| 3 | **awesome-quant** | 规则已吃透：一项目一 PR、描述以句号结尾、https、放 `## Trading & Backtesting` 节、12 个月内有提交（我们满足）、181 个 open PR（审核慢，正常） | fork → README 加一行 `- [StrategyFalsifier](https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2) - `Python` - Falsifies trading strategies with six instruments and a diagnosis layer; verdicts from rules declared before any number is read.` → PR 描述写项目说明（一项目一 PR，勿带其他改动） | 20 分钟 | **用户**（fork/PR 需账号） |
| 4 | **Toku** | 已注册（agent_id 在凭据档）；服务档位还是旧的 $99/$299 两档；平台 4065 agents、85% 结算、Stripe Connect 提现 | dashboard 更新服务为四档（toku-profile.md 已写好新档位表与输入契约）；Stripe Connect 入驻状态需确认（W-8BEN + 国内银行卡，未完成则只能收不能提） | 30 分钟 | **用户**（dashboard + KYC） |
| 5 | **Agensi** | ZIP（66KB）+ listing 文案 + SHA256 全就绪；免费档含完整引擎 | Creator Studio 提交免费版；收款核验（Zoneless USDC/Solana）完成后再上付费档 | 提交 15 分钟；KYC 数天 | **用户** |
| 6 | **Reddit r/algotrading** | 文案已更新到六件+诊断层+7 案例（`reddit-algotrading.md`）；版规惯例：自推内容需以“我做了 X，数据如下”叙事而非广告 | 看版规 self-promotion 比例限制（通常 9:1）；帖子发“36 小时六 agent 无 edge 所以停了，留下仪器”故事线——这个故事本身就是最好的素材 | 15 分钟 | **用户** |
| 7 | **闲鱼 / 掘金 / 知乎 / 公众号** | 文案齐备（`xianyu-listing.md` / `content/juejin.md` / `content/zhihu.md` / `content/wechat.md`），均已同步 361 测试数与诊断层 | 按 `submissions.md` 的顺序发；内容稿建议明天上午发（今天已晚） | 各 15-30 分钟 | **用户** |

## 每个渠道的关键规则（调研实录，防踩坑）

**Show HN**（多源一致）：
- 周二至周四 8-11am ET；避免周五下午到周日、美国假期
- 标题 plain factual，无 emoji 无感叹号无“revolutionary”；不带 metrics 进标题
- 第一条评论在提交后**立刻**发：问题、技术选择、诚实的限制、一个开放问题
- demo 必须无墙可试——我们的 `python -m research.verify --demo` 是天然优势（30 秒出真实格式报告，零注册）
- 被 flag 不要评论争辩，邮件 hn@ycombinator.com
- 无 traction 允许数天后重发（不要机枪式重发）

**awesome-quant**：
- 一项目一 PR，多个项目一定被拒
- 条目格式必须匹配解析正则：`- [Name](url) - \`Python\` - Description ending with a period.`
- 放 `## Trading & Backtesting`（按功能分类，不按语言）
- PR 描述必须解释项目（空描述自动关闭）
- 注意：需要 rebase 到最新 main（无冲突才会被审）

**Toku**：
- 注册一次 API 调用；服务档位 USD；85% 自动入账；Stripe Connect 真钱
- Agent Feed 可发动态（`POST /api/agents/feed`）——服务更新后可以去 feed 发一条 announce（需要 agent 的 Bearer token，在凭据档）
- job 板流量早期有限，按 gate 标准投入，不建自动交付

## 试单获取（第一个 $129 的真正来源）

渠道是等量，试单是变量。三个可执行切入点：
1. **样张即钩子**：`docs/sales-sample/` 的“止盈止损设多少”案例是任何人都会问的问题——在 HN/Reddit/掘金都用这个故事切入，结尾一句“第一个 10 单免费换匿名案例授权”
2. **GitHub issue 即订单**：免费档入口就是 issue 模板——每个认真提交的 issue 都是 $99/$129 的候选（提交者已经有策略要决策）
3. **竞品评论区**：Agensi 上 Backtest-Validator（$5）和 Trading Strategy Backtest Auditor（$9.99）的评论区——找“想要更多”的买家（这个我无法代看，需要你账号）

## 本环境做不到的（需要你）

所有需要登录态的动作：HN 发帖、fork/PR、Toku dashboard、Agensi Studio、Reddit 发帖、闲鱼/掘金/知乎发布。物料侧我已全部就绪并核对一致。
