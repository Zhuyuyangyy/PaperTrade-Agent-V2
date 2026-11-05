# 分发作战包（2026-11-05）

目标：30 天内拿到 **3 个付费 job 或 10 个认真免费申请**。所有渠道只发布可复现数据；不荐股、不带单、不代管资金、不承诺收益。

## 当前状态

- GitHub 免费版：已合并到 `main`，Pages 已构建。
- 测试口径：CI `pytest -q tests` 为 **396 passed / 1 skipped**；Windows 本地与全仓库口径差异见 `submissions.md`。
- Gumroad：Pro ZIP、listing 文案和 SHA256 物料已准备，尚待账户登录与正式上架。
- Toku：现有 `StrategyFalsifier` profile 已在仓库联系入口中引用；只更新现有主页，不重新注册。
- Agensi：免费 Skill ZIP 已准备；市场提交尚未完成。Agensi 页面只发免费版，不放外部 checkout 或付费导流。
- Show HN、awesome-quant、闲鱼、知乎：文案已准备，尚未公开投稿。

## 发布顺序

| 顺序 | 渠道 | 动作 |
|---:|---|---|
| 1 | Gumroad | 上架 Falsifier Pro，$29；上传 `skill/strategy-falsifier-pro/papertrade-falsifier-pro-v1.0.0.zip` 与 SHA256。 |
| 2 | Toku.agency | 更新现有 Agent；免费入口指向 GitHub 前 3 单申请表；一个付费服务设 Basic $29 / Standard $99 / Premium $299。 |
| 3 | Agensi | 提交完整免费 Skill；不出现 Gumroad 链接、外部付费版本或 checkout 导流。 |
| 4 | Show HN | 选下一个周二或周三美东上午窗口；一次发布，不重复刷帖。 |
| 5 | awesome-quant | 先确认 fork，再按 README 的 `Trading & Backtesting` 规则开一个项目 PR；不直接改上游。 |
| 6 | 闲鱼 / 知乎 / V2EX | 分日发布，按各站规则，不跨平台短时间复制粘贴。 |

## 定价口径（USD）

- 免费：前 3 单完整人工证伪报告，含自动诊断层；须单独取得匿名公开授权。
- **Pro Skill：$29**，自助工作流包。
- **人工审计：$99**，含数据 / 信号保真度核验、诊断复核与录屏解读。
- **深度审计：$299**，$99 全部内容 + ×10 成本压力、数据刷新复跑与 60 分钟解读。

Agensi 是例外渠道：只上架免费 Skill，不把上述付费档位或外部购买入口写入 Agensi listing。

## 账户持有人必须完成的步骤

1. Gumroad：账户登录、商品上传、售价与退款政策确认，以及平台要求的收款资料。
2. Toku：登录现有 profile，编辑而非重复创建服务；Stripe Connect KYC 和银行资料必须由账户持有人按真实身份完成。未启用 payout 前不正式推广付费档。
3. Agensi：登录 Creator Studio，提交免费 listing；免费上架无需先绑定 payout。
4. Show HN / 闲鱼 / 知乎：使用本人账号；公开发布前确认最终文案、链接与账号状态。

## 30 天复盘 Gate

- **≥3 个付费**：继续投入，建设 webhook 自动交付与策略框架适配。
- **1–2 个付费**：保留服务，收窄场景或调整定价。
- **0 付费但 ≥10 个认真询盘**：检查定价与交付摩擦。
- **0 付费且 <5 个认真询盘**：停止商业投入，保留引擎与论文作为作品集。

## 安全与边界

- 不收集客户交易所 API key、账户密码或钱包私钥。
- 不执行真实订单、不代管资金、不提供投资建议、不承诺收益。
- 免费报告只有在用户明确授权后才可匿名公开。
- 不买 star、不互赞、不批量跨社区投放。