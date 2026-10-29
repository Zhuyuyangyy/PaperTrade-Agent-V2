# 自动化记忆：PaperTrade-Agent-V2-Fast 副班看门狗（错峰轮，fd0dbcde）

自动化 ID: fd0dbcde-2345-4afe-a4ad-52dade5de2ce（副班，与主看门狗 3e183681 错峰，故障窗口压到 ~30min）
工作目录: D:/ZYY_Inovation/money/PaperTrade-Agent-V2-Fast
Python: C:/Users/联想/.workbuddy/binaries/python/envs/default/Scripts/python.exe
规则与主看门狗一致；判活只用只读 sqlite + 心跳 + CIM 进程核查，不跑 once/loop 试探。

## 2026-10-28 执行记录

### 07:14 轮（副班首轮）
- 首检快照：E 入口活（130.3s）、E 退出 mark 68.0s（假新鲜）、arm_stop22 活（entry 191.0s / mark 83.9s）、arm_tp20 活（28.4s / 59.0s）、arm_nojudge 活（27.3s / 26.4s）、arm_bold entry 310.9s 超阈值、arm_bold exit 57.4s、D 心跳 47.3s（running）、dashboard GET /room 200。
- **CIM 进程核查发现 3 个问题**（数据新鲜度单独不可信，复现主看门狗 05:06 轮教训）：
  1. **E 退出循环进程不在**（主账本 exit-loop 无进程；mark 68s 系入口循环每轮 EXIT 标记步骤维持的假新鲜）；
  2. **arm_stop22 退出循环进程不在**（同理，mark 假新鲜）；
  3. **arm_bold 入口循环进程在但卡死**（PID 54184+58348，01:15 起；run 66 于 07:10 启动后 340s 无任何阶段输出，12s CPU 增量=0，同 04:03 款卡死）。
- 07:1x 杀 arm_bold 卡死对（54184+58348，子随父退）；并行补起三服务：E 退出循环、arm_stop22 退出循环、arm_bold 入口循环（--sleep 240）。
- 80s 复查：E exit mark 8.3s、arm_stop22 mark 24.5s、arm_bold entry 90.1s（新 run 67 已开跑）、E entry 103.4s → 全部新鲜。
- 终态：12 服务全部单实例（父子对=正常单实例模式），五库 journal_mode=wal 未改。
- locked 上报（规则 #8）：E loop.log 累计 79 次（主看门狗 06:22 轮 71，~1h +8），集中在 run 1165-1186 区间，均自愈；嫌疑不变（E 入口/退出双写者 + 并行 once 叠加）。D agent.log 263 次全历史（mtime 10-27 06:29 未变）。四臂日志 0 locked；全局无 429。
- **一小时两次死亡预警（规则 #7）**：E 退出循环（06:13 主看门狗重启 → 07:14 又死）与 arm_stop22 退出循环（06:13 重启 → 07:14 又死）均出现 1 小时内第 2 次死亡，延续主看门狗 06:22 轮已列的"exit-loop 每小时静默死/卡一次，疑代码级缺陷"待查项。
- 臂进度（60 笔目标 / 2026-11-03 截止，剩 6 天）：stop22 closed=11、tp20 closed=13、nojudge closed=12、bold closed=12（open 均 4-5）；E closed=86。按节奏 60 笔难按期，届期按规则停臂。

### 14:41 轮（副班第七轮）—— 全存活零重启
- 首检（14:41:00）：E entry 6.3s；E exit equity 9.6s（0 持仓）；stop22 entry 211.4s/equity 49.7s；tp20 entry 30.6s（mark ~4.97h 假陈旧）；nojudge entry 329.2s、bold entry 328.7s 略超 300s 阈——CIM+日志证实为 --sleep 240 睡眠+数据源拖慢（nojudge run 206 / bold run 119 均完整跑完）；bold mark 218.5s；D 心跳 73.9s running；面板 200/91351B。
- CIM：**12 服务全部单实例父子对存活，零重启**。13:26-13:29 外部会话补起的 5 个退出循环 + dashboard 已连续存活 ~75min；10:25 重启的 bold 入口已活 4h+。
- 外部 once 第 8 次（14:40:53，61904+56824，PPID bash），检查时仍存活（CPU 0.1s），预计 1-2min 自终；E entry 6.3s 新鲜度可能被 once 写 pipeline_runs 污染（教训 #13 延续）。
- locked（规则 #8）：loop.log 98 无新增；四臂 0；D 263 全历史（mtime 10-27 未变）；loop.log 中 "429" 一处为误报（`[run 1429] equity=...` 行号含 429），全局无真实限流。五库 jmode=wal 只读核验未动。
- 规则 #7：13:26 集体死亡补起后稳定 ~75min，无二次死亡，未触发红线。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：E closed=96/open=0/eq 902.67；stop22=21/0/1024.79；tp20=18/5/981.53；nojudge=15/5/1109.46；bold=24/4/939.99。nojudge "连续 52 轮空漏斗 · 8 数据源异常"延续（API 侧不可达），各臂 60 笔难按期，届期按规则停臂。
- 未改任何代码/配置。

