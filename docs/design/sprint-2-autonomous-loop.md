# Sprint 2 — 自主决策与模拟交易闭环

> 状态：**IMPLEMENTED / PAPER-ONLY**。链路：
> `MarketState → StrategyRegistry → TradeIntent → RiskDecision → PaperBroker → DecisionLog`
>
> 边界：不接实盘、不接私钥、不自动跟单。所有"成交"都在本地 sqlite 模拟账本里。

---

## 1. 交付物

| 分区 | 文件 | 职责 |
|---|---|---|
| 数据质量门（前置修复） | `research/market/contracts.py`、`state.py` | 逐周期陈旧、真实 `close_ms`、缺口进入风险、策略特征 |
| 策略契约 | `research/strategy/contracts.py` | `StrategySpec` / `TradeIntent` / `Candidate` / `Selection` |
| 三策略 | `research/strategy/strategies.py` | 趋势跟随 / 区间回归 / 突破确认 |
| 注册与选择 | `research/strategy/registry.py` | 状态过滤、验证状态、优先级、HOLD 原因 |
| 风控 | `research/risk/engine.py` | 10 项确定性检查 + 市场级门禁 |
| 模拟撮合 | `research/paper/broker.py` | 幂等订单、滑点、双边费、止损止盈、超时、恢复 |
| 运行时 | `research/runtime/round.py`、`log.py`、`__main__.py` | 一轮编排 + JSONL 决策日志 + demo |
| 测试 | `tests/market/`、`tests/strategy/`、`tests/risk/`、`tests/paper/`、`tests/e2e/` | 离线确定性 |

---

## 2. 数据质量门（Sprint 1 复审的三个问题）

| 问题 | 修法 |
|---|---|
| 整体陈旧度取"所有周期里最新的一根"，5m 新鲜掩盖 1h 陈旧 | 每个周期独立算 `age_ms` / `stale`；`critical_stale` 只看 **1h**（趋势周期）；`per_timeframe` 全量记录 |
| `last_closed_ms` 实际是 `open_ms` | 改为 `close_ms`；另存 `last_open_ms`；陈旧度按"数据何时完整"算 |
| `ensure_contiguous` 存在但 `analyze` 只记录缺口 | `analyze` 报告 `contiguous` / `gaps` 不抛错；**风控层**据此 DENY；趋势周期缺口也进 `invalidations` |

新增策略特征（策略层不得重算）：`range_high` / `range_low` / `range_mid` / `range_width_bps` /
`breakout_ref_high` / `breakout_ref_low` / `atr_abs` / `atr_pct` / `ema20|60|200` / `adx` /
`rsi` / `volume_z` / `slope_bps` / `distance_to_range_high_bps` / `distance_to_range_low_bps`。

---

## 3. 策略注册

| 策略 | 适用状态 | 方向 | 入场 | 止损 / 止盈 |
|---|---|---|---|---|
| `trend_follow` | TREND_UP/DOWN | 顺势 | 收盘价 | 1.5×ATR / 3×ATR |
| `range_revert` | RANGE | 逆势 | 距区间边界 ≤25% 宽度 | 边界 ±0.5×ATR / 区间中位 |
| `breakout_confirm` | RANGE→趋势 | 突破方向 | 收盘突破前高/低 ≥5bps 且 量能 z ≥ 1 | 参考价 ∓0.5×ATR / 2×ATR |

验证状态是硬门：

- `FALSIFIED` → 排除；
- `UNTESTED` / `SHADOW` → 只生成**影子意图**（走完整风控与日志，但不进账本）；
- `VERIFIED` → 才允许占用账本额度（Sprint 2 默认没有任何策略是 VERIFIED）。

选择规则：状态适用 → 策略函数给出方向 → 按预注册 `priority` 取最小者。**不按近期表现择优**。
全部不可用 → `HOLD`，原因优先写数据问题，其次逐条列出策略弃权码。

---

## 4. 风控（独立、确定性、顺序固定）

| 顺序 | 码 | 结果 | 条件 |
|---|---|---|---|
| 1 | `KILL_SWITCH` | FREEZE | 人工/熔断开关 |
| 2 | `DATA_STALE` | FREEZE | 1h 数据陈旧 |
| 3 | `DATA_GAPS` | DENY | 1h 缺口 |
| 4 | `RISK_EXTREME` | DENY | 状态机极端风险 |
| 5 | `SPREAD_TOO_WIDE` | DENY | 点差 > 20bps |
| 6 | `COST_TOO_HIGH` / `COST_UNKNOWN` | DENY | EV_net = 0.5×ATR% − (2×fee + spread + slip) < 5bps |
| 7 | `MAX_POSITIONS` | DENY | 持仓数上限 |
| 8 | `EXPOSURE_LIMIT` | DENY | 总敞口上限 |
| 9 | `DAILY_LOSS` | FREEZE | 当日已实现亏损超限 |
| 10 | `INSUFFICIENT_CASH` | DENY | 现金不足（含手续费） |

