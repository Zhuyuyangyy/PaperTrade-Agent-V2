# DecisionProvider：多模型对照（Alpha Discovery V1 延伸）

> 状态：**RESEARCH ONLY**。规则 / DeepSeek / Jev / 线性模型对同一份快照各投一票，
> 结果只写日志，**不进入** `Selection`、`RiskDecision`、`VERIFIED`。
> 谁更有效，由未见样本的净收益裁决。

---

## 1. 接口

```python
from research.decision import MarketSnapshot, run, RulesProvider, DeepSeekProvider

snapshot = MarketSnapshot(as_of_ms, "BTC/USDT", state, intel_features)
decisions = run(snapshot, [RulesProvider(), DeepSeekProvider(cfg)], log_path="data/providers.jsonl")
```

每个 `Decision`：`provider / version / status / choice / score / confidence / rationale / evidence / latency_ms`。

`status`：`OK` / `UNAVAILABLE` / `NO_DATA` / `ERROR`。`ERROR` 与 `UNAVAILABLE` 都退化为 HOLD，不猜。

---

## 2. 四个 provider 的现状

| Provider | 状态 | 配置 | 说明 |
|---|---|---|---|
| `rules` | 可用 | 无 | 现有三策略 + 注册表，确定性基线 |
| `deepseek` | 可用 | `DEEPSEEK_API_KEY`（仅环境变量） | 快照 JSON → 结构化判断；已用 55 条真实新闻验证抽取 |
| `jev` | **未配置** | `JEV_ENDPOINT` / `JEV_API_KEY` / `JEV_MODEL` | TypeSafe 风格 typed judgment；无端点 → UNAVAILABLE |
| `linear` | **未训练** | `LogisticModel` JSON | 纯 Python 逻辑回归；训练后导出权重，可在 Windows 推理 |

**Jev 注意**：返回的 `confidence` 是模型自述的置信度，**不是** BTC 上涨概率；
`choice=NOUL`（不确定）映射为 HOLD。不要用它替代回测。

---

## 3. 训练与迁移

- `research.decision.train(samples, labels)`：逻辑回归，样本 <30 直接拒绝；
- `LogisticModel.save/load`：JSON 权重 + 特征名 + 样本数，跨机器可迁移；
- 特征契约 `FEATURES`：trend/momentum/ADX/RSI/ATR + 新闻/预测市场/钱包/微观 bias；
- Spark 上训练、Windows 上推理：模型文件是纯 JSON，无 GPU、无框架依赖。

---

## 4. 假设卡（来自 Trader Playbook）

`docs/research/hypotheses/`：

| ID | 假设 | 对应熬鹰方法 |
|---|---|---|
| `TA-EVENT-001` | 事件方向 + 价格确认一致的突破，净收益优于纯 K 线突破 | 先事件、后价格确认 |
| `TA-TREND-002` | 趋势跟随的亏损主要来自状态切换期入场 | 先大周期、走势不对立即认错 |
| `TA-HOLD-003` | 低 ADX 震荡期强制 HOLD 减少亏损且不牺牲趋势收益 | 不适合的行情宁可空仓 |

三张卡均通过 `openalpha-trader-playbook/scripts/validate_hypotheses.py` 校验。
**校验只说明格式合规，不代表策略有效。**

---

## 5. 验证边界

1. Provider 输出只写 `data/providers.jsonl`，不影响交易；
2. 各 provider 的模拟表现由**同一批决策点、同一成本模型**比较；
3. 样本 <30 或 CI 跨 0 → INCONCLUSIVE；
4. 任何"提升为 VERIFIED"都必须有独立验证报告 ID，provider 与 LLM 无权；
5. Jev/DeepSeek 的置信度不是概率，不用于仓位。

---

## 6. 下一步

- 在 Spark 上配置 `JEV_ENDPOINT/KEY`，先用 20 条快照对比 rules vs jev vs deepseek；
- 用 `scripts/forward_test_v1.sh` 累积 ≥72 轮真实时间运行，保存 `providers.jsonl`；
- 用消融框架比较四类 provider 的净收益，而不是比较它们的自述置信度；
- 训练第一版 logistic 模型并导出 JSON，在 Windows 上验证推理一致。

---

## 7. 接线状态（Sprint 6.1）

### 7.1 RunLoop 每轮生成 providers.jsonl

`RunLoop(providers=[...], providers_log_path=...)`：每个**成功**决策点调用一次
`compare.run()`，写入：

- `as_of_ms` / `completed_at_ms` / `symbol`
- `snapshot`（state 摘要 + 五类特征）
- `decisions[]`（provider / version / status / choice / score / confidence /
  rationale / evidence / latency_ms）
- `note: research only; not applied to Selection/Risk`

`live`/`replay` 默认 `rules`；有 `DEEPSEEK_API_KEY` 加 deepseek；有
`JEV_ENDPOINT` + `JEV_API_KEY` 加 jev；`--model path.json` 加 linear。
`--no-providers` 关闭。provider 失败只进 `provider_errors`，不影响交易。

### 7.2 Jev 协议

按公开资料改为 `{model, state, questions}` → `answers`；`noul` 映射 HOLD。
兼容 `answers` 为 dict/list 与旧式 `output`。没有可验证访问权限时保持
`UNAVAILABLE`，不付费、不阻塞。

### 7.3 Point-in-time 数据集

`research/decision/dataset.py`：

- 从决策日志提取特征；标签用未来 1h/4h/24h 收盘价，**兑现后才写**，否则 `None`；
- `purged_split()` 按时间切分并留 embargo；
- `labelled()` 只返回已兑现样本。

**30 条只是 `train()` 的代码下限，不是有效样本量。** 9 个特征 + 相邻小时高相关，
至少需要数百个独立决策点并做 purged walk-forward，才有资格讨论"训练有效"。

### 7.4 DeepSeek 真实快照实测（2026-11-21）

```
python scripts/deepseek_snapshot_smoke.py --rounds 20
20 轮全部 OK，全 HOLD，平均 1175ms/次，总 23.6s
```

20 个历史决策点当时都是 `RANGE` + 弱 ADX（11–14），DeepSeek 的判断是
"没有方向证据，不做"——这正是期望的 HOLD 行为，而不是为了给方向而给方向。