### 15:43 轮（副班第八轮）—— 全存活零重启
- 首检（15:43-15:44）：CIM 确认 12 服务全部单实例父子对存活，零重启零死亡。
- 数据：E entry 110.9s（run 1495 完，equity 902.6651）/exit equity 20.7s（0 持仓）；stop22 entry 199.0s/exit equity 37.7s（0 持仓）；tp20 entry 228.6s/exit equity 28.7s（mark 21710.7s 假陈旧，Jupiter 冻结延续，5 仓盲跑）；nojudge entry 195.1s/exit equity 75.0s（mark 21722.3s 假陈旧，5 仓）；bold entry 20.6s（run 130 完，equity 958.6033）/exit equity 15.5s（mark 260.7s）；D 心跳 164.1s running；面板 200/91357B/345ms。
- 进程起始：E 入口 01:13:25、stop22/tp20/nojudge 入口 01:14-01:16、bold 入口 10:25:57（已 5h+）、5 个退出循环 13:26:49-13:28:36（外部会话补起后稳定 2h15m）、D 10-27 19:39、面板 13:29:16。
- locked（规则 #8）：loop.log 98 无新增；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变，无新增）；真实 429（HTTP Error 429/Too Many Requests）全局 0。
- D last_poll 四池全"网络不可达 timed out"（geckoterminal 不可达延续第 4 轮；前轮 baidu 可达性探测已定位=特定 API 主机不可达），心跳新鲜按规则不动作。
- 外部 once 第 8/9 次：15:41:27 PID 13044（已自终）、15:44:38 PID 34856+44424（刚起，PPID bash 48356，非副班发起）；E entry 判据以 CIM+equity cadence 为准，once 污染延续。
- 规则 #7：未触发红线（13:26 集体补起后 2h15m 零二次死亡）。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：E 96/0/eq 902.67；stop22 21/0/1024.79；tp20 18/5/981.53；nojudge 15/5/1109.46；bold 24/5/958.60。难按期，届期按规则停臂。
- 五库 jmode=wal 只读核验未动；未改任何代码/配置。

### 16:47 轮（副班第九轮）—— 全存活零重启
- 首检（16:47）：数据层 E entry 80.5s / equity 32.3s（0 持仓）；stop22 entry 112.0s / equity 12.6s；tp20 entry 20.7s / mark 25466.2s（假陈旧，Jupiter 冻结延续，5 仓）；nojudge entry 317.6s 略超 300s 阈（日志尾部 run 229 完整跑完 equity=1109.4576 在睡，非卡死）/ mark 25477.8s（假陈旧，5 仓）；bold entry 292.0s / mark 197.4s；D 心跳 54.0s running；面板 200/91373B/0.37s。
- CIM：12 服务全部单实例父子对存活，零重启零死亡。进程起始：E 入口 01:13:25、stop22/tp20/nojudge 入口 01:14-01:16、bold 入口 10:25:57（已 6h+）、5 个退出循环 13:26:49-13:28:36（外部会话补起后已稳定 3h20m）、D 10-27 19:39、面板 13:29:16。
- locked（规则 #8）：loop.log 98 无新增；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变）；全局无真实 429。
- 规则 #7：无近期死亡（上次集体死亡 13:26-13:29，已 3h20m），未触发"1h 两次"红线。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：E closed=96/eq 902.67；stop22=21；tp20=18；nojudge=15；bold=24（open 各 4-5）。较上轮（15:43）各臂 closed 无增长，难按期，届期按规则停臂。
- 五库 jmode=wal 只读核验未动；未改任何代码/配置。