没有意图时仍执行 `evaluate_market_gate()`：数据陈旧/缺口必须在风控层留下明确记录，
而不是一个语焉不详的 HOLD。

---

## 5. 模拟撮合

- **幂等**：`orders.intent_id` 主键 + `INSERT OR IGNORE`；同一轮重复执行不会产生第二笔成交。
- **价格**：开仓 `entry_ref × (1 ± 5bps)`；平仓同样计滑点。
- **费用**：双边 taker 5bps；开仓即扣，平仓再扣。
- **方向对称**：LONG/SHORT 都占用 notional 作保证金，平仓返还并结算 pnl。
  这是**简化模型**（不是真实借币/保证金），日志与文档标注。
- **退出**：止损/止盈按触发价成交；超时按最近记账价平仓。
- **恢复**：`restore()` 从账本读回现金与持仓；`account()` 给风控提供一致快照。

---

## 6. 决策日志（每轮一条 JSONL）

```json
{
  "round_ts_ms": 0, "symbol": "BTC/USDT",
  "state": {"trend": "...", "volatility": "...", "momentum": "...", "risk": "...",
            "confidence": "...", "reason": "...", "evidence": [], "contradictions": [],
            "invalidations": [], "data_quality": {}, "features": {}},
  "selection": {"decision": "LONG|SHORT|HOLD", "intent": {}, "candidates": [], "hold_reason": ""},
  "risk": {"decision": "ALLOW|DENY|FREEZE", "codes": [], "checks": {}, "notional": "..."},
  "fill": {"intent_id": "...", "price": "...", "fee": "...", "qty": "..."},
  "closed": [], "equity": "...", "hold_reason": ""
}
```

每一笔成交都能回溯到：`state.evidence` → `candidate` → `intent_id` → `risk.checks` → `fill`。

---

## 7. 真实演示输出

```
$ python -m research.runtime demo --verified range_revert
[trend_up] decision=LONG strategy=trend_follow shadow=False
  state: TREND_UP/LOW_VOL/MOMENTUM_UP/RISK_LOW conf=0.90
  intent: LONG entry=161.85 stop=161.04 tp=163.47
  risk: DENY ('COST_TOO_HIGH',) — EV_net=-3.3bps < 5bps（毛 16.7 − 成本 20.0）
  fill: none                       # 有信号，但不够付成本：不做
[range] decision=LONG strategy=range_revert shadow=False
  state: RANGE/NORMAL_VOL/MOMENTUM_UP/RISK_NORMAL conf=0.60
  risk: ALLOW ('OK',) — 放行 100 USDT（EV_net=11.2bps）
  fill: {'intent_id': '1f1b08898cafd627', 'side': 'LONG', 'price': '99.7945', 'fee': '0.05', ...}
[high_vol] decision=HOLD
  hold: HOLD: 无策略适用（trend=RANGE, vol=HIGH_VOL）；trend_follow=STATE_NOT_APPLICABLE，
        breakout_confirm=NO_BREAKOUT, range_revert=NOT_AT_RANGE_EDGE
```

三种结果同时出现：**值得做并成交 / 有信号但成本不允许 / 没有策略适用**。
默认（不加 `--verified`）全部是影子意图：走完整风控并写日志，但不进账本。

---

## 8. 测试

```bash
python -m pytest tests -q
# 564 + Sprint 2 新增（market 数据门 6 / strategy 11 / risk 12 / paper 10 / e2e 11）
```

| 层 | 覆盖 |
|---|---|
| `tests/market/test_data_gate.py` | 5m 新鲜不掩盖 1h 陈旧、close_ms、逐周期 age、4h 陈旧不误伤、缺口不抛错、特征值 |
| `tests/strategy` | 三策略方向/止损止盈关系、弃权码、优先级、FALSIFIED 排除、影子/VERIFIED |
| `tests/risk` | 10 项检查逐一触发、数据门短路优先、无意图市场门禁 |
| `tests/paper` | 幂等、滑点/费用、止损/止盈/超时、权益、恢复、现金不足 |
| `tests/e2e` | 上涨/下跌/震荡/缺口/断流/无策略/影子/幂等/重启恢复/熔断/止损平仓 |

---

## 9. 明确限制

- 没有 L2 盘口、资金费、OI：Sprint 3 从 `cross_market` 采集器接线，作为额外证据；
- SHORT 是名义记账（不借币、不计资金费），只用于研究；
- 策略参数（1.5/3 ATR、25% 边界、5bps 突破缓冲）是**先验**，未做样本外验证；
  因此默认 `UNTESTED`，只能影子；
- 状态机与风控都是规则模型，不保证盈利；它们保证的是"不失控、可复盘"。

---

## 10. Sprint 3 接口

- 新闻与 Polymarket：默认 `advisory_only=true`，不改变 `Selection`，只写日志；
- 增量验证通过后（样本外、成本、稳健性），才允许把信号接进 `StrategySpec` 的适用条件；
- `VERIFIED` 的授予必须由独立验证链路产生，不能由运行时自动升级。