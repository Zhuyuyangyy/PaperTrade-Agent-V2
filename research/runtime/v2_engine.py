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
    #: 收盘宽限：K 线 close_ms 必须 <= as_of - settle_ms 才算已收盘
    settle_ms: int = 30_000
    #: 历史刷新周期（5 分钟）；失败不推进，按退避重试
    history_refresh_ms: int = 300_000
    #: 最新已收盘 5m K 线最多能比 as_of 旧多久；超过就不许开新仓
    #: （Binance REST 会 418/429 限流，刷新连续失败时会触发）
    max_stale_ms: int = 3 * 300_000
    #: 最长持有期（超时平仓）
    max_holding_ms: int = 4 * TF_1H_MS


@dataclass
class V2Engine:
    root: Path | str
    feed: object
    limits: RiskLimits
    deepseek: Provider | None = None
    second: Provider | None = None
    intel_fn: object = None
    #: Demo 执行适配器（research.execution.adapter）。None = 纯本地虚拟盘，一个订单都不发。
    executor: object = None
    #: 只把这本账镜像到交易所。None = 不镜像（默认）。
    demo_book: str | None = None
    specs: tuple[StrategySpec, ...] = DEFAULT_SPECS
    config: V2Config = field(default_factory=V2Config)
    books: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    _last_bar: int = 0
    _history_ts: int = 0
    #: 最近一次成功刷新的数据指纹（各周期最后一根 K 线的 open_ms）
    _history_fp: dict = field(default_factory=dict)
    #: 已镜像到交易所的 intent_id → TradeIntent（用于平仓时撤条件单）
    _mirrored: dict = field(default_factory=dict)
    #: 镜像出现「无法确认」的失败后置 True：停止镜像直到重启，避免在状态不清时继续发单
    mirror_halted: bool = False
    #: 镜像出现「无法确认」的失败后置 True：停止镜像直到重启，避免在状态不清时继续发单
    mirror_halted: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    errors: list = field(default_factory=list)
    #: 最近一轮的实际模型输入（供审计：证明 digest 真的进了模型）
    last_inputs: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in BOOKS:
            self.books[name] = PaperBroker(
                self.root / f"{name}.sqlite", starting_cash=D("1000"),
                funding_bps_per_8h=D("1"), shadow_mode="shadow_only")

    # ---------- 行情 ----------
    def load_history(self, as_of_ms: int | None = None) -> bool:
        """拉一次 1h/4h/5m/15m 历史。**只有拿到已收盘且非空的数据才算成功**。

        返回 True 才允许调用方推进 ``_history_ts``。失败时保留旧 history 并记错，
        绝不把「函数被调用过」当成「数据刷新过」。
        """
        from research.runtime.live import BinanceKlineSource

        now_ms = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
        try:
            views = BinanceKlineSource(symbol="BTCUSDT", limit=500, timeout_s=15,
                                       retries=1).candles(now_ms)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"history: {type(exc).__name__}: {exc}")
            return False
        fresh = {tf: list(views.get(tf) or []) for tf in ("5m", "15m", "1h", "4h")}
        if not fresh.get("5m") or not fresh.get("1h"):
            self.errors.append("history: 刷新结果缺少 5m/1h 已收盘 K 线")
            return False
        # 只接受比现有数据更新（或首次加载）的快照，防止乱序响应把数据改老
        old_5m = (self.history.get("5m") or [None])[-1]
        new_5m = fresh["5m"][-1]
        if old_5m is not None and new_5m.open_ms < old_5m.open_ms:
            self.errors.append(
                f"history: 刷新结果过期（新 {new_5m.open_ms} < 旧 {old_5m.open_ms}）")
            return False
        if new_5m.close_ms > now_ms:
            self.errors.append(f"history: 5m 末根未收盘（close_ms={new_5m.close_ms} > {now_ms}）")
            return False
        self.history = fresh
        self._history_fp = {tf: (v[-1].open_ms if v else 0) for tf, v in fresh.items()}
        return True

    def _candles(self, tf: str) -> list[Candle]:
        """raw 序列（可能含未收盘末根）；裁剪在 ``_views`` 里按 as_of 做。"""
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

    def _views(self, as_of_ms: int | None = None) -> dict:
        """只给**已收盘** K 线：``close_ms <= as_of - settle``。

        settle 不是可选项：最后一根刚收盘的 K 线在交易所侧可能还有尾部成交在落库，
        历史上本引擎用 feed 里**正在形成**的那根当 as_of，反而把刚收盘的一根排除掉，
        等于所有决策都延迟一根 5m。
        """
        now_ms = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
        cutoff = now_ms - self.config.settle_ms
        return {tf: closed_only(self._candles(tf), cutoff) for tf, _ in TFS}

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

    def _snapshot(self, state, as_of_ms: int, digest=None) -> MarketSnapshot:
        """同一根收盘 K 线只构造一次快照（含情报特征），三个账本共用。"""
        feats = compute_intel(state, digest)
        return MarketSnapshot(as_of_ms, SYMBOL, state, feats)

    def _model_decision(self, name: str, provider: Provider | None, snap: MarketSnapshot,
                        calls: list) -> Decision:
        reason = self._prefilter(snap.state)
        if reason:
            return Decision(name, "prefilter", "NO_DATA", "HOLD", 0.0, D("0"), reason)
        if provider is None or not provider.available():
            return Decision(name, "unavailable", "UNAVAILABLE", "HOLD", 0.0, D("0"),
                            "provider not configured")
        calls.append(name)
        return provider.decide(snap)

    def _decide(self, name: str, state, digest=None, memo: dict | None = None,
                as_of_ms: int | None = None) -> Decision:
        """三个账本的决策。同一快照内 DeepSeek 只调用一次，Dual 复用结果。

        ``memo`` 由 ``on_5m_close`` 每轮新建，生命周期就是一根 5m K 线。
        """
        if name == "rules_5m":
            return self._rules_decision(state)
        memo = memo if memo is not None else {}
        ts = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
        snap = memo.get("snap")
        if snap is None:
            snap = memo["snap"] = self._snapshot(state, ts, digest)
        calls = memo.setdefault("calls", [])
        ds = memo.get("deepseek")
        if ds is None:
            ds = memo["deepseek"] = self._model_decision("deepseek_5m", self.deepseek, snap, calls)
        if name == "deepseek_5m":
            return ds
        # 第二模型未配置：Dual 盘暂停开仓，不复制 DeepSeek 的决策，也不重复调用
        if self.second is None or not self.second.available():
            return Decision("dual_5m", ds.version, "UNAVAILABLE", "HOLD", 0.0, D("0"),
                            "dual_paused: second model not configured", ())
        second = memo.get("second")
        if second is None:
            second = memo["second"] = self._model_decision("dual_5m", self.second, snap, calls)
        if second.status != "OK":
            return Decision("dual_5m", ds.version, "UNAVAILABLE", "HOLD", 0.0, D("0"),
                            f"dual_paused: second={second.rationale}", ())
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
            self.config.max_holding_ms,
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
        notional = min(risk.notional, self.config.notional)
        fill = broker.submit(intent, notional)
        out = {"risk": risk.to_dict(), "fill": fill.to_dict() if fill else None}
        if fill is not None and name == self.demo_book and self.executor is not None:
            out["demo"] = self._mirror_open(intent, notional)
        return out

    # ---------- 交易所镜像 ----------
    def _mirror_open(self, intent: TradeIntent, notional) -> dict:
        """本地成交后镜像到 Demo 交易所。失败只记录，不吞掉本地记录。

        关键：**只有真的在交易所挂上了才登记到 `_mirrored`**。
        原实现先登记再判断 ok，导致镜像失败后仍被当成"有仓"，
        平仓时去平一个不存在的仓，本地与交易所从此对不上。
        镜像一旦出现无法确认的失败 → `mirror_halted`，停止镜像直到重启，
        避免在状态不清的情况下继续发单。
        """
        if self.mirror_halted:
            return {"ok": False, "error": "MIRROR_HALTED: 上一次镜像失败，已停止镜像"}
        try:
            res = self.executor.open(intent, notional)
        except Exception as exc:                     # noqa: BLE001 - 平不掉是致命，由适配器抛
            self.errors.append(f"demo_open {intent.intent_id}: {type(exc).__name__}: {exc}")
            self.mirror_halted = True
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "halted": True}
        if not res.ok:
            self.errors.append(f"demo_open {intent.intent_id}: {res.stage}: {res.error}")
            # 只有"确定没下出去"才允许继续镜像；无法确认就必须停
            uncertain = "无法确认" in (res.error or "") or res.stage in ("probe", "protect")
            if uncertain:
                self.mirror_halted = True
            self._reconcile_note(f"mirror_open_failed {intent.intent_id}")
            return {**res.to_dict(), "halted": self.mirror_halted}
        self._mirrored[intent.intent_id] = intent
        return res.to_dict()

    def _reconcile_note(self, tag: str) -> None:
        """镜像出问题后立刻核对一次交易所真实状态，结果写进 errors，不静默。"""
        if self.executor is None:
            return
        try:
            rec = self.executor.reconcile()
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"{tag}: reconcile 失败 {type(exc).__name__}: {exc}")
            return
        if rec.get("diffs"):
            self.errors.append(f"{tag}: reconcile diffs={rec['diffs']}")
            self.mirror_halted = True

    def _mirror_close(self, name: str, pos: dict, reason: str) -> dict | None:
        if name != self.demo_book or self.executor is None:
            return None
        if self.mirror_halted and str(pos.get("intent_id") or "") not in self._mirrored:
            # 从来没在交易所挂上过，别去平一个不存在的仓
            return {"ok": False, "error": "MIRROR_HALTED: 该仓未在交易所建立，跳过镜像平仓"}
        if self.mirror_halted and str(pos.get("intent_id") or "") not in self._mirrored:
            # 从来没在交易所挂上过，别去平一个不存在的仓
            return {"ok": False, "error": "MIRROR_HALTED: 该仓未在交易所建立，跳过镜像平仓"}
        intent = self._mirrored.get(str(pos.get("intent_id") or ""))
        if intent is None:
            # 重启后本地有持仓但内存丢了：用持仓行重建一个只用于平仓的意图
            intent = self._intent_from_position(pos)
        try:
            res = self.executor.close(intent, D(str(pos["qty"])), reason)
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"demo_close {pos.get('intent_id')}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._mirrored.pop(intent.intent_id, None)
        if not res.ok:
            self.errors.append(f"demo_close {intent.intent_id}: {res.error}")
        return res.to_dict()

    def _intent_from_position(self, pos: dict) -> TradeIntent:
        side = str(pos["side"])
        return TradeIntent(str(pos["intent_id"]), int(pos["entry_ts"]), str(pos["symbol"]), side,
                           str(pos.get("strategy_id") or "restored"), "restored",
                           D(str(pos["entry_price"])), D(str(pos["stop_price"])),
                           D(str(pos["tp_price"])), int(pos.get("max_holding_ms") or 4 * TF_1H_MS),
                           D("0.5"), "restored from ledger", shadow=True)

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
                c = broker.close_position(int(pos["id"]), mark, as_of_ms, "EARLY_EXIT")
                if c:
                    self._mirror_close(name, pos, "EARLY_EXIT")
                    closed.append(c)
        return [c for c in closed if c]

    def on_tick(self, price: float | None, ts_ms: int) -> list:
        if price is None:
            return []
        closed = []
        for name, broker in self.books.items():
            for pos in broker.open_positions():
                stop, tp = D(pos["stop_price"]), D(pos["tp_price"])
                px = D(str(price))
                reason = ""
                if pos["side"] == "LONG":
                    if px <= stop:
                        reason = "STOP_LOSS"
                    elif px >= tp:
                        reason = "TAKE_PROFIT"
                else:
                    if px >= stop:
                        reason = "STOP_LOSS"
                    elif px <= tp:
                        reason = "TAKE_PROFIT"
                if reason:
                    c = broker.close_position(int(pos["id"]), px, ts_ms, reason)
                    if c:
                        self._mirror_close(name, pos, reason)
                        closed.append(c)
        self._write_heartbeat(ts_ms)
        return [c for c in closed if c]

    # ---------- 5m 决策 ----------
    def _refresh_history(self, as_of_ms: int) -> dict:
        """按 5 分钟节流刷新历史；**失败不推进 _history_ts**，按 30s 退避重试。"""
        due = as_of_ms - self._history_ts
        if due < self.config.history_refresh_ms:
            return {"attempted": False, "ok": None, "due_in_ms": self.config.history_refresh_ms - due}
        ok = self.load_history(as_of_ms)
        if ok:
            self._history_ts = as_of_ms
        else:
            # 只把节流点推进一个退避间隔，下一次 5m 决策会重试，而不是等满 5 分钟
            self._history_ts = as_of_ms - self.config.history_refresh_ms + 30_000
        return {"attempted": True, "ok": ok,
                "fingerprint": dict(self._history_fp),
                "errors": self.errors[-2:] if not ok else []}

    @staticmethod
    def _stale_ms(views: dict, as_of_ms: int) -> int:
        """最新一根**参与决策的**已收盘 5m K 线距 as_of 多久（毫秒）。

        必须看 views 而不是 self.history：history 可能为空（测试注入 feed、或启动加载失败），
        真正喂给模型的永远是 views。
        """
        c5 = views.get("5m") or []
        if not c5:
            return 10 ** 9
        return int(as_of_ms) - int(c5[-1].close_ms)

    def _collect_intel(self, as_of_ms: int):
        """真正调用 intel_fn。情报失败不阻塞交易，但必须留痕。"""
        if self.intel_fn is None:
            return None, {"status": "DISABLED"}
        try:
            digest = self.intel_fn(as_of_ms)
        except Exception as exc:  # noqa: BLE001 - 情报是增益项，不是必要条件
            self.errors.append(f"intel: {type(exc).__name__}: {exc}")
            return None, {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        if digest is None:
            return None, {"status": "EMPTY"}
        return digest, {
            "status": "OK",
            "news": len(getattr(digest, "news", ()) or ()),
            "markets": len(getattr(digest, "markets", ()) or ()),
            "wallets": len(getattr(digest, "wallets", ()) or ()),
            "warnings": list(getattr(digest, "warnings", ()) or ()),
        }

    def on_5m_close(self, as_of_ms: int, bar_open_ms: int | None = None) -> dict:
        """一轮 5m 决策。

        ``as_of_ms`` 是**决策时刻**（通常 now），``bar_open_ms`` 是触发它的那根
        已收盘 K 线的 open。两者必须分开：早期版本把 bar 的 open 当 as_of，
        导致 settle 把这根刚收盘的 K 线又排除掉，实际一直用更早一根决策。
        """
        # 历史只在启动加载会越跑越旧（夜间 73/94 轮 STALE）；每 5 分钟刷新一次，
        # 但只有**真的拿到更新的已收盘数据**才推进 _history_ts。
        refresh = self._refresh_history(as_of_ms)
        views = self._views(as_of_ms)
        if not views.get("5m") or not views.get("1h"):
            return {}
        # 数据陈旧时**不许开新仓**：REST 被 418/429 限流时 refresh 会失败，
        # 此时 history 还是上一次的快照，继续交易等于拿旧行情下新单。
        stale_ms = self._stale_ms(views, as_of_ms)
        if stale_ms > self.config.max_stale_ms:
            self.errors.append(
                f"stale: 最新 5m 收盘已是 {stale_ms // 1000}s 前"
                f"（阈值 {self.config.max_stale_ms // 1000}s），本轮跳过开仓；"
                f"history_refresh={refresh.get('ok')}")
            return {}
        state = analyze(SYMBOL, views, as_of_ms, settle_ms=self.config.settle_ms)
        digest, intel_info = self._collect_intel(as_of_ms)
        # 同一根收盘 K 线：一个快照、一次 DeepSeek、一次第二模型
        memo: dict = {}
        out = {}
        for name in BOOKS:
            decision = self._decide(name, state, digest, memo=memo, as_of_ms=as_of_ms)
            entry = self._enter(name, decision, state, as_of_ms) if decision.actionable else None
            closed = self._early_exit(name, state, as_of_ms)
            entry = dict(entry or {})
            entry.update({"closed": [c.to_dict() for c in closed]})
            log = {"as_of_ms": as_of_ms, "book": name, "decision": decision.to_dict(),
                   "entry": entry or None,
                   "closed_bar_ms": int(views["5m"][-1].close_ms),
                   "trigger_bar_ms": int(bar_open_ms if bar_open_ms is not None
                                         else views["5m"][-1].open_ms),
                   "trigger_bar_ms": int(bar_open_ms if bar_open_ms is not None
                                         else views["5m"][-1].open_ms),
                   "history_refresh": refresh,
                   "intel": intel_info,
                   "equity": str(self.books[name].equity(as_of_ms,
                                                         {SYMBOL: D(str(state.features.get("close") or 0))}))}
            p = self.root / f"{name}_decisions.jsonl"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(log, ensure_ascii=False, sort_keys=True) + "\n")
            out[name] = log
        # 审计：模型实际收到了什么（证明 digest 真的进了 snapshot，不是只存在于参数里）
        snap = memo.get("snap")
        self.last_inputs = {
            "as_of_ms": as_of_ms,
            "closed_bar_ms": int(views["5m"][-1].close_ms),
            "trigger_bar_ms": int(bar_open_ms if bar_open_ms is not None
                                  else views["5m"][-1].open_ms),
            "trigger_bar_ms": int(bar_open_ms if bar_open_ms is not None
                                  else views["5m"][-1].open_ms),
            "intel": intel_info,
            "model_calls": list(memo.get("calls") or []),
            "intel_features": (snap.intel.to_dict() if snap is not None and snap.intel else None),
        }
        # _last_bar 只记录**触发的那根 K 线**的 open_ms，绝不能存 as_of：
        # run() 是拿它和 bar_open 比的，存 as_of 会让下一根永远比不上，引擎静默停摆。
        self._last_bar = int(bar_open_ms if bar_open_ms is not None
                             else views["5m"][-1].open_ms)
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
    def latest_closed_5m(self, now_ms: int) -> int:
        """当前**已收盘**的最后一根 5m K 线 open_ms；没有则 0。

        绝不用 ``feed.latest()``：那根还在形成，open_ms 会被误当成决策时刻。
        """
        bars = []
        for b in self.feed.series("5m", 10):
            try:
                bars.append(Candle(int(b["time"]) * 1000, D(str(b["open"])), D(str(b["high"])),
                                   D(str(b["low"])), D(str(b["close"])), D(str(b["volume"])),
                                   TF_5M_MS))
            except (ValueError, ArithmeticError):
                continue
        closed = closed_only(bars, now_ms - self.config.settle_ms)
        return closed[-1].open_ms if closed else 0

    def step_once(self, now_ms: int) -> dict | None:
        """一次轮询：先 tick 风控，再判断是否有**新的已收盘** 5m K 线。

        返回本轮决策结果；没到新 K 线返回 None。抽出来是为了让测试能推模拟时钟，
        而不是靠 ``time.sleep`` 撞运气。
        """
        try:
            self.on_tick(self.price(), now_ms)
            bar_open = self.latest_closed_5m(now_ms)
            if bar_open > self._last_bar:
                # as_of 用**当前时刻**，不是那根 K 线的 open_ms；
                # 否则 analyze 的 settle 会把这根刚收盘的 K 线排除掉。
                return self.on_5m_close(now_ms, bar_open_ms=bar_open)
        except Exception as exc:  # noqa: BLE001 - 单轮失败不该杀死引擎
            self.errors.append(f"{type(exc).__name__}: {exc}")
        return None

    def run(self, minutes: float = 30.0, poll_s: float = 1.0) -> None:
        deadline = time.time() + minutes * 60
        while time.time() < deadline and not self._stop.is_set():
            self.step_once(int(time.time() * 1000))
            time.sleep(poll_s)

    def stop(self) -> None:
        self._stop.set()