### 17:50 轮（副班第十轮）—— 全存活零重启；Jupiter 恢复后臂集中平仓
- 首检（17:49-17:50）：E entry 68.1s / mark 4.9s / equity 4.9s（4 持仓，closed=96，eq 903.08）；stop22 entry 21.9s / mark 47.9s / equity 47.9s（closed=21，open=2，eq 1026.37）；tp20 entry 137.5s / mark 36.8s（closed 18→23，open 5→3，eq 968.10）；nojudge entry 190.5s / mark None（0 持仓）/ equity 47.8s（closed 15→20，eq 1092.45）；bold entry 318.7s 略超阈（CIM+日志证伪：进程 22572+22608 10:25:57 起在表，run 152 完整跑完 equity=958.9272 在睡）/ mark 25.9s（closed 24→29，open 5→3，eq 959.74）；D 心跳 7.7s running；面板 200/95643B/0.47s。
- CIM：12 服务全部单实例父子对存活，零重启零死亡。**dashboard 实例已换代为 3220+16744（17:35:14 起）**——13:29:16 实例（30064，主看门狗 17:11 轮仍在表）在 17:11-17:35 间死亡并被外部会话补起（非副班、非主看门狗记录轮次），存活 ~3h46m 符合 ~3h 系统回收；一小时内仅死 1 次，未触发规则 #7 红线。
- **上游数据恢复兑现**：对比主看门狗 17:11 轮记录，Jupiter/SEARCH 恢复后 1 小时内 tp20/nojudge/bold 各平仓 5 笔（止盈止损不再盲跑）；E 新开 4 仓；D last_poll 恢复 fetched=380、errors=[]（geckoterminal 不可达解除）。
- locked（规则 #8）：loop.log 98→99（+1，line 4834 round failed 自愈）；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变）；"429" 仍为 [run 1429] 误报，真实 429=0。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：stop22=21、tp20=23、nojudge=20、bold=29；E closed=96/eq 903.08。恢复后节奏 ~+5 笔/h/臂，stop22/nojudge 按期仍偏紧，届期按规则停臂。
- 五库 jmode=wal 只读核验未动；无并行 once；未改任何代码/配置。

## 关键经验（副班侧）
1. **mark/心跳新鲜度不能单独证明 exit-loop 存活**——入口循环每轮的 EXIT 标记步骤会写 positions.mark_at_ms 维持假新鲜；必须配 CIM 进程核查（`Name='python.exe'` + CommandLine like '*research.*'）。
2. **--sleep 240 臂入口的 max(started_ms) age 到 ~340s 可能是正常睡眠周期**（主看门狗 06:22 轮实证）：区分方法是看日志——若最后一行是已完成 run 的阶段输出（在睡）则健康；若是新 run 横幅后无线索 + CPU 增量=0 则卡死，需杀后重启。
3. 每服务进程为父子对（PPID 指向另一同 cmdline python.exe），计单实例；重启后按 cmdline 分组计数去重。
4. 副班与主看门狗错峰 ~30min，重启动作前后都要 CIM 去重，防止双循环双开仓。

### 08:45 轮（副班第二轮）
- 首检：12 服务中 9 个进程在跑；**E exit / stop22 exit / bold entry 三服务进程缺失**。数据假新鲜：mark 由入口循环 EXIT 标记步骤 + bold exit 循环维持（bold started_age 760s 但 mark 13.7s 新鲜）。确认已死且无卡死进程需杀，08:45:02 补起三服务（父子对单实例）。
- 80s 复查全部转鲜：E mark 21.8s、stop22 mark 13.7s（run 106 完）、bold 新入口跑完 run 71、D 心跳 81.5s、面板 200；五库 jmode=wal 未动；终态 CIM 复核 12 服务单实例、once 进程已自终。
- **异常：08:44:23 出现来源不明的 `orchestrator once` 进程**（PID 46060+24984，PPID 33156 为 bash，非副班发起；副班从不跑 once），约 1-2 分钟自终；时间与 loop.log 新增 locked 失败重合。once 的 stdout 若重定向进 loop.log 会以 "[loop] round failed" 形态出现（本轮末行即此形态），大概率是该 once 试跑主账本撞入口循环写锁后失败退出。若主看门狗在跑 once 试探，违反双看门狗共同规则，需向用户反馈。
- locked（规则 #8）：loop.log 79 → 92（+13/1.5h），均自愈（下一轮正常跑）；无 429；D agent.log 263 全历史未增长（mtime 10-27 06:29 未变，D 心跳/wal 新鲜不受影响）。
- 规则 #7：未触发"一小时内两次"红线（退出循环死亡间隔 61-91 分钟），但 E/stop22 退出循环静默死今天第 3 次（06:13→07:14→08:45）；bold 入口本次为"完成 run 70 后静默死"（08:32 完成、进程消失，非卡死）——静默死不限于 exit-loop，代码级缺陷待查项延续。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：stop22 closed=12、tp20=14、nojudge=13、bold=13（open 均 5）；E closed=89。按 ~1 笔/1.5h/臂节奏，60 笔预计 10-31~11-01 达成，届期按规则停臂。

