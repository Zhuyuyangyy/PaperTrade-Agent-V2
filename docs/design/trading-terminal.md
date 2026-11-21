# OpenAlpha Trading Terminal V1

只读可视化终端：真实 Binance K 线 + 三本真实模拟账本 + DeepSeek 判断面板。
**终端不产生交易，不伪造成交，不连接真钱。**

## 运行

```bash
python -m research.terminal --root data/openalpha_forward_v1 --port 8787
# 浏览器打开 http://127.0.0.1:8787
```

- 默认只监听 `127.0.0.1`；
- 需要远程访问时：`--host 0.0.0.0 --token <随机串>`，浏览器用 `?token=` 或 `Authorization: Bearer`；
- `--root` 指向包含 `shadow.sqlite` / `deepseek_shadow.sqlite` / `verified.sqlite`、
  `data/*.jsonl`、`state/heartbeat.json` 的运行目录。

## 数据来源（全部只读）

| 内容 | 来源 |
|---|---|
| K 线（1m/5m/15m/1h/4h） | Binance `fapi/v1/klines` |
| 三本账余额/权益/胜率/回撤 | `summarize_books()` 读 SQLite |
| 开平仓标记与理由 | `orders` join `positions` + `decisions.jsonl` / `deepseek_decisions.jsonl` |
| DeepSeek 判断/版本/延迟 | `deepseek_decisions.jsonl` + `providers.jsonl` |
| 进程/心跳/熔断 | `state/heartbeat.json` |

## 功能

- 周期切换；K 线每 10 秒经 WebSocket 推送更新；
- 开仓（▲/▼）、平仓（TP/SL/EX）标记；Rules 绿/红、DeepSeek 青/紫；
- 点击标记显示：来源账本、方向、策略、入场价、数量、决策理由、平仓原因与盈亏；
- 三本账同屏：余额、权益、已实现、胜率、最大回撤；
- 状态条：进程 PID、心跳延迟、WebSocket 状态、熔断标记；
- 刷新页面从 `/api/state` 与 `/api/marks` 恢复历史标记。

## 边界

1. 终端只读：没有任何下单接口；交易仍由 `RunLoop` + `PaperBroker` 决定；
2. 标记全部来自真实账本订单，没有 demo 数据；
3. 图表为 TradingView Lightweight Charts（保留 `TradingView Lightweight Charts™ © TradingView` 标识）；
4. TradingView MCP 属于独立研究通道（P2），不接入本终端、不驱动交易、不训练模型；
   需要用户自行配置 MCP 凭据，本仓库不保存。

## 截图

`docs/terminal/terminal.png`（1h 周期，规则与 DeepSeek 盘的模拟开平仓标记、三本账、最近决策）。