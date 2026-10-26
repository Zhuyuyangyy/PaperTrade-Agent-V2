# PaperTrade-Agent V1 — 零实名虚拟盘实验

这是一个可以直接运行的**本地模拟交易 Agent 骨架**，用于观察真实市场行情并在本地模拟交易。**不含真实交易逻辑、私有账户接口或交易所 API Key；绝不发送真实订单。**

- Python 3.10+；仅标准库，无需 pip install。
- 行情：币安专用公开市场数据主机 `data-api.binance.vision`，只使用 `GET /api/v3/klines` 和 `GET /api/v3/ticker/price`。公开行情无需交易所账户，但受网络连接、所在地适用规则和服务可用性限制。
- 初始模拟资金 1,000 USDT，BTC/ETH 现货多头（不做空、不使用杠杆）。
- 观察完成的 1 小时 K 线：SMA20/SMA50 趋势基线，价格触发固定止损。所有决策日志、模拟交易、账户快照保存在 SQLite。重复运行不会在同一 K 线上重复下策略订单。
- 费用假设：交易手续费 0.1%、滑点 0.05%；不是实际报价或成交保证。
- 单币最大仓位 10%，组合总持仓上限 20%；每日净值下跌达到 2% 后**停止开新仓**，仍然允许风控卖出。无借贷或自动加杠杆。

## 快速开始

在解压后的文件夹中：

```bash
# 1. 不联网即可运行：**合成行情演示**（仅用于检查程序是否工作）
python paper_agent.py demo
# 打开 data/demo_dashboard.html 可看合成行情的模拟资金曲线

# 2. 用真实的公开行情执行一次虚拟决策（不是历史回测）
python paper_agent.py once

# 3. 持续运行，每 60 秒查询一次最新价格，每根已收盘的 1h K 线最多做一次趋势决策
# 请在持续开机的电脑/服务器上运行；停止程序即停止监测。
python paper_agent.py loop

# 4. 查看报告或生成本地仪表盘（始终只有虚拟数据）
python paper_agent.py report
python paper_agent.py dashboard
# 浏览器打开 data/live_dashboard.html

# 5. 导出虚拟成交记录为 CSV
python paper_agent.py export
```

Windows 使用 `py paper_agent.py ...` 也可以。命令都在本地终端执行。开启 `loop` 前建议先用 `once` 检查网络是否允许获取公开数据。

## 代码工作流程

```text
public OHLCV/ticker (GET only)
       ↓
closed-candle filter + trend rule (BUY/SELL/HOLD proposal)
       ↓
position exposure / daily loss cutoff / stop loss
       ↓
LOCAL simulated fills (fees + slippage)
       ↓
SQLite journal + static HTML dashboard + export
```

**这里的“Agent V1”是可审计的规则决策代理，不是 LLM 自由买卖。** 后续可加一个只读的 LLM 研究员，输出固定 JSON 提案/风险解释，但不能绕开风控，更不能自己接交易所的真实订单接口。

## 重要局限

1. 本软件是实验原型，并非生产级交易系统；不保证盈利，模拟成绩不代表实盘成绩。
2. `demo` 使用**人为合成价格**，只能用于程序验收，**不可当作历史行情回测的盈利证据**。真实行情模式 `once/loop` 的模拟成交才算前向纸面测试。
3. 真实交易可能存在断网、行情延迟、价格跳空、滑点和手续费差异；虚拟盘无法完整模拟交易深度、限价规则与订单排队。
4. `loop` 里的止损在轮询时才检查，并不意味着自动止损一定成交。程序停机时不会自动检查风控。
5. 实际的公开数据服务可能有地域、速率、网络限制；不要绕过平台地区限制或使用别人的 KYC 账户。
6. 前向测试建议 30 天以上，记录总收益、最大回撤、胜率、交易次数、手续费、与 BTC 买入持有的对照；低样本量不能说明策略有效。

## 安全原则

- 本仓库**没有真实交易执行路径**，没有任何连接私有 API 的代码。
- 无需身份证、交易所实名、助记词、私钥、API Key、充值。
- 不要把任何真实 API Key 或助记词发给 AI、加入配置或上传 GitHub。
- 若以后考虑实盘，先进行独立安全审查和合法性评估；单独开发与模拟环境隔离的执行器，严格限制权限、额度和停机开关。绝不把 `paper` 默默切成 `live`。

## 下一阶段（不在当前版本中）

- 实际历史 OHLCV 数据回测、基准比较和样本外检验，检查未来函数。
- 接入 Freqtrade 的 `dry_run` 引擎和风控功能，长期跑纸面测试。
- 加入只读 LLM 分析 Agent、信息面分析、每日解释性简报。
- 加入告警、Docker 和只读 Web 控制台；监控 30 天后再讨论是否具备进一步测试条件。

## 官方参考

- Binance 免认证行情接口：https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only_CN.md
- Freqtrade Dry-run：https://www.freqtrade.io/en/stable/strategy-101/
- CCXT 公共行情：https://github.com/ccxt/ccxt/wiki/manual
