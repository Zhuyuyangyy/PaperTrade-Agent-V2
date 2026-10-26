# 今晚启动手册（纯虚拟盘）

> 目标：验证公开行情、两套主实验 Freqtrade Dry-run 账户、独立快速实验 C 和只读仪表盘能否联动。不是盈利挑战。没有交易也不等于启动失败。

## 启动前

- 安装并启动 Docker Desktop；确认 `docker compose version` 能返回版本号。
- 解压项目 ZIP，打开 PowerShell，并进入包含 `docker-compose.yml` 的 `PaperTrade-Agent-V2` 目录。
- 不需要交易所账户、API Key、身份证信息或钱包私钥。

## 1. 安全检查并启动三套虚拟账户

```powershell
python scripts/safety_check.py
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=80 research baseline hybrid fast equity-sampler
```

启动日志中应能看到三个独立 Freqtrade Dry-run 账户、researcher 从 Kraken 公共行情读取已收盘的 1 小时 K 线，以及 equity-sampler 每分钟记录账户权益。Researcher 每 120 秒检查一次，同一根 K 线不会重复写入信号；公开 ticker marks 单独更新。首次下载镜像和行情数据可能需要一些时间。

## 2. 打开自动刷新的仪表盘

保留上面的交易服务继续运行。再开一个 PowerShell 窗口，进入同一个项目目录：

```powershell
python scripts/dashboard_server.py
```

在浏览器打开 `http://127.0.0.1:8765`。页面每 15 秒重新读取本地账本、权益快照和经过校验的信号。持仓估值要求公开 ticker mark 不超过 5 分钟；如果任何持仓无法估值，总权益会显示不可用而不是用已实现盈亏冒充。页面只绑定本机，不开放到局域网或公网。

## 3. 检查数据与报告

需要查看命令行汇总时，另开终端执行：

```powershell
python scripts/report.py
python scripts/dashboard.py
```

`report.py` 输出已实现盈亏、持仓浮动盈亏和完整估值状态；`dashboard.py` 生成一个可单独打开的静态 HTML 快照。实时页面使用 `dashboard_server.py`。运行结果要留档时，执行 `python scripts/export_experiment.py` 导出带 SHA-256 manifest 的实验快照。

## Upgrading an already-running earlier V2

1. In the old project directory, stop the old stack with `docker compose down`.
2. Keep the old project folder intact. The migration prefers the current isolated source ledgers at
   `runtime/baseline/baseline.dryrun.sqlite` and `runtime/hybrid/hybrid.dryrun.sqlite`, with a fallback
   to legacy `user_data/*.dryrun.sqlite` paths. From the NEW project directory, run
   `python scripts/migrate_ledgers.py --legacy-root <old-project-path> --target-root .` before starting the new stack.
3. The script uses SQLite's backup API, checks open/closed trade counts, and never overwrites
   an existing target. Confirm the printed counts. If both old ledgers have no fills, the
   migration is still useful to preserve schema/state; if files are missing, it reports that
   explicitly and the new account will start fresh.
4. Run the safety check and `docker compose config`, then start the new services.

## 4. 正常现象与停止方式

- 刚启动时 KPI 显示 `--`、曲线为空：正常，采样器尚未写入足够的完整权益点。
- A/B 几小时没有交易：可能正常，它们只使用 1 小时信号。快速实验 C 使用 15 分钟 EMA/RSI 规则，反馈更快但也可能空仓；看板会显示真实 Freqtrade 心跳和最新公开行情标记，即使没有成交也能确认进程是否运行。
- `research failed closed`：表示行情读取或校验失败，Agent 账户不会因缺失/过期信号获准入场。检查网络和日志，不要通过关闭风控来绕过。
- `docker compose down`：停止服务并保留 SQLite 账本。不要删除 `runtime/baseline`、`runtime/hybrid` 或 `runtime/fast`，否则会丢失实验记录。
- Dashboard 窗口按 `Ctrl+C` 停止。

## 安全边界

三套账户均应保持 `dry_run: true`、`trading_mode: spot`、空的交易所凭证、关闭远程 API。不要添加交易密钥或把服务端口暴露到公网。当前 Agent 投票是确定性规则，不是已启用的 LLM 交易员。首晚只验证系统链路，不评价策略是否盈利。


## Fast feedback account C

The stack now includes an isolated `fast` service using 15m EMA/RSI rules. It is for quicker pipeline feedback only and is excluded from the primary A/B comparison. It can still stay flat when the market does not meet its entry rules. The account has its own `runtime/fast/` directory and SQLite ledger.
