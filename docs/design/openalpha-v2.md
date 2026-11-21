# OpenAlpha V2 — 5m 实时模拟引擎（P0/P1 落地）

> 状态：**LIVE PAPER，三本独立账本，全虚拟资金。** P2（Polymarket 价值盘）尚未实现，不在此文档中冒充完成。

## 运行

```bash
export DEEPSEEK_API_KEY=...
python -m research.runtime v2 --root data/openalpha_v2 --minutes 90
```

- 5m 已收盘 K 线触发决策；1 秒级价格监控持仓（止损/止盈），**不依赖 LLM 在线**；
- 三本独立账本：`rules_5m.sqlite`、`deepseek_5m.sqlite`、`dual_5m.sqlite`；
- 每本账独立决策日志 `*_decisions.jsonl`，含决策、风控、成交、平仓、权益；
- 心跳：`state/heartbeat_v2.json`（价格、feed 状态、三本账订单/持仓/现金）。

## 决策链

```
5m 收盘 → 本地指标（EMA/ATR/RSI/ADX/量能）
        → 规则预筛（数据陈旧 / ATR 预算 < 成本 → HOLD）
        → rules_5m: 现有策略注册表
        → deepseek_5m: DeepSeek 结构化判断（带 intel digest）
        → dual_5m: DeepSeek + 第二模型；无凭据 → second=UNAVAILABLE（不假装）
        → 确定性风控 evaluate() → PaperBroker
```

## 退出机制

| 机制 | 说明 |
|---|---|
| 固定 ATR 止损/止盈 | 1.5×ATR / 3×ATR |
| 实时 guard | 每 tick 用最新价检查止损/止盈（`on_tick`） |
| 2 根 5m 认错 | 持仓 ≥ 2 根 5m 且浮亏 → `EARLY_EXIT`（A/B 实验组） |

## 当前运行证据（2026-11-22 00:39 CST）

```
rules_5m    HOLD  no strategy (TREND_DOWN/HIGH_VOL, momentum conflict)
deepseek_5m HOLD  downtrend with strong ADX but momentum up; conflicting signals favor waiting
dual_5m     HOLD  ... second=UNAVAILABLE
```

- 三本账 equity 均 1,000，0 单 —— 当前状态无优势，**不做**是正确输出；
- 订单路径由 `tests/runtime/test_v2_engine.py` 覆盖（开仓/止损/早退/隔离/预筛）。

## 限制

1. 5m 触发依赖 feed；本机 WS 不稳时降级 3 秒 REST，决策仍按已收盘 5m bar；
2. 第二模型未配置（Grok/Jev 凭据缺失），`dual_5m` 目前等同 DeepSeek；
3. Polymarket 独立概率盘（P2）未实现；
4. 未接真实资金、未导入任何交易所密钥。

## 下一步

- P2：Polymarket YES/NO 双盘口、扣费 Edge、独立 `polymarket_paper.sqlite`；
- 用真实成交样本（≥100 笔）比较三本账的净收益与退出原因分布，而不是比较 LLM 置信度。