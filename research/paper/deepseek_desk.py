"""DeepSeek 独立实验盘（P1）：自己的 1000 USDT、自己的账本、自己的成交记录。

规则（事前固定，不看结果调）：
  - DeepSeek 只给 LONG/SHORT/HOLD；HOLD 不交易，不能强迫模型开仓；
  - LONG/SHORT 必须经过同一套确定性风控 `evaluate()`；
  - 仓位 = RiskLimits.max_notional（默认 100），止损 1.5×ATR、止盈 3×ATR、最长 4h；
  - 手续费/滑点/资金费与 Rules 实验盘完全相同的参数；
  - 每笔交易记录对应决策理由与 provider 版本；
  - 不改变 VERIFIED、风控参数或任何真实交易接口。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from research.decision import MarketSnapshot, Provider, Decision
from research.market.contracts import Candle, MarketState
from research.paper.broker import PaperBroker
from research.risk.engine import RiskDecision, RiskLimits, evaluate
from research.runtime.loop import utc_day_start
from research.strategy.contracts import TradeIntent

D = Decimal


@dataclass
class DeskConfig:
    symbol: str = "BTC/USDT"
    notional: Decimal = D("100")
    stop_atr: Decimal = D("1.5")
    tp_atr: Decimal = D("3")
    max_holding_ms: int = 4 * 3_600_000
    spread_bps: Decimal = D("5")


@dataclass
class DeepSeekDesk:
    broker: PaperBroker
    provider: Provider
    limits: RiskLimits
    log_path: Path | str
    config: DeskConfig = field(default_factory=DeskConfig)

    def _intent(self, decision: Decision, state: MarketState,
                as_of_ms: int) -> TradeIntent | None:
        close = state.features.get("close")
        atr = state.features.get("atr_abs")
        if close is None or atr is None or atr <= 0:
            return None
        side = "LONG" if decision.choice == "LONG" else "SHORT"
        sign = D("1") if side == "LONG" else D("-1")
        stop = close - sign * self.config.stop_atr * atr
        tp = close + sign * self.config.tp_atr * atr
        if stop <= 0 or tp <= 0:
            return None
        iid = hashlib.sha256(
            f"deepseek|{as_of_ms // 3_600_000 * 3_600_000}|{side}|"
            f"{decision.version}".encode()).hexdigest()[:16]
        return TradeIntent(
            intent_id=iid, decision_ts_ms=as_of_ms, symbol=self.config.symbol,
            side=side, strategy_id="deepseek", strategy_version=decision.version,
            entry_ref_price=close, stop_price=stop, take_profit_price=tp,
            expected_holding_ms=self.config.max_holding_ms,
            confidence=decision.confidence, reason=decision.rationale or "deepseek",
            shadow=True,          # 实验盘语义：非验证策略
        )

    def step(self, state: MarketState, as_of_ms: int,
             bar: Candle | None) -> dict:
        """先处理持仓（mark/expire），再让 DeepSeek 判断一次。"""
        closed = []
        if bar is not None:
            closed += self.broker.mark_bar(self.config.symbol, bar.open, bar.high,
                                           bar.low, as_of_ms, bar_open_ms=bar.open_ms)
        mark = state.features.get("close")
        if mark is not None:
            closed += self.broker.expire(as_of_ms, {self.config.symbol: mark})

        decision = self.provider.decide(
            MarketSnapshot(as_of_ms, self.config.symbol, state, None))
        risk: RiskDecision | None = None
        fill = None
        if decision.actionable:
            intent = self._intent(decision, state, as_of_ms)
            if intent is not None:
                account = self.broker.account(utc_day_start(as_of_ms))
                risk = evaluate(intent, state, account, self.limits, self.config.spread_bps)
                if risk.decision == "ALLOW":
                    fill = self.broker.submit(intent, min(risk.notional,
                                                          self.config.notional))

        entry = {
            "as_of_ms": as_of_ms,
            "provider": decision.provider,
            "version": decision.version,
            "status": decision.status,
            "choice": decision.choice,
            "confidence": str(decision.confidence),
            "rationale": decision.rationale,
            "risk": risk.to_dict() if risk else None,
            "fill": fill.to_dict() if fill else None,
            "closed": [c.to_dict() for c in closed],
            "equity": str(self.broker.equity(as_of_ms,
                                             {self.config.symbol: mark} if mark else {})),
        }
        path = Path(self.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry