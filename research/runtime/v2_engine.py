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
BOOKS = ("rules_5m", "deepseek_5m", "dual_5m", "deepseek_5m_manager")
TFS = (("1m", 60_000), ("5m", TF_5M_MS), ("15m", TF_15M_MS),
       ("1h", TF_1H_MS), ("4h", TF_4H_MS))


@dataclass
class V2Config:
    decision_ms: int = TF_5M_MS
    notional: Decimal = D("100")
    stop_atr: Decimal = D("1.5")
    tp_atr: Decimal = D("3")
    exit_bars: int = 2
    #: 「认错」所需的最小**毛亏**（bps，按开仓价计）。低于此值说明只是噪声，
    #: 退出的手续费比躲掉的下跌还贵——宁可不退。
    exit_min_loss_bps: Decimal = D("15")
    #: 平仓后同方向冷却的 K 线数：避免"平了立刻再开"刷手续费
    cooldown_bars: int = 2
    #: 持仓管理：off（旧行为：固定规则退场）| rules（确定性移动止损/分批）| ai（模型管理）
    position_manager: str = "off"
    #: 分批止盈比例、移动止损距离（以 R 为单位）
    partial_fraction: Decimal = D("0.5")
    trail_r: Decimal = D("1.0")
    #: 执行口径用的周期：入场价/止损止盈/提前退出的基准都取这个周期的收盘。
    #: 旧行为隐式用了 1h（state.features 来自 1h），导致同一小时内每笔的
    #: 入场价与保护价完全相同，而真实成交价差出几十美元。
    exec_tf: str = "5m"
    max_positions: int = 1
    spread_bps: Decimal = D("5")
    #: 收盘宽限：K 线 close_ms 必须 <= as_of - settle_ms 才算已收盘
    settle_ms: int = 30_000
    #: 历史刷新周期（5 分钟）；失败不推进，按退避重试
    history_refresh_ms: int = 300_000
    #: 最新已收盘 5m K 线最多能比 as_of 旧多久；超过就不许开新仓
    #: （Binance REST 会 418/429 限流，刷新连续失败时会触发）
    max_stale_ms: int = 3 * 300_000
    #: 遇到 418/429 后的 REST 冷却窗口；遵守限流，不再反复敲门
    rest_cooldown_ms: int = 600_000
    #: 最长持有期（超时平仓）
    max_holding_ms: int = 4 * TF_1H_MS
    #: 入场模型：legacy=冻结的 1h 特征基线；multitf=多周期 EntrySnapshot 候选
    entry_mode: str = "legacy"


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
    #: (book, side) → 最近一次平仓时间；用于冷却，重启后清空（保守：重新开始计时）
    _last_close_ts: dict = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    errors: list = field(default_factory=list)
    #: 最近一轮的实际模型输入（供审计：证明 digest 真的进了模型）
    last_inputs: dict = field(default_factory=dict)
    #: 持仓管理模型（有 decide(snapshot) 即可）；None 且 position_manager=ai 时退化为 rules
    position_ai: object = None
    #: 实验 E 的多周期入场 provider（有 decide(EntrySnapshot) 即可）
    entry_provider: object = None
    #: 每本账上次看到新闻的时间，用于算 news_delta（不做假观测）
    _last_news_ms: dict = field(default_factory=dict)
    #: 持仓管理模型调用统计：区分真 AI 与回退
    position_ai_stats: dict = field(default_factory=lambda: {
        "calls": 0, "ok": 0, "error": 0, "fallback": 0})
    #: 行情数据质量计数：K 线覆盖、刷新成功率、限流、陈旧跳过
    data_stats: dict = field(default_factory=lambda: {
        "refresh_attempts": 0, "refresh_ok": 0, "rate_limited": 0,
        "rest_skipped": 0, "stale_skips": 0, "duplicate_bar_skips": 0,
        "closed_bars_seen": 0})
    _rest_cooldown_until: int = 0
    #: 每本账的配置覆盖（实验组）。空 = 所有账用 self.config。
    #: 单进程 + 一份行情 + 一次历史刷新/情报采集，避免多进程打爆 Binance REST。
    book_configs: dict = field(default_factory=dict)
    #: 对照实验模式：所有账本共用**同一个入场信号**（一次模型调用），
    #: 只在退出/持仓管理规则上分化。避免"入场不同"污染对照结论。
    experiment: bool = False

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in BOOKS:
            self.books[name] = PaperBroker(
                self.root / f"{name}.sqlite", starting_cash=D("1000"),
                funding_bps_per_8h=D("1"), shadow_mode="shadow_only")
            # 重启后重建冷却：从账本读每个方向最后一次平仓时间。
            # 没有这一步，进程一重启就能立刻反手，等于绕过冷却。
            try:
                rows = self.books[name]._db.execute(
                    "SELECT side, MAX(exit_ts) AS ts FROM positions"
                    " WHERE status='closed' AND exit_ts IS NOT NULL GROUP BY side").fetchall()
                for r in rows:
                    if r["ts"]:
                        self._last_close_ts[(name, str(r["side"]))] = int(r["ts"])
            except Exception as exc:                 # noqa: BLE001 - 旧账本没有列时跳过
                self.errors.append(f"{name}: 冷却恢复失败 {type(exc).__name__}: {exc}")

    def _cfg(self, name: str) -> "V2Config":
        """这本账的有效配置。没有覆盖就用引擎默认配置。"""
        return self.book_configs.get(name, self.config)

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
                as_of_ms: int | None = None, shared: Decision | None = None) -> Decision:
        """三个账本的决策。同一快照内 DeepSeek 只调用一次，Dual 复用结果。

        ``memo`` 由 ``on_5m_close`` 每轮新建，生命周期就是一根 5m K 线。
        """
        # 对照实验里四本账必须共用同一个入场信号；
        # 否则 A 用 rules、B/C/D 用 DeepSeek，差异来源混在一起就说不清了。
        if not self.experiment and name == "rules_5m":
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
        if shared is not None:
            # 实验 E：所有账本共用多周期候选入场信号
            return shared
        if self.experiment:
            # 所有账本同一个入场信号：差异只来自 book_configs 的退出/管理规则
            return ds
        if name in ("deepseek_5m", "deepseek_5m_manager"):
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
    def _intent(self, name: str, decision: Decision, state, as_of_ms: int,
                cfg: "V2Config | None" = None) -> TradeIntent | None:
        cfg = cfg or self.config
        close, atr = self._exec_refs(state, cfg)
        if close is None or atr is None or atr <= 0:
            return None
        side = "LONG" if decision.choice == "LONG" else "SHORT"
        sign = D("1") if side == "LONG" else D("-1")
        iid = hashlib.sha256(f"{name}|{as_of_ms}|{side}".encode()).hexdigest()[:16]
        return TradeIntent(
            iid, as_of_ms, SYMBOL, side, name, decision.version, close,
            close - sign * cfg.stop_atr * atr,
            close + sign * cfg.tp_atr * atr,
            cfg.max_holding_ms,
            decision.confidence, decision.rationale or name, shadow=True)

    def _enter(self, name: str, decision: Decision, state, as_of_ms: int,
               cfg: "V2Config | None" = None) -> dict | None:
        cfg = cfg or self.config
        broker = self.books[name]
        if len(broker.open_positions()) >= cfg.max_positions:
            return None
        # 冷却：刚平掉的方向不要下一根就照抄。
        # 用户实测那笔就是 15:31 平空、15:36 又开空，多付一次 8bps。
        side = "LONG" if decision.choice == "LONG" else "SHORT"
        last = self._last_close_ts.get((name, side))
        if last is not None and cfg.cooldown_bars > 0:
            need = cfg.cooldown_bars * cfg.decision_ms
            if as_of_ms - last < need:
                left = need - (as_of_ms - last)
                return {"blocked": "COOLDOWN",
                        "reason": f"{side} 于 {last} 平仓，冷却剩余 {left // 1000}s"
                                  f"（{cfg.cooldown_bars} 根 5m）",
                        "cooldown_left_ms": left}
        intent = self._intent(name, decision, state, as_of_ms, cfg)
        if intent is None:
            return None
        risk = evaluate(intent, state, broker.account(as_of_ms // 86_400_000 * 86_400_000),
                        self.limits, cfg.spread_bps)
        if risk.decision != "ALLOW":
            return {"risk": risk.to_dict()}
        notional = min(risk.notional, cfg.notional)
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

    def _mirror_reduce(self, name: str, pos: dict, qty) -> dict:
        """部分减仓镜像到交易所（reduceOnly 市价）。"""
        intent = self._mirrored.get(str(pos.get("intent_id") or "")) or self._intent_from_position(pos)
        try:
            res = self.executor.reduce(intent, D(str(qty)), "MGR_PARTIAL")
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"demo_reduce {pos.get('intent_id')}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not res.ok:
            self.errors.append(f"demo_reduce {intent.intent_id}: {res.error}")
        return res.to_dict()

    def _mirror_stop(self, name: str, pos: dict, new_stop: D) -> dict:
        """换止损：先挂新单再撤旧单，任何时刻都不裸仓。"""
        intent = self._mirrored.get(str(pos.get("intent_id") or "")) or self._intent_from_position(pos)
        try:
            res = self.executor.replace_stop(intent, D(str(new_stop)))
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"demo_stop {pos.get('intent_id')}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not res.ok:
            self.errors.append(f"demo_stop {intent.intent_id}: {res.error}")
        return res.to_dict()

    def _mirror_tp(self, name: str, pos: dict, new_tp: D) -> dict:
        intent = self._mirrored.get(str(pos.get("intent_id") or "")) or self._intent_from_position(pos)
        try:
            res = self.executor.replace_tp(intent, D(str(new_tp)))
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"demo_tp {pos.get('intent_id')}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not res.ok:
            self.errors.append(f"demo_tp {intent.intent_id}: {res.error}")
        return res.to_dict()

    def _intent_from_position(self, pos: dict) -> TradeIntent:
        side = str(pos["side"])
        return TradeIntent(str(pos["intent_id"]), int(pos["entry_ts"]), str(pos["symbol"]), side,
                           str(pos.get("strategy_id") or "restored"), "restored",
                           D(str(pos["entry_price"])), D(str(pos["stop_price"])),
                           D(str(pos["tp_price"])), int(pos.get("max_holding_ms") or 4 * TF_1H_MS),
                           D("0.5"), "restored from ledger", shadow=True)

    # ---------- 持仓管理 ----------
    @staticmethod
    def _tf_trend(view) -> str:
        """从已收盘指标推导趋势，不填假值、不用领域外推。"""
        if view is None:
            return ""
        e20, e60 = getattr(view, "ema20", None), getattr(view, "ema60", None)
        if e20 is None or e60 is None:
            return "UNKNOWN"
        if e20 > e60:
            return "TREND_UP"
        if e20 < e60:
            return "TREND_DOWN"
        return "FLAT"

    def _excursion(self, pos: dict, as_of_ms: int) -> tuple[D, D]:
        """从 5m 已收盘 K 线算这笔仓位的 MFE/MAE（USDT，按当前数量）。"""
        qty = D(str(pos["qty"]))
        entry = D(str(pos["entry_price"]))
        direction = D("1") if str(pos["side"]) == "LONG" else D("-1")
        mfe = mae = D("0")
        for b in self.feed.series("5m", 200):
            try:
                t = int(b["time"]) * 1000
            except (KeyError, TypeError, ValueError):
                continue
            if t < int(pos["entry_ts"]) or t > as_of_ms:
                continue
            hi, lo = D(str(b["high"])), D(str(b["low"]))
            if direction > 0:
                fav, adv = (hi - entry) * qty, (entry - lo) * qty
            else:
                fav, adv = (entry - lo) * qty, (hi - entry) * qty
            mfe, mae = max(mfe, fav), max(mae, adv)
        return mfe, mae

    def _news_delta(self, digest, as_of_ms: int, since_ms: int) -> tuple[str, ...]:
        """自上次评估以来的新标题；没有情报就返回空元组，不编造。"""
        if digest is None:
            return ()
        out = []
        for n in (getattr(digest, "news", ()) or ()):
            # NewsEvent 的真实字段是 headline / occurred_at_ms（不是 title / published_ms）
            t = int(getattr(n, "occurred_at_ms", 0) or 0)
            title = str(getattr(n, "headline", "") or "")
            if since_ms < t <= as_of_ms and title:
                out.append(title[:120])
        return tuple(out[:5])

    def _manage_position(self, name: str, state, as_of_ms: int,
                         cfg: "V2Config | None" = None, digest=None) -> dict | None:
        """把当前仓位的"处境"交给规则或模型，拿回一个有界动作并执行。"""
        cfg = cfg or self.config
        from research.decision.position import (
            PositionDecisionError, rules_position_action, snapshot_from_row,
            validate_against_position,
        )

        broker = self.books[name]
        pos = broker.open_positions()[0]
        mark = self._mark_price(state, cfg)
        if mark is None:
            return {"action": "SKIP", "reason": "没有可用价格"}
        tfs = getattr(state, "timeframes", None) or {}
        trends = {tf: self._tf_trend(tfs.get(tf)) for tf in ("5m", "15m", "1h")}
        mfe, mae = self._excursion(pos, as_of_ms)
        since = int(self._last_news_ms.get(name, 0))
        news = self._news_delta(digest, as_of_ms, since)
        if news or since == 0:
            self._last_news_ms[name] = as_of_ms
        day_start = as_of_ms // 86_400_000 * 86_400_000
        realized_today = broker.realized_pnl_since(day_start)
        risk_budget = max(D("0"), self.limits.daily_loss_limit - abs(realized_today))
        snap = snapshot_from_row(
            row=pos, mark=mark, as_of_ms=as_of_ms, fee_bps=broker.fee_bps,
            trends=trends, news_delta=news, risk_budget_left=risk_budget,
            mfe=mfe, mae=mae, data_as_of_ms=as_of_ms,
        )

        mode = cfg.position_manager
        stage = str(pos.get("mgmt_stage") or "")
        meta = {"ai_provider": None, "ai_status": "NOT_CALLED", "ai_used": False,
                "ai_fallback": False}
        def rule_dec():
            return rules_position_action(snap, trail_atr=cfg.trail_r,
                                         partial_fraction=cfg.partial_fraction,
                                         stage=stage)

        if mode == "ai" and self.position_ai is not None:
            try:
                dec = self.position_ai.decide(snap)
                self.position_ai_stats["calls"] += 1
                st = getattr(dec, "status", "OK")
                if st == "OK":
                    self.position_ai_stats["ok"] += 1
                else:
                    self.position_ai_stats["error"] += 1
                meta.update(ai_provider=getattr(dec, "provider", None),
                            ai_status=st, ai_used=True, ai_fallback=st != "OK")
            except Exception as exc:                     # noqa: BLE001
                self.errors.append(f"position_ai: {type(exc).__name__}: {exc}")
                for k in ("calls", "error", "fallback"):
                    self.position_ai_stats[k] += 1
                meta.update(ai_provider=getattr(self.position_ai, "name", "?"),
                            ai_status="EXCEPTION", ai_used=True, ai_fallback=True)
                dec = rule_dec()
        elif mode == "ai":
            # 声明 AI 但没有 provider：绝不能标成 AI 实验
            self.position_ai_stats["fallback"] += 1
            meta.update(ai_provider=None, ai_status="NO_PROVIDER", ai_fallback=True)
            dec = rule_dec()
        else:
            dec = rule_dec()
        try:
            validate_against_position(dec, snap)
        except PositionDecisionError as exc:
            self.errors.append(f"position_decision 拒绝 {dec.action}: {exc}")
            return {"action": "REJECTED", "requested": dec.action, "error": str(exc),
                    "decision": dec.to_dict(), "snapshot": snap.summary(), **meta}

        out = {"action": dec.action, "decision": dec.to_dict(), "snapshot": snap.summary(),
               "executed": False, "mgmt_stage": stage, **meta}
        if dec.action == "HOLD":
            out["executed"] = True
        elif dec.action == "CLOSE_PARTIAL":
            qty = snap.qty * D(dec.fraction)
            res = broker.close_partial(int(pos["id"]), qty, mark, as_of_ms, "MGR_PARTIAL")
            out["executed"] = bool(res)
            out["fill"] = res
            if res and "partial_done" not in stage:
                # 阶段状态写进账本：重启后仍在，第一次分批只做一次
                out["stage_set"] = broker.set_mgmt_stage(int(pos["id"]), "partial_done")
        elif dec.action == "CLOSE_ALL":
            c = broker.close_position(int(pos["id"]), mark, as_of_ms, "MGR_CLOSE")
            out["executed"] = bool(c)
            out["closed"] = c.to_dict() if c else None
            if c:
                self._last_close_ts[(name, str(pos["side"]))] = as_of_ms
                self._mirror_close(name, pos, "MGR_CLOSE")
        elif dec.action == "MOVE_STOP":
            out["executed"] = broker.move_stop(int(pos["id"]), D(dec.new_stop))
            if out["executed"] and name == self.demo_book and self.executor is not None:
                out["demo"] = self._mirror_stop(name, pos, D(dec.new_stop))
        elif dec.action == "UPDATE_TP":
            out["executed"] = broker.update_tp(int(pos["id"]), D(dec.new_tp))
            if out["executed"] and name == self.demo_book and self.executor is not None:
                out["demo"] = self._mirror_tp(name, pos, D(dec.new_tp))
        elif dec.action == "ADD_TO_WINNER":
            out["executed"] = False
            out["refused"] = "ADD_TO_WINNER 在 V3 第一轮被禁用（需单独实验）"
        return out

    # ---------- 退出 ----------
    def _early_exit(self, name: str, state, as_of_ms: int,
                    cfg: "V2Config | None" = None) -> list:
        cfg = cfg or self.config
        broker = self.books[name]
        closed = []
        for pos in broker.open_positions():
            age = as_of_ms - int(pos["entry_ts"])
            if age < cfg.exit_bars * cfg.decision_ms:
                continue
            mark = self._mark_price(state)
            if mark is None:
                continue
            direction = D("1") if pos["side"] == "LONG" else D("-1")
            entry = D(pos["entry_price"])
            pnl = (mark - entry) * D(pos["qty"]) * direction
            # 只有"亏得比手续费明显更多"才认错。否则等于花 8bps 去躲 1bps 的波动。
            gross_bps = abs(mark - entry) / entry * D("10000") if entry > 0 else D("0")
            if pnl < 0 and gross_bps >= cfg.exit_min_loss_bps:
                c = broker.close_position(int(pos["id"]), mark, as_of_ms, "EARLY_EXIT")
                if c:
                    self._last_close_ts[(name, str(pos["side"]))] = as_of_ms
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
                        self._last_close_ts[(name, str(pos["side"]))] = ts_ms
                        self._mirror_close(name, pos, reason)
                        closed.append(c)
        self._write_heartbeat(ts_ms)
        return [c for c in closed if c]

    # ---------- 5m 决策 ----------
    def _refresh_history(self, as_of_ms: int) -> dict:
        """按 5 分钟节流刷新历史；**失败不推进 _history_ts**，按 30s 退避重试。"""
        if as_of_ms < self._rest_cooldown_until:
            self.data_stats["rest_skipped"] += 1
            return {"attempted": False, "ok": None,
                    "rest_cooldown_left_ms": self._rest_cooldown_until - as_of_ms}
        due = as_of_ms - self._history_ts
        if due < self.config.history_refresh_ms:
            return {"attempted": False, "ok": None, "due_in_ms": self.config.history_refresh_ms - due}
        self.data_stats["refresh_attempts"] += 1
        ok = self.load_history(as_of_ms)
        errs = self.errors[-2:] if not ok else []
        limited = any(("418" in str(e) or "429" in str(e)) for e in errs)
        if ok:
            self.data_stats["refresh_ok"] += 1
            self._history_ts = as_of_ms
        else:
            if limited:
                self.data_stats["rate_limited"] += 1
                self._rest_cooldown_until = as_of_ms + self.config.rest_cooldown_ms
            # 只把节流点推进一个退避间隔，下一次 5m 决策会重试，而不是等满 5 分钟
            self._history_ts = as_of_ms - self.config.history_refresh_ms + 30_000
        return {"attempted": True, "ok": ok,
                "fingerprint": dict(self._history_fp),
                "errors": errs, "rate_limited": limited}

    def _exec_refs(self, state, cfg: "V2Config | None" = None) -> tuple:
        """执行口径的 (close, atr_abs)。

        默认取 ``exec_tf``（5m）的收盘与 ATR；缺失时退回 ``state.features``。
        必须和执行频率一致：5m 开仓却用 1h 收盘当入场价，等于每小时只更新一次参考价。
        """
        cfg = cfg or self.config
        tf = (getattr(state, "timeframes", None) or {}).get(cfg.exec_tf)
        if tf is not None and getattr(tf, "close", None):
            close = D(str(tf.close))
            pct = getattr(tf, "atr_pct", None)
            atr = close * D(str(pct)) / D("100") if pct is not None else None
            if atr is not None and atr > 0:
                return close, atr
        return state.features.get("close"), state.features.get("atr_abs")

    def _mark_price(self, state, cfg: "V2Config | None" = None) -> Decimal | None:
        """平仓判定用的现价：优先实时价，其次执行周期收盘，最后 features。"""
        cfg = cfg or self.config
        px = self.price()
        if px:
            return D(str(px))
        tf = (getattr(state, "timeframes", None) or {}).get(cfg.exec_tf)
        if tf is not None and getattr(tf, "close", None):
            return D(str(tf.close))
        return state.features.get("close")

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
        # 第 0 道门：历史数据里的"最新收盘"没动，就绝不能再用同一根 K 线做决策。
        # 24h 实验里有 10 轮决策只对应 4 根不同的已收盘 K 线——模型输入完全没变，
        # 连续 HOLD 是必然结果，不是策略结论。
        newest_open = int(views["5m"][-1].open_ms)
        newest_close = int(views["5m"][-1].close_ms)
        if (newest_open <= int(self._last_bar)
                and as_of_ms - newest_close > self.config.decision_ms):
            self.data_stats["duplicate_bar_skips"] += 1
            return {}
        # 第 0 道门：历史数据里的"最新收盘"没动，就绝不能再用同一根 K 线做决策。
        # 24h 实验里有 10 轮决策只对应 4 根不同的已收盘 K 线——模型输入完全没变，
        # 连续 HOLD 是必然结果，不是策略结论。
        newest_open = int(views["5m"][-1].open_ms)
        newest_close = int(views["5m"][-1].close_ms)
        if (newest_open <= int(self._last_bar)
                and as_of_ms - newest_close > self.config.decision_ms):
            self.data_stats["duplicate_bar_skips"] += 1
            return {}
        stale_ms = self._stale_ms(views, as_of_ms)
        if stale_ms > self.config.max_stale_ms:
            self.data_stats["stale_skips"] += 1
            self.errors.append(
                f"stale: 最新 5m 收盘已是 {stale_ms // 1000}s 前"
                f"（阈值 {self.config.max_stale_ms // 1000}s），本轮跳过开仓；"
                f"history_refresh={refresh.get('ok')}")
            # 必须推进 _last_bar：否则 step_once 下一秒会对**同一根 K 线**
            # 反复进入这个分支，误导"有效市场数据"统计。
            self._last_bar = int(bar_open_ms if bar_open_ms is not None
                                 else views["5m"][-1].open_ms)
            return {}
        self.data_stats["closed_bars_seen"] += len(views.get("5m") or [])
        state = analyze(SYMBOL, views, as_of_ms, settle_ms=self.config.settle_ms)
        digest, intel_info = self._collect_intel(as_of_ms)
        shared_entry = None
        entry_input = None
        if self.config.entry_mode == "multitf" and self.entry_provider is not None:
            from research.decision.entry import build_entry_snapshot

            entry_input = build_entry_snapshot(
                state=state, as_of_ms=as_of_ms, digest=digest,
                fee_bps=self.limits.fee_bps,
                slippage_bps=self.limits.slippage_bps,
                spread_bps=D(str(self.config.spread_bps)),
                data_quality=self.data_stats,
                last_news_ms=int(self._last_news_ms.get("__entry__", 0)),
                baseline={"legacy": str(self._rules_decision(state).choice)},
            )
            self._last_news_ms["__entry__"] = as_of_ms
            try:
                shared_entry = self.entry_provider.decide(entry_input)
            except Exception as exc:                 # noqa: BLE001 - 候选失败退回冻结基线
                self.errors.append(f"entry_provider: {type(exc).__name__}: {exc}")
                shared_entry = None
        # 同一根收盘 K 线：一个快照、一次 DeepSeek、一次第二模型
        memo: dict = {}
        out = {}
        for name in BOOKS:
            cfg = self._cfg(name)
            decision = self._decide(name, state, digest, memo=memo, as_of_ms=as_of_ms,
                                    shared=shared_entry)
            managed = None
            has_pos = bool(self.books[name].open_positions())
            if cfg.position_manager != "off" and has_pos:
                # 有仓位时由持仓管理决策；旧的"两根 K 线认错"同时停用，
                # 否则两个出口会互相打架（一个要拿住、一个要平掉）。
                managed = self._manage_position(name, state, as_of_ms, cfg, digest)
                entry, closed = None, []
            else:
                entry = self._enter(name, decision, state, as_of_ms, cfg) if decision.actionable else None
                closed = self._early_exit(name, state, as_of_ms, cfg)
            entry = dict(entry or {})
            entry.update({"closed": [c.to_dict() for c in closed]})
            if managed is not None:
                entry["managed"] = managed
            log = {"as_of_ms": as_of_ms, "book": name, "decision": decision.to_dict(),
                   "entry": entry or None,
                   "closed_bar_ms": int(views["5m"][-1].close_ms),
                   "trigger_bar_ms": int(bar_open_ms if bar_open_ms is not None
                                         else views["5m"][-1].open_ms),
                   "entry_model": decision.provider,
                   "entry_input_hash": (hashlib.sha256(json.dumps(
                       entry_input.summary(), ensure_ascii=False, sort_keys=True).encode()
                   ).hexdigest()[:16] if entry_input is not None else None),
                   "history_refresh": refresh,
                   "data_stats": {**dict(self.data_stats),
                                  "rest_budget": __import__(
                                      "research.market.budget", fromlist=["GLOBAL_BUDGET"]
                                  ).GLOBAL_BUDGET.snapshot()},
                   "position_ai_stats": dict(self.position_ai_stats),
                   "intel": intel_info,
                   "equity": str(self.books[name].equity(as_of_ms,
                                                         {SYMBOL: D(str(state.features.get("close") or 0))}))}
            p = self.root / f"{name}_decisions.jsonl"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(log, ensure_ascii=False, sort_keys=True) + "\n")
            if entry_input is not None and name == BOOKS[0]:
                ip = self.root / "entry_inputs.jsonl"
                with ip.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry_input.summary(), ensure_ascii=False,
                                        sort_keys=True) + "\n")
            out[name] = log
        # 审计：模型实际收到了什么（证明 digest 真的进了 snapshot，不是只存在于参数里）
        snap = memo.get("snap")
        self.last_inputs = {
            "as_of_ms": as_of_ms,
            "closed_bar_ms": int(views["5m"][-1].close_ms),
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