### 新增/修正经验（副班侧）
5. **PowerShell CIM 命令里不能出现独立 "Cmd" 字样**——安全策略按 cmd.exe 关键词拦截（报错 "cmd.exe cannot be used from the PowerShell tool"）；直接 `Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | Format-List` 输出，勿自定义含 Cmd 键的哈希表。
6. **修正经验 #2：日志"最后一行是已完成 run"不等于在睡**——bold 本轮即死在此形态（run 70 完成后静默死，日志无任何异常 Output）。唯一可靠判据是 CIM 进程核查；日志仅用于区分卡死（新 run 横幅后无阶段输出）与正常睡眠。

### 10:25 轮（副班第三轮）
- 首检（10:05:22）：E 入口活；**E 退出 / stop22 退出 / bold 入口三服务进程缺失**（CIM 确认；E/stop22 的 mark 1286.9s/1427.5s 已超 180s 阈，bold started_age 4819.5s 超 300s 阈）。外部 once ×2（10:03:56、10:11:39，PPID 均为 bash，非副班发起）。
- 10:14 首轮重启：**E 退出与 stop22 退出的 python 死在启动阶段**（grep 横幅数未+1、CIM 无进程、日志无 traceback——疑壳退遇上大库 Pipeline 构造慢：memehunter.sqlite 59MB/WAL 5.5MB、stop22 7.6MB）；bold 入口存活并跑完 run 72（equity 973.7345），~10:21:30 完成轮次后静默死（"重启后只活一轮"第 4 次）。
- 10:25:57 重试三项全部成功（单实例父子对）：E 退出=12524+48400、stop22 退出=32704+39220、bold 入口=22572+22608。
- 终态（10:29:26）：12 服务全部单实例存活。E mark 13.6s、stop22 mark 7.9s（新退出循环完整 pass 已打标）、bold run 73 完成、D 心跳 64.2s running、面板 200；五库 jmode=wal 未动。
- locked（规则 #8）：loop.log 累计 98（较 10:05 无新增）；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变）；全局无 429。
- 规则 #7：主看门狗 08:32 后未再运行（其 memory mtime 08:33:54），过去 1 小时每服务仅死 1 次，未触发"1 小时 2 次"红线；但 E 退出今日第 7 次死、stop22 退出第 5 次、bold 入口"只活一轮"第 4 次——代码级缺陷待查项延续。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：stop22 closed=16、tp20=18、nojudge=15、bold=13（各 5 持仓）；E closed=91。按 ~2-4 笔/天/臂节奏难按期，届期按规则停臂。
- 无报价持仓（新观察）：E 退出循环报 SI/STONK/JEANPH 无报价、stop22 报 SI/JUP/███ 无报价——这些持仓无法打标/平仓，看门狗不可动作，仅记录。

### 新增经验（副班侧，10:25 轮）
7. **大库上重启 exit-loop 可能死在启动阶段**：壳退若落在 python 启动窗口（大库 Pipeline 构造数秒），进程未打印横幅即死、日志无任何痕迹。判据：重启后 grep 横幅数未 +1 且 CIM 无进程 → 直接重试，无需其他证据。
8. **臂退出循环的 mark 会长期假死（数据判活失效）**：tp20/nojudge/bold 退出循环进程活着，但 mark_age 已 ~46min 且持续增长（入口循环 EXIT 步骤"标记 0 仓"不再维持假新鲜；mark 只在完整 pass/once 轮时写）。对臂退出循环，>180s 的 mark 阈值已不可信，必须以 CIM 为准；切勿仅凭 mark 超阈重启活着的进程（会双开）。
9. **外部脚本会重写臂日志**：loop.arm_*.log 的 mtime 持续前进但内容不变，并出现内容行号漂移（10:23→10:27 间 loop.arm_bold.log 的 run 72 从 548 行挪到 551 行）。日志仍可用，但取证以内容为准、勿依赖行号。
10. **bold 入口循环重启后稳定"只活一轮"**（07:26/08:30/08:45/10:14 四次均跑完一个 run 即静默死），与 01:1x 外部会话启动、已活 9h+ 的其他臂入口不同——疑似与看门狗重启方式相关，待查。

### 13:26 补充（exit 127 壳退通知跟进轮）
- 10:25:57 三项重启的壳退通知（exit 127）延迟至 13:26 才送达。复查发现 10:29–13:26 间共 **6 个服务死亡**：E 退出、stop22 退出、tp20 退出、nojudge 退出、bold 退出（均静默死）+ dashboard（00:49:37 起实例死，首次观察到面板死亡）。
- **主看门狗 13:26–13:29 轮已将 6 个服务全部补起**（均单实例父子对）：E 退出 28096（13:26:49）、stop22 退出 7428（13:27:09）、tp20 退出 23704（13:27:40）、nojudge 退出 44180（13:28:00）、bold 退出 40572（13:28:36）、dashboard 30064（13:29:16），补起间隔 20–50s/个。副班本轮未动手（并行重启会双开），仅 CIM 复核确认。
- 副班 10:25:57 重启的 E 退出/stop22 退出死于 ~13:26（整 3 小时，符合规则 #7 的 ~3h 系统回收，属正常设计）；**bold 入口（22572）突破"只活一轮"模式，存活 3h+ 持续在跑**（closed 13→19）。
- 终态（13:32:25）：12 服务全部单实例存活。E closed=96（open=0，此前的无报价持仓已全部平掉）、stop22 closed=21（open=0）、bold closed=19；nojudge/tp20 mark 仍假死（进程活、mark ~3.8h 旧，教训 #8 再现）；locked 仍累计 98 无新增、无 429；D 心跳 112.3s running；面板 200。
- 经验 #11：**主看门狗一轮会连续补起多个死亡服务**（本轮 6 个、间隔 20–50s）——副班撞上死亡潮时先等 3–5 分钟让其本轮做完再 CIM 复核，切勿并行重启（双开风险，重演 06:22 事故形态）；壳退通知可能延迟 ~3h 送达，一切以 CIM 实测为准。

### 18:06 补充（全员死亡潮 + 外部竞争重启）
- 18:04 CIM 发现 **17:12–18:04 间发生全员死亡潮**：12 循环服务 + D 全部静默死亡（主看门狗 17:11 轮还全部存活；无 STOP 文件、系统未重启——uptime 1天10h 连续）。仅 dashboard 被外部力量于 17:35:14 重启（3220+16744）。
- 18:06:03 外部力量又重启一个 D（24724+47064，PPID 54780，非副班发起）；副班 18:06:13–14 批量重启 10 个 pipeline 服务（E 入口/退出 + 四臂入口/退出，全部单实例）。**D 双实例**：杀较晚的 35736+52328（副班自己的 jp4dW8，其 exit 127 通知证实子进程被本kill掉），保留外部 24724。
- 终态（18:10:40）：12 服务全部单实例 = 副班 10 个（18:06:13–14）+ 外部 D（18:06:03）+ 外部 dashboard（17:35:14）。数据全鲜：E mark 68.5s、open=4（上游 SEARCH 恢复后重新开仓）、bold mark 9.5s、closed 24→29、nojudge closed 15→20、tp20 closed 18→23、stop22 closed=21；D 心跳 73s running；面板 200。外部 once（18:09:15）随跑随退。
- locked：loop.log 98→102（+4，重启风暴期多进程齐启 + once 重叠的瞬时失败，自愈）；**loop.log 首次出现 429×1**（Jupiter 限流，客户端带重试，规则 #8 仅记录待下一轮）；四臂 0 locked / 0 429。
- 经验 #12：**外部力量在独立重启服务**（今日已见：dashboard 17:35、D 18:06:03，外加全天 once 轮次）——副班批量重启后必须立即 CIM 查重，双实例杀较晚者；撞车两次（13:26 主看门狗连补 6 个、18:06 外部补 D）。
- 经验 #13：**全员死亡潮新形态**（17:12–18:04，12 服务同时静默死；无重启、无 STOP 文件、机器未重启）——与 ~3h 回收（规则 #7）和单服务静默死形态均不同，疑外部 taskkill 或系统级事件，待查。

