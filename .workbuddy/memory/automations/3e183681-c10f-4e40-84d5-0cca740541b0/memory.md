# 自动化记忆：PaperTrade-Agent-V2-Fast 交易循环看护

自动化 ID: 3e183681-c10f-4e40-84d5-0cca740541b0
工作目录: D:/ZYY_Inovation/money/PaperTrade-Agent-V2-Fast
Python: C:/Users/联想/.workbuddy/binaries/python/envs/default/Scripts/python.exe

## 2026-10-27 执行记录

### 19:19 轮（首轮）
- E 存活（116s），D 已死（2598s）→ 仅重启 D。D 存活至 ~19:29 后随回合结束被杀。
- E 于 19:20:26（run 812 后）自行死亡，原因不明（无 traceback）。

### 19:34 轮（续跑轮）
- 复查：E 死（804.9s）、D 死（571.1s）。
- 19:37:09 并行看护会话（另一 codebuddy shell）重启 E；E 跑完 run 813-816 后于 ~19:40 随该会话回合结束被杀。
- 19:39:19 本轮重启 D（PID 44440），心跳恢复；19:47:08 并行会话又启一个 D（PID 46108）→ 双实例，后自行收敛为单实例（一对父子进程退出）。
- ~19:48 本轮重启 E（QlHUp7）：run 817-820 完成，最近轮次 166.7s，equity=945.0281。D 心跳 107.8s。
- 终态：E、D 均单实例存活。

### 20:31 轮
- 复查：E 活（64.6s）、D 活（87.0s），均 < 300s → 不操作。
- 进程核查：E = PID 53440 + 子 55964（19:53:29 起），D = PID 44440 + 子 42180（19:39:20 起），均为父子对单实例；上轮重启的实例跨会话存活。

## 关键经验（跨轮次有效）
1. **循环进程随启动它的 agent 回合结束而死**（壳退现象）：前台启动 + 600000ms timeout 只能保证在看护回合内存活；下次看护轮会再次重启，属预期行为。
2. **并行看护会话会造成双循环**：重启前先用 CIM 查 `python.exe` + `research.*` 进程表；E 的 `rm -f .loop.pid` 会破坏 pid 去重，双 E 循环会双跑 pipeline（双开仓风险），比双 D 更危险。
3. **D 的轮询间隔约 300s**（心跳节拍），健康心跳年龄会在 0-300s 间波动；E 周期约 60-170s。
4. **D 存在代码级错误（超范围，未修）**：`AttributeError: 'int' object has no attribute 'append'`；E 活跃写库时 D 报 `database is locked`。需用户另行修复。
5. ps -W 不显示命令行参数，判断循环进程必须用 PowerShell CIM（注意过滤掉自身 powershell 包装进程，用 `Name='python.exe'` + `CommandLine like '*research.*'`）。
