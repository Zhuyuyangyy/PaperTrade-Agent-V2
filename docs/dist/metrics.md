# 指标基线与巡检 — 策略证伪引擎 Skill 售卖

> 项目：PaperTrade-Agent-V2-Fast（策略证伪引擎）
> 公开仓库：https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
> 售卖渠道：Toku agent「StrategyFalsifier」+ GitHub 开源引流
> 本文件为唯一指标基线文件，由每周自动化巡检追加数据。

---

## 一、指标体系

### 北极星指标（唯一重要的数字）

**qualified inbound = GitHub issue 询盘数 + Toku 付费 job 询盘数**

定义：
- GitHub issue 询盘：issue 中明确表达购买/试用/定制意愿（区别于 bug 报告和使用提问）。
- Toku 付费 job：Toku 平台 jobs 列表中由**真实买家**发布、且与本项目（StrategyFalsifier / 策略证伪 / falsification）相关的 job。
  - 甄别规则：绝大多数 jobs 是其他 agent 发的 "AVAILABLE:" 广告帖，不算买家询盘；只统计 `buyerName` 非空且非广告帖的帖子。
- **qualified inbound = 0 是当前基线事实，本周期的核心任务就是把它从 0 推到 ≥ 1。**

### 先行指标（leading indicators）

| 指标 | 含义 | 现状 |
|---|---|---|
| GitHub stars | 开源社区关注度，间接决定赞助/转化区间（见下方区间表） | 0（2026-10-30 实测） |
| Landing page 访问 | docs/index.html 落地页流量 | **未 instrumented**（无任何统计代码，此为事实记录；接入统计前该列恒记"未instrumented"） |
| Toku 平台 jobs 总量 | 市场活跃度参照（其中买家真 job 占比极低） | 378 open jobs / 约 5 条买家真 job（2026-10-30 实测） |

### 记录规则

每周一 10:00 由 automation「周指标巡检-证伪引擎」自动执行：
1. 查询上述全部指标（命令见下方"巡检命令"）；
2. 在下方记录表追加一行：`日期 | stars | issues | Toku jobs | qualified inbound | 动作`；
3. 若 qualified inbound > 0，立即在 `win-log.md` 记录详情并**标注需人工跟进**。

---

## 二、真实基线（2026-10-30 查询，gh api + Toku API 实测）

| 指标 | 数值 |
|---|---|
| GitHub stars | 0 |
| GitHub forks | 0 |
| GitHub open issues | 0 |
| GitHub watchers | 0 |
| Toku open jobs（全量） | 378 |
| Toku 买家发布的真 job | 5（Eliot Herbst ×1、topanga ×1、seikixtc ×3；其余 373 条为 agent 广告帖） |
| 提及本项目/证伪的 Toku job | 0 |
| **qualified inbound（北极星）** | **0** |
| Landing page 访问 | 未 instrumented |

结论：项目处于冷启动零点。GitHub 侧零曝光、零询盘；Toku 侧市场活跃但暂无买家发现本项目。一切增长动作（Reddit 帖、X thread、闲鱼上架、文档分发）均以 qualified inbound 从 0 → 1 为验收标准。

---

## 三、Stars 的真实含义与目标区间

市场调研结论（开源项目 star 数与月度赞助/被动收入的常见对应区间）：

| Stars 区间 | 典型月收入（开源赞助/被动转化） |
|---|---|
| 0 – 500 | $0 – 50/月 |
| 500 – 2000 | $100 – 800/月 |
| 2000+ | $800 – 5000/月 |

**本项目目标区间：**
- 短期（1 个月）：stars 0 → 50+，进入"有人看的仓库"状态，qualified inbound ≥ 1。
- 中期（3 个月）：stars 50 → 500，对应 $0–50/月 赞助区间；Toku 付费 job ≥ 3 单。
- 长期（6-12 个月）：stars 500 → 2000，进入 $100–800/月 区间，Skill 售卖形成稳定询盘流。

注意：stars 是先行指标而非收入承诺；本项目主要变现路径是 Toku Skill 售卖，stars 的价值在于给买家"可信度信号"。

---

## 四、巡检命令（automation 每周执行）

```bash
# GitHub 指标
gh api repos/Zhuyuyangyy/PaperTrade-Agent-V2 --jq '{stars: .stargazers_count, forks: .forks_count, issues: .open_issues_count, watchers: .subscribers_count}'

# Toku jobs（翻页拉全量 378 条，统计买家真 job 与证伪相关询盘）
for off in 0 100 200 300; do
  curl -s -H "Authorization: Bearer tok_3486e7306c944cb8a476bf190c6b1bd2" \
    "https://www.toku.agency/api/agents/jobs?limit=100&offset=$off"
done | jq -s '[.[].jobPosts[]] | {
  total: length,
  buyer_real_jobs: [.[] | select(.buyerName != null and (.title | test("^AVAILABLE") | not))] | length,
  falsify_mentions: [.[] | select((.title + .description) | test("falsif|StrategyFalsifier|证伪"; "i"))] | length
}'
```

---

## 五、每周记录表

| 日期 | stars | issues | Toku jobs | qualified inbound | 动作 |
|---|---|---|---|---|---|
| 2026-10-30 | 0 | 0 | 378（买家真 job 5） | 0 | 建立指标基线；创建周巡检 automation |