### 19:52 补充（死亡潮 #2/#3 三连 + loop.sh 真相 + 第三轮全员重启）
- **死亡潮 #2**（18:10 全活 → 19:44:54 仅剩 dashboard 3220）：副班 18:06 重启的 10 实例 + D（18:06:03 起）全灭。18:06:03 的 D 经主看门狗 18:18 轮记忆确认是其本轮所为（任务 o6o6zV）——**修正经验 #12 的"外部力量"判断：那个 D 是主看门狗，不是外部脚本**；外部力量只 confirmed 了两件事：start-copywatch.bat（17:35 起 dashboard）和 loop.sh。
- **loop.sh 真相（主看门狗 18:18 轮揭示）**：10-27 21:09:54 起 E 入口实际由 `runtime/memehunter/loop.sh`（bash 32616）驱动——`while true → orchestrator once + sleep 100`，python 子进程每轮才短生；**CIM 只扫 python.exe 会漏判 E 入口**，必须同时扫 `bash.exe ... loop.sh`；E 的 started_ms 新鲜度由 loop.sh 维持。今天所有"神秘 once 进程"都是 loop.sh 的轮次。
- 19:45:34 外部又起一个 D（62088）与副班 19:45:54 的 D（17008）重复 → 杀较晚者（17008+32780）；副班 19:45:54 批次的 E 入口实例（3452）在 loop.sh 存活时属多余双写者 → 杀（3452+55708）；19:49:03 出现第二个 dashboard（48616+46508）→ 杀较晚者。
- **死亡潮 #3**（19:51:30–19:52:00）：副班 19:45:54 的 9 实例、外部 D 62088、**loop.sh 32616（熬过 22.6h 和前两潮，本次阵亡）**、dashboard 3220 全部死亡；外部监管力量 19:52:04 再起 dashboard（3156，此后每几分钟被重起一次，Keep-alive 只保面板）。
- 19:52:54 副班**第三轮全员重启 11 项**（loop.sh 已死，E 入口这次是真需要；四臂×2 + E 退出 + D；dashboard 交给外部 Keep-alive 未动）。19:54:47 CIM：11 服务全部单实例；数据全鲜（E started 111.4s / mark 51.0s、D 心跳 11.2s、面板 200）；locked 仍 108 无新增、429 仍 1、四臂 0/0。
- 经验 #14：**死亡潮今日 3 波**（~17:12–18:04、~19:3x–19:44、19:51:30–19:52），间隔越来越短（1.5h → 1h → 6min），无差别杀光所有 python（含 22.5h 老进程），仅外部 Keep-alive 的 dashboard 能瞬间恢复——**必须人工介入查杀手**（本机 schtasks 被安全策略拦截，需用户手动查计划任务/监控脚本）。
- 经验 #15：**loop.sh 活着时不要重启 E 入口 loop 实例**（started_ms 被 once 轮维持永远新鲜，数据规则判"活"；loop 实例+loop.sh 双写者是 run 号 +2 跳和 locked 增长的来源之一）；loop.sh 死后 E 入口才真需要 loop --sleep 100。
- 经验 #16：外部 Keep-alive 只保 dashboard（每几分钟重起）；D 死了只有看门狗补——D 判死仍以 CIM + 心跳（阈值取 heartbeat.json 的 poll_seconds=90，主看门狗教训 #6）为准。

