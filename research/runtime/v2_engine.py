"""OpenAlpha V2 5m 引擎：三本独立模拟账本 + 实时持仓监控 + 情报注入。

设计要点：
  - 5m 已收盘 K 线触发决策；1s 级价格监控持仓（止损/止盈），不依赖 LLM 在线；
  - 三本账：rules_5m（确定性）、deepseek_5m（规则预筛 + DeepSeek）、dual_5m
    （再加第二模型；无凭据则 UNAVAILABLE，不假装存在）；
  - 模型输出只是候选，必须经过同一套确定性风控；
  - 每本账独立 sqlite 与决策日志，收盘后仍按 2 根 5m 认错（EARLY_EXIT）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from research.decision import Decision, MarketSnapshot, Provider
from research.intel.features import compute as compute_intel
from research.market.contracts import Candle, TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.klines import closed_only
from research.market.state import analyze
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits, evaluate
from research.strategy.contracts import StrategySpec, TradeIntent
from research.strategy.registry import DEFAULT_SPECS, select

D = Decimal
SYMBOL = "BTC/USDT"
BOOKS = ("rules_5m", "deepseek_5m", "dual_5m")
TFS = (("1m", 60_000), ("5m", TF_5M_MS), ("15m", TF_15M_MS),
       ("1h", TF_1H_MS), ("4h", TF_4H_MS))


@dataclass
class V2Config:
    decision_ms: int = TF_5M_MS
    notional: Decimal = D("100")
    stop_atr: Decimal = D("1.5")
    tp_atr: Decimal = D("3")
    exit_bars: int = 2
    max_positions: int = 1
    spread_bps: Decimal = D("5")


@dataclass
class V2Engine:
    root: Path | str
    feed: object
    limits: RiskLimits
    deepseek: Provider | None = None
    second: Provider | None = None
    intel_fn: object = None
    specs: tuple[StrategySpec, ...] = DEFAULT_SPECS
    config: V2Config = field(default_factory=V2Config)
    books: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    _last_bar: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    errors: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in BOOKS:
            self.books[name] = PaperBroker(
                self.root / f"{name}.sqlite", starting_cash=D("1000"),
                funding_bps_per_8h=D("1"), shadow_mode="shadow_only")

    # ---------- 行情 ----------
    def load_history(self) -> None:
        """用成熟的 REST 客户端拉一次 1h/4h/5m/15m 历史，避免依赖 feed 的后台回填。"""
        from research.runtime.live import BinanceKlineSource

        try:
            views = BinanceKlineSource(symbol="BTCUSDT", limit=500, timeout_s=15,
                                       retries=1).candles(int(time.time() * 1000))
            self.history = {tf: list(views.get(tf) or []) for tf in ("5m", "15m", "1h", "4h")}
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"history: {type(exc).__name__}: {exc}")

    def _candles(self, tf: str) -> list[Candle]:
        if getattr(self, "history", None) and self.history.get(tf):
            return self.history[tf]
        out = []
        src = self.feed.base_series(tf) if (tf in ("1h", "4h") and hasattr(self.feed, "base_series")) else self.feed.series(tf, 500)
        for b in src:
            try:
                out.append(Candle(b["time"] * 1000, D(str(b["open"])), D(str(b["high"])),
                                  D(str(b["low"])), D(str(b["close"])), D(str(b["volume"])),
                                  dict(TFS)[tf]))
            except (ValueError, ArithmeticError):
                continue
        return out

    def _views(self) -> dict:
        return {tf: self._candles(tf) for tf, _ in TFS}

    def price(self) -> float | None:
        return self.feed.price()

    # ---------- 决策 ----------
    def _prefilter(self, state) -> str | None:
        dq = state.data_quality
        if dq is not None and dq.critical_stale:
            return "STALE"
        atr = state.features.get("atr_abs")
        close = state.features.get("close")
        if atr is None or not close:
            return "NO_ATR"
        atr_pct = atr / close
        budget = atr_pct * D("10000") * D("0.5")
        cost = self.limits.fee_bps * 2 + self.limits.slippage_bps * 2 + self.config.spread_bps
        if budget < cost:
            return "COST_TOO_HIGH"
        return None

    def _rules_decision(self, state) -> Decision:
        sel = select(state, self.specs)
        if sel.intent is not None:
            choice = "LONG" if sel.intent.side == "LONG" else "SHORT"
            return Decision("rules_5m", "registry", "OK", choice, 1.0, state.confidence,
                            sel.intent.reason, (sel.intent.strategy_id,))
        return Decision("rules_5m", "registry", "OK", "HOLD", 0.0, D("0"),
                        sel.hold_reason or "no strategy", ())

    def _model_decision(self, name: str, provider: Provider | None, state) -> Decision:
        reason = self._prefilter(state)
        if reason:
            return Decision(name, "prefilter", "NO_DATA", "HOLD", 0.0, D("0"), reason)
        if provider is None or not provider.available():
            return Decision(name, "unavailable", "UNAVAILABLE", "HOLD", 0.0, D("0"),
                            "provider not configured")
        snap = MarketSnapshot(int(time.time() * 1000), SYMBOL, state, compute_intel(state, None))
        return provider.decide(snap)

    def _decide(self, name: str, state) -> Decision:
        if name == "rules_5m":
            return self._rules_decision(state)
        ds = self._model_decision("deepseek_5m", self.deepseek, state)
        if name == "deepseek_5m":
            return ds
        second = self._model_decision("dual_5m", self.second, state)
        if second.status != "OK":
            return Decision("dual_5m", ds.version, "OK", ds.choice, ds.score, ds.confidence,
                            f"{ds.rationale} | second=UNAVAILABLE", ds.evidence)
        if ds.choice == second.choice:
            return Decision("dual_5m", "dual", "OK", ds.choice, (ds.score + second.score) / 2,
                            min(ds.confidence, second.confidence),
                            f"agree: {ds.rationale} | {second.rationale}",
                            ds.evidence + second.evidence)
        return Decision("dual_5m", "dual", "OK", "HOLD", 0.0, D("0"),
                        f"disagree: ds={ds.choice} second={second.choice}", ())

    # ---------- 成交 ----------
    def _intent(self, name: str, decision: Decision, state, as_of_ms: int) -> TradeIntent | None:
        close, atr = state.features.get("close"), state.features.get("atr_abs")
        if close is None or atr is None or atr <= 0:
            return None
        side = "LONG" if decision.choice == "LONG" else "SHORT"
        sign = D("1") if side == "LONG" else D("-1")
        iid = hashlib.sha256(f"{name}|{as_of_ms}|{side}".encode()).hexdigest()[:16]
        return TradeIntent(
            iid, as_of_ms, SYMBOL, side, name, decision.version, close,
            close - sign * self.config.stop_atr * atr,
            close + sign * self.config.tp_atr * atr,
            self.config.max_holding_ms if hasattr(self.config, "max_holding_ms") else 4 * TF_1H_MS,
            decision.confidence, decision.rationale or name, shadow=True)

    def _enter(self, name: str, decision: Decision, state, as_of_ms: int) -> dict | None:
        broker = self.books[name]
        if len(broker.open_positions()) >= self.config.max_positions:
            return None
        intent = self._intent(name, decision, state, as_of_ms)
        if intent is None:
            return None
        risk = evaluate(intent, state, broker.account(as_of_ms // 86_400_000 * 86_400_000),
                        self.limits, self.config.spread_bps)
        if risk.decision != "ALLOW":
            return {"risk": risk.to_dict()}
        fill = broker.submit(intent, min(risk.notional, self.config.notional))
        return {"risk": risk.to_dict(), "fill": fill.to_dict() if fill else None}

    # ---------- 退出 ----------
    def _early_exit(self, name: str, state, as_of_ms: int) -> list:
        broker = self.books[name]
        closed = []
        for pos in broker.open_positions():
            age = as_of_ms - int(pos["entry_ts"])
            if age < self.config.exit_bars * self.config.decision_ms:
                continue
            mark = state.features.get("close")
            if mark is None:
                continue
            direction = D("1") if pos["side"] == "LONG" else D("-1")
            pnl = (mark - D(pos["entry_price"])) * D(pos["qty"]) * direction
            if pnl < 0:
                closed.append(broker.close_position(int(pos["id"]), mark, as_of_ms, "EARLY_EXIT"))
        return [c for c in closed if c]

    def on_tick(self, price: float | None, ts_ms: int) -> list:
        if price is None:
            return []
        closed = []
        for name, broker in self.books.items():
            for pos in broker.open_positions():
                stop, tp = D(pos["stop_price"]), D(pos["tp_price"])
                px = D(str(price))
                if pos["side"] == "LONG":
                    if px <= stop:
                        closed.append(broker.close_position(int(pos["id"]), px, ts_ms, "STOP_LOSS"))
                    elif px >= tp:
                        closed.append(broker.close_position(int(pos["id"]), px, ts_ms, "TAKE_PROFIT"))
                else:
                    if px >= stop:
                        closed.append(broker.close_position(int(pos["id"]), px, ts_ms, "STOP_LOSS"))
                    elif px <= tp:
                        closed.append(broker.close_position(int(pos["id"]), px, ts_ms, "TAKE_PROFIT"))
        self._write_heartbeat(ts_ms)
        return [c for c in closed if c]

    # ---------- 5m 决策 ----------
    def on_5m_close(self, as_of_ms: int) -> dict:
        views = self._views()
        if not views.get("5m"):
            return {}
        state = analyze(SYMBOL, views, as_of_ms)
        out = {}
        for name in BOOKS:
            decision = self._decide(name, state)
            entry = self._enter(name, decision, state, as_of_ms) if decision.actionable else None
            closed = self._early_exit(name, state, as_of_ms)
            entry = dict(entry or {})
            entry.update({"closed": [c.to_dict() for c in closed]})
            log = {"as_of_ms": as_of_ms, "book": name, "decision": decision.to_dict(),
                   "entry": entry or None,
                   "equity": str(self.books[name].equity(as_of_ms,
                                                         {SYMBOL: D(str(state.features.get("close") or 0))}))}
            p = self.root / f"{name}_decisions.jsonl"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(log, ensure_ascii=False, sort_keys=True) + "\n")
            out[name] = log
        self._last_bar = as_of_ms
        return out

    def _write_heartbeat(self, ts_ms: int) -> None:
        hb = self.root / "state" / "heartbeat_v2.json"
        hb.parent.mkdir(parents=True, exist_ok=True)
        price = self.price()
        payload = {
            "updated_at_ms": ts_ms,
            "price": price,
            "books": {name: {"orders": int(self.books[name]._db.execute(
                "SELECT COUNT(*) FROM orders").fetchone()[0]),
                "open": len(self.books[name].open_positions()),
                "cash": str(self.books[name].cash())} for name in BOOKS},
            "feed": self.feed.status() if hasattr(self.feed, "status") else None,
            "errors": self.errors[-3:],
        }
        tmp = hb.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(hb)

    # ---------- 运行 ----------
    def run(self, minutes: float = 30.0, poll_s: float = 1.0) -> None:
        deadline = time.time() + minutes * 60
        while time.time() < deadline and not self._stop.is_set():
            now = int(time.time() * 1000)
            try:
                self.on_tick(self.price(), now)
                bar = self.feed.latest("5m")
                if bar and int(bar["time"]) * 1000 > self._last_bar + self.config.decision_ms - 1000:
                    self.on_5m_close(int(bar["time"]) * 1000)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(poll_s)

    def stop(self) -> None:
        self._stop.set()