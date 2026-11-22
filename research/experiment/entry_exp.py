"""实验 E：AI 入场能力研究（与冻结的实验 M 完全隔离）。

三组候选，共用同一行情时间窗 / 费用 / 风险预算 / 撮合 / 固定退出规则：
  A  legacy   : 旧版 DeepSeek（MarketSnapshot.summary，核心特征来自 1h）
  B  multitf  : DeepSeekMultiTF（EntrySnapshot，5m/15m/1h/4h + 成本门槛）
  C  rules    : 确定性多周期规则（无模型）

铁律：
  - 决策只用 bar[0..i] 的**已收盘**数据；成交在 bar[i+1] 开盘（无前视，见 ledger）；
  - 记录**全部** LONG/SHORT/HOLD 决策，HOLD 的机会成本单独计；
  - 退出规则固定（ATR 止损止盈 + 最大持有），三组完全一致，不因组而异；
  - 推理成本按实际 token 计费，未接入 token 计数时按保守估算并标记；
  - 严格时间切分：研究集定参数，样本外集只评估一次，不调参。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from typing import Callable

from research.decision.contracts import Decision, MarketSnapshot
from research.decision.entry import build_entry_snapshot
from research.market.contracts import Candle
from research.market.state import analyze

from .ledger import BPS
from .paired import aggregate
from .sim import simulate_hold

TFS = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class ExpConfig:
    """实验 E 的冻结参数（研究集确定后不再改）。"""
    fee_bps: D = D("4")
    slippage_bps: D = D("5")
    notional: D = D("100")
    stop_atr: D = D("1.5")
    tp_atr: D = D("3")
    max_hold_bars: int = 72
    min_net_bps: D = D("10")
    settle_ms: int = 30_000
    #: 预热：1h 的 ema200 需要 205 根 1h = 2460 根 5m。
    #: 不预热会让旧版 A 组的 trend 永远 UNKNOWN（数据不足，不是判断力差），对照失效。
    warmup_bars: int = 2500
    # 推理成本：$ / 1K tokens（DeepSeek-chat 保守估算）；有真实 usage 时覆盖
    usd_per_1k_tokens: D = D("0.001")
    usd_per_call_est: D = D("0.0003")

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, D) else v) for k, v in self.__dict__.items()}


@dataclass
class EntryDecisionLog:
    bar: int
    as_of_ms: int
    group: str
    choice: str
    status: str
    confidence: str
    rationale: str
    provider: str
    input_hash: str
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: str = "0"
    trade: dict | None = None

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "confidence"}
        d["confidence"] = str(self.confidence)
        return d


@dataclass
class MultiTFState:
    """在某个决策时刻，用 bar[0..i] 构造的 analyze() 状态。"""
    market: MarketSnapshot
    views: dict[str, list[Candle]]     # 每个周期已收盘的 K 线列表
    last_close: D
    atr_abs: D                          # 5m Wilder ATR14 绝对值（自算，不依赖 state.features）


def _closed_upto(bars: list[Candle], i: int, minutes: int) -> list[Candle]:
    """bar[0..i] 聚合成目标周期后，只保留已收盘（close_ms <= bars[i].close_ms）。"""
    step = minutes * 60_000
    buckets: dict[int, list[Candle]] = {}
    for c in bars[:i + 1]:
        buckets.setdefault(int(c.open_ms // step) * step, []).append(c)
    cutoff = int(bars[i].close_ms)
    out = []
    for k in sorted(buckets):
        rows = buckets[k]
        cm = k + step
        if cm > cutoff:            # 当前周期尚未收盘，排除
            continue
        out.append(Candle(k, rows[0].open,
                          max(x.high for x in rows), min(x.low for x in rows),
                          rows[-1].close, sum((x.volume for x in rows), D("0")), step))
    return out


def _atr14(candles: list[Candle], period: int = 14) -> D:
    """Wilder ATR；数据不足返回 0。"""
    if len(candles) < period + 1:
        return D("0")
    trs = []
    for k in range(1, len(candles)):
        c, prev = candles[k], candles[k - 1]
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    atr = sum(trs[:period], D("0")) / D(period)
    for tr in trs[period:]:
        atr = (atr * D(period - 1) + tr) / D(period)
    return atr


def make_state(bars: list[Candle], i: int, symbol: str = "BTC/USDT") -> MultiTFState | None:
    # 5m=5, 15m=15, 1h=60, 4h=240（分钟）
    minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
    views_map = {tf: _closed_upto(bars, i, minutes[tf]) for tf in TFS}
    if not views_map.get("5m") or not views_map.get("1h"):
        return None
    as_of = int(bars[i].close_ms)
    st = analyze(symbol, views_map, as_of, settle_ms=0)
    snap = MarketSnapshot(as_of_ms=as_of, symbol=symbol, state=st, intel=None)
    return MultiTFState(market=snap, views=views_map, last_close=bars[i].close,
                        atr_abs=_atr14(views_map["5m"]))


def _rules_multitf(snap, state: MultiTFState) -> Decision:
    """确定性多周期规则（C 组）：多周期趋势一致 + 成本门槛，否则 HOLD。"""
    tf = state.views
    def trend(view_list):
        if len(view_list) < 21:
            return "UNKNOWN"
        closes = [c.close for c in view_list[-21:]]
        e = closes[0]
        for c in closes[1:]:
            e = c * D("2") / D("21") + e * D("19") / D("21")
        return "TREND_UP" if closes[-1] > e else ("TREND_DOWN" if closes[-1] < e else "FLAT")

    t5, t15, t1h = trend(tf["5m"]), trend(tf["15m"]), trend(tf["1h"])
    # C 组是"无 AI 的确定性基线"：只需覆盖双边交易成本 roundtrip，
    # 不叠加模型的 min_net_edge（那是模型自己的风险偏好，不该强加给规则组）。
    # 门槛在三组间对"成本"部分保持一致，均为 roundtrip_bps。
    need = D(str(snap.costs.get("roundtrip_bps", "10")))
    atr = state.atr_abs
    close5 = tf["5m"][-1].close
    atr_bps = (atr / close5 * BPS) if atr > 0 else D("0")
    if t5 == t15 == t1h == "TREND_UP" and atr_bps >= need:
        return Decision("rules_multitf", "v1", "OK", "LONG", 1.0, D("1"),
                        f"三周期多头共振，ATR {atr_bps:.1f}bps >= 门槛 {need}bps")
    if t5 == t15 == t1h == "TREND_DOWN" and atr_bps >= need:
        return Decision("rules_multitf", "v1", "OK", "SHORT", -1.0, D("1"),
                        f"三周期空头共振，ATR {atr_bps:.1f}bps >= 门槛 {need}bps")
    return Decision("rules_multitf", "v1", "OK", "HOLD", 0.0, D("0"),
                    f"无共振 t5={t5} t15={t15} t1h={t1h} 或 ATR {atr_bps:.1f}bps < {need}bps")


def _cost(tokens_in: int, tokens_out: int, cfg: ExpConfig) -> D:
    if tokens_in or tokens_out:
        return (D(tokens_in + tokens_out) / D("1000")) * cfg.usd_per_1k_tokens
    return cfg.usd_per_call_est


def _call_model(provider, snap, cfg: ExpConfig, group: str, bar: int, i_hash: str) -> EntryDecisionLog:
    t0 = time.time()
    try:
        dec = provider.decide(snap)
        latency = int((time.time() - t0) * 1000)
        # 从 provider 抓取真实 token usage（若实现）
        tin = int(getattr(provider, "last_tokens_in", 0) or 0)
        tout = int(getattr(provider, "last_tokens_out", 0) or 0)
        return EntryDecisionLog(bar=bar, as_of_ms=snap.as_of_ms, group=group,
                                choice=dec.choice, status=dec.status,
                                confidence=dec.confidence, rationale=dec.rationale,
                                provider=dec.provider, input_hash=i_hash, latency_ms=latency,
                                tokens_in=tin, tokens_out=tout,
                                cost_usd=str(_cost(tin, tout, cfg)))
    except Exception as exc:  # noqa: BLE001 - 模型异常=HOLD，不中断实验
        return EntryDecisionLog(bar=bar, as_of_ms=snap.as_of_ms, group=group,
                                choice="HOLD", status="EXCEPTION",
                                confidence=D("0"), rationale=f"{type(exc).__name__}: {exc}"[:200],
                                provider=getattr(provider, "name", group), input_hash=i_hash,
                                cost_usd=str(cfg.usd_per_call_est))


def _trade_for(dec: Decision, state: MultiTFState, bar_i: int, bars: list[Candle],
               cfg: ExpConfig) -> dict | None:
    if not dec.actionable:
        return None
    atr = state.atr_abs
    if atr <= 0:
        return None
    entry_ref = bars[bar_i].close
    sign = D("1") if dec.choice == "LONG" else D("-1")
    stop = entry_ref - sign * cfg.stop_atr * atr
    tp = entry_ref + sign * cfg.tp_atr * atr
    r = simulate_hold(bars, bar_i, dec.choice, cfg.notional, stop, tp,
                      fee_bps=cfg.fee_bps, slippage_bps=cfg.slippage_bps,
                      max_hold_bars=cfg.max_hold_bars)
    return r.to_dict() if r else None


def run_experiment(bars: list[Candle], *, cfg: ExpConfig, providers: dict[str, object],
                   research_end_ms: int, max_bars: int | None = None,
                   full_bars: list[Candle] | None = None,
                   filter_provider: object | None = None,
                   decision_every: int = 6) -> dict:
    """逐 bar 驱动三组，返回全部决策、成交与成本。research/样本外按时间切。"""
    decisions: list[EntryDecisionLog] = []
    trades: list[dict] = []
    # 多周期视图用 full_bars（完整历史，保证 1h/4h 有足够已收盘 K 线）；
    # 决策点 i 只在 bars（本次实验窗口）内；成交用 full_bars[i+1]（同一条时间线）。
    hist = full_bars if full_bars is not None else bars
    n = len(bars) if max_bars is None else min(len(bars), max_bars)
    start = max(20, int(cfg.warmup_bars))
    if start >= n:
        raise ValueError(
            f"可用 K 线 {n} 根不足以覆盖预热 {start} 根；"
            f"1h ema200 需要 ~205 根 1h（≈2460 根 5m），请给更多历史")
    for i in range(start, n, max(1, decision_every)):
        if i + 1 >= len(hist):     # 需要下一根可交易 bar
            break
        state = make_state(hist, i)
        if state is None:
            continue
        entry_snap = build_entry_snapshot(
            state=state.market.state, as_of_ms=state.market.as_of_ms, digest=None,
            fee_bps=cfg.fee_bps, slippage_bps=cfg.slippage_bps,
            spread_bps=D("0"), min_net_bps=cfg.min_net_bps,
            data_quality={"gaps": 0})
        i_hash = hashlib.sha256(json.dumps(entry_snap.summary(), ensure_ascii=False,
                                           sort_keys=True).encode()).hexdigest()[:16]
        for group in ("A", "B", "C"):
            provider = providers.get(group)
            if provider is None:
                continue
            snap = state.market if group == "A" else entry_snap
            if group == "C":
                dec = provider(entry_snap, state)
                log = EntryDecisionLog(bar=i, as_of_ms=state.market.as_of_ms, group=group,
                                       choice=dec.choice, status=dec.status,
                                       confidence=dec.confidence, rationale=dec.rationale,
                                       provider=dec.provider, input_hash=i_hash)
            else:
                log = _call_model(provider, snap, cfg, group, i, i_hash)
            trade = _trade_for(dec, state, i, hist, cfg) if group == "C" else _trade_for(
                _dec_from_log(log), state, i, hist, cfg)
            log.trade = trade
            split = "research" if state.market.as_of_ms <= research_end_ms else "oos"
            rec = log.to_dict()
            rec["split"] = split
            decisions.append(rec)
            if trade:
                t = dict(trade)
                t.update({"group": group, "split": split, "bar": i,
                          "decision": log.choice, "input_hash": i_hash})
                trades.append(t)

        # ---- D 组：规则信号 + AI 过滤（P1b）----
        # 只有规则 C 在本 bar 给出非 HOLD 时才调用过滤器（严格配对：同一批候选信号）
        c_dec = decisions[-1]["choice"] if decisions and decisions[-1]["group"] == "C" else None
        if c_dec and c_dec != "HOLD" and filter_provider is not None:
            f = filter_provider.decide(entry_snap, c_dec)
            accept = bool(f.get("accept", True))
            status = f.get("status", "?")
            tin, tout = int(f.get("tokens_in") or 0), int(f.get("tokens_out") or 0)
            cost = _cost(tin, tout, cfg)
            dlog = EntryDecisionLog(bar=i, as_of_ms=state.market.as_of_ms, group="D",
                                    choice=c_dec if accept else "HOLD", status=status,
                                    confidence=D("0"), rationale=f.get("reason", ""),
                                    provider=f.get("provider", "filter"), input_hash=i_hash,
                                    tokens_in=tin, tokens_out=tout, cost_usd=str(cost))
            if accept:
                c_dec_log = Decision("rules_multitf", "v1", "OK", c_dec,
                                     0.0, D("1"), "filtered-candidate")
                dtrade = _trade_for(c_dec_log, state, i, hist, cfg)
            else:
                dtrade = None
            dlog.trade = dtrade
            split = "research" if state.market.as_of_ms <= research_end_ms else "oos"
            rec = dlog.to_dict(); rec["split"] = split
            decisions.append(rec)
            if dtrade:
                t = dict(dtrade)
                t.update({"group": "D", "split": split, "bar": i,
                          "decision": c_dec, "input_hash": i_hash})
                trades.append(t)
    return {"config": cfg.to_dict(), "decisions": decisions, "trades": trades,
            "research_end_ms": research_end_ms, "bars_used": n,
            "decision_every": decision_every, "warmup_bars": cfg.warmup_bars}


def _dec_from_log(log: EntryDecisionLog) -> Decision:
    return Decision(log.provider, "v1", log.status, log.choice, 0.0, D(log.confidence),
                    log.rationale)


__all__ = ["ExpConfig", "EntryDecisionLog", "make_state", "run_experiment",
           "TFS"]