### 19:56 补充（STOP 文件出现：运营者急停，全循环干净退出）
- 19:56:02 发现项目根目录 **STOP 文件存在**（0 字节，mtime 19:54）——guards.py 的运营者紧急停止开关。随后 5 个退出循环任务以 exit 0 结束（guard_round 检出 STOP → `[exit-loop] halted` → return 0，干净退出非被杀），4 个臂入口任务 exit 1 结束；副班 19:52:54 重启的 11 项全部停摆。
- 终态（19:56:03）：存活仅 dashboard（3156，外部 Keep-alive）+ D（15060+19188，copywatch 不查 STOP，只读轮询无害）；E 入口（60644）在 100s 睡眠中，下一轮 guard_round 也会 halt。
- **副班动作：不重启、不删除 STOP**——重启会被 STOP 立即 halt（无意义），删 STOP 是运营者专属动作（看门狗越权）。已向用户报告，待确认 STOP 来源（今日 3 波死亡潮后出现，需确认是人工急停还是同一股力量的手笔）。
- 经验 #17：**见到 STOP 文件 = 运营者急停，看门狗进入"只报告不重启"模式**；恢复流程：用户删除 STOP → 下轮看门狗按规程重启全部循环（exit-loop/loop 的 guard_round 在 STOP 删除后自动放行）。
- 臂进度定格（60 笔目标 / 2026-11-03 截止）：stop22 closed=21、tp20=23、nojudge=22、bold=29；E closed=96、open=5。急停前最后一轮全部正常完成（E run 1656 eq 905.32、stop22 run 229 eq 1028.71）。

### 11:19 轮（副班第四轮）—— 全存活零重启
- 首检（11:19:39）：**12 服务全部单实例存活，无需重启**。数据：E entry 21.6s/mark 43.9s；stop22 entry 177.1s/mark 55.4s；tp20 entry 217.4s/mark 5849.1s（假陈旧）；nojudge entry 185.6s/mark 5860.7s（假陈旧）；bold entry 147.6s/mark 38.1s；D 心跳 118.7s running；面板 /room 200。
- CIM：11 组 research 进程 + 面板进程均为父子对单实例；10:25:57 重启的 E 退出/stop22 退出/bold 入口已连续存活 54min。**bold 入口已摆脱"只活一轮"**（10:25 重启后连续跑完 run 74→84 共 11 轮，日志尾部 run 84 equity=970.6468），修正经验 #10 的疑虑——10:14 那次"只活一轮"或与启动阶段壳退/大库构造有关，非必然复发。
- 日志尾部全部健康：E run 1329 完、stop22 run 133、tp20 run 131、nojudge run 170、bold run 84（开 2 笔试探仓），无卡死形态。
- locked（规则 #8）：loop.log 98（无新增）；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变）；全局无 429。
- 规则 #7：本轮零重启，一小时内无二次死亡；E 退出/stop22 退出今日累计静默死次数延续代码级待查项（上次死亡 10:05，已 74min 未复发）。
- **新观察：D last_poll 四池全部"网络不可达 timed out"**（geckoterminal 不可达，base/eth/robinhood×2）——非 Jupiter 429，心跳仍新鲜（118.7s<300s），按规则不动作，记录待观察；若持续多轮需查本机出网。
- **外部 once 第 4 次**：11:20:44 PID 61460+21960（PPID bash，非副班发起），本次未造成新增 locked。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：E closed=93、stop22=17、tp20=18、nojudge=15、bold=17（各 4-5 持仓）。
- 五库 jmode=wal 只读核验未动；未改任何代码/配置。

### 12:23 轮（副班第五轮）—— 全存活零重启
- 首检（12:23:30）：12 服务进程全在（CIM 分组计数全部=2 父子对单实例）。数据：E entry 1.8s/equity 1.1s（0 持仓）；stop22 entry 18.4s/mark 52.8s；tp20 entry 58.6s/mark 9653.6s（假陈旧）；nojudge entry 27.0s/mark 9665.2s（假陈旧）；bold entry 326.6s/mark 230.9s；D 心跳 153.2s running；面板 200。
- arm_bold entry age 326.6s 曾疑卡死，双采样排除：日志 mtime 12:24:43 新鲜、22608 CPU 15s 增量 +0.016s、run 95 已完整跑完（SEARCH 160→EXIT 标记 5 仓）并开出 run 96 —— 正常睡眠/慢网络，非卡死。
- **外部 once 第 5 次**：12:23:49 PID 29228+28992（PPID bash 61700，非副班发起），~12:25 自终（1.5 分钟），未造成新增 locked（loop.log 仍 98）。
- locked（规则 #8）：loop.log 98（无新增）；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变）；全局无 429。
- 规则 #7：零重启，无二次死亡；E 退出/stop22 退出/bold 入口自 10:25:57 重启后已连续存活 ~2h，静默死暂未复发。
- D last_poll 连续第 2 轮四池全"网络不可达 timed out"（robinhood×2/ton/solana）——心跳新鲜不动作，若下轮仍持续需查本机出网。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：E closed=96、stop22=20、tp20=18、nojudge=15、bold=19；四臂仍难按期，届期按规则停臂。

