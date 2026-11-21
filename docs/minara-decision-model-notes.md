# Minara AI 决策模型研究笔记（2026-11-19）

> 用户推荐："minara ai 那个决策模型还是挺不错的"。
> 核验来源：minara.ai/llms.txt + product/agent 页（只读，未注册未使用）。

## 它的模型：四步生命周期 + 三态 Agent

**Agent Factory——每个 agent 走四步**：
```
01 Author   → 自然语言 → 编译成结构化 spec（"一句话就是源码"）
02 Simulate → 对历史数据 + 合成场景回放；先看触发率/PnL/风险，
             "before anyone's capital moves"
03 Deploy   → 一键 promote，继承 guardrails 行动
04 Observe  → 每个决策可追溯到输入；可在同一条时间线上暂停/调整/退役
```

**三种 canonical 形态**：
- **Monitor**：盯流/钱包/新闻/链上事件，条件触发才 ping
- **Trade**：把 trigger 变成交易，自带 sizing/slippage/retry
- **Yield**：按实时 APY/风险/流动性再平衡

**组合是一等公民**：agent 之间发事件/消费输出（monitor 唤醒 trader，
trader 再平衡 yield agent）。

## 与我们的对照（PaperTrade-Agent-V2 / Cross-Market v2.2）

| Minara | 我们的对应 | 关系 |
|---|---|---|
| Author（NL→spec） | market_mapping.yaml + 预注册假设 | **我们更严**：预注册+多重检验校正是 Minara 未广告的 |
| Simulate（历史+合成） | PaperTrade 引擎 + Replay + 合成价格 | 同理念；我们的可用性门禁更细 |
| Deploy（guardrails） | auto-execute 白名单（8 规则） | 同构 |
| Observe（决策溯源） | audit log + 三层证据链 | 同构 |
| Monitor/Trade/Yield 三态 | radar / miner / paper 三线 | 同构 |
| 组合（agent 链） | pipeline（发现→审查→执行→复盘） | 同构 |

## 值得直接借鉴的三件事

1. **四步生命周期作为显式状态机**：我们的部件都有，但没有把
   "authored → simulated → deployed → observed" 作为 agent 的显式生命周期。
   建议 Cross-Market 的 A/B/C 实验与 PaperTrade 策略都挂上这个状态机，
   每个策略实例都能回答"我现在在第几步"。
2. **"每个决策可追溯到输入"作为产品级要求**：我们的证据链要做成
   决策对象的默认字段（不是可选项），Minara 把这点做成了产品语言。
3. **Monitor/Trade/Yield 作为命名规范**：我们的 collector（Monitor）、
   采集→EventStore（基础设施）、auto-execute（Trade）可以用这套词汇统一，
   减少"探测器/矿工/执行器"各叫各的。

## 它的边界（我们的差异化）

- 托管钱包 + 自动交易是它的产品终点；**我们的终点是证伪**
  （"没有增量 Alpha 也是研究结果"——Minara 的商业模型决定了它
  必须让用户"一直交易"，我们的模型允许"一直不交易"）。
- 未见到它对多重检验/前视偏差的制度化处理（可能未在营销页展示），
  这正是 Cross-Market v2.2 的存在理由。

## 结论

**决策模型：A-（四步生命周期+三态+组合，清晰且诚实）。**
值得借鉴其生命周期命名与溯源语言；不必迁移其执行框架（我们的证伪框架更严）。
一句话：**它把"AI 帮你交易"做成了产品；我们在把"AI 帮你验证该不该交易"做成学科。**