### 13:27 轮（副班第六轮）—— 零重启：外部并行会话补起全部 5 个退出循环
- 首检（13:27:59）：数据层多个假 DEAD：stop22 entry 313.2s（进程在、--sleep 240 正常睡眠）、tp20/nojudge mark ~13500s（Jupiter 冻结假陈旧）、bold mark 303.5s。CIM 发现**外部并行会话正在依次重启退出循环**：E(13:26:49)→stop22(13:27:09)→tp20(13:27:40)→nojudge(13:28:00)，bold 退出进程缺失；外部 once ×2（13:27:34、13:30:45，今日第 6/7 次，频率上升）。
- 判定不重复启动（避免双开），等 90s 复查：13:28:36 bold 退出已被外部会话补起（40572+9200）——序列走完，**12 服务全部单实例，副班零重启**。
- 终态（13:32:00）：E entry 73.8s/equity 10.3s；stop22 234.2s/50.2s；tp20 184.1s/mark 13741.7s（假）；nojudge 152.3s/mark 13753.4s（假）；bold 304.2s/mark 201.2s（假，重启后 Jupiter 无报价）；D 87.5s running；面板 200/91349B。equity gaps 7-70s 证明 5 个退出循环均在 cycling；五库 jmode=wal 未动；未改任何代码/配置。
- **重大异常：5 个退出循环在 12:43-13:26 之间集体静默死亡**（01:13-01:14 起活 12h+ 的 tp20/nojudge/bold 退出首次死亡；E/stop22 退出慢性死延续）。同期 5 个入口循环（同时代启动）全部存活——死亡特异性指向 exit-loop 代码路径，"代码级缺陷"待查项升级为"集体静默死"，需人工查 exit-loop 主循环异常退出路径。
- 规则 #7：每服务过去 1 小时仅死 1 次，未触发"1h 两次"红线；但集体死亡事件已在本轮报告指出。
- 出网探测（read-only）：baidu 200/0.1s → 本机网络正常；api.geckoterminal.com、lite-api/quote-api.jup.ag 均 8-10s 超时 → **特定 API 主机不可达，非本机断网**。Jupiter 宕机自 ~09:42 持续 ~4h，15 个持仓（tp20/nojudge/bold 各 5）止盈止损盲跑；D 四池轮询全超时第 3 轮（心跳新鲜，按规则不动作）。
- locked（规则 #8）：loop.log 98（无新增）；四臂 0；D agent.log 263 全历史（mtime 10-27 06:29 未变）；无 429。
- 臂进度（60 笔 / 2026-11-03 截止，剩 6 天）：E closed=96、stop22=21、tp20=18、nojudge=15、bold=19（bold eq 937.19 续跌）；60 笔难按期，届期按规则停臂。

### 新增经验（副班侧，13:27 轮）
11. **外部并行会话会整段补起退出循环**：13:26-13:28 见到 E/stop22/tp20/nojudge/bold 退出被依次重启（bash payload 型进程，非主看门狗——其 memory mtime 12:43 未动）。副班正确动作：CIM 确认无双实例后不重复启动，等 60-90s 复查其是否补起最后一个（bold 13:28:36 被补），避免双开仓。
12. **出网故障定位法**：baidu 200 + 目标 API 超时 = 特定主机不可达（非本机断网）；用于区分 D 轮询全超时的根因（API 侧 vs 本机网络）。
13. once 污染仍在：外部 once 今日第 7 次（08:44/10:03/10:11/11:20/12:23/13:27/13:30），3 分钟内 2 次、频率上升，会往 pipeline_runs 插孤儿行污染 max(started_ms) 新鲜度（E entry 73.8s 即疑 once 写入）；判 E 入口活以 CIM+日志+equity cadence 为准。
