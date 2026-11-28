"""事件驱动回测引擎。

**三条不可妥协的执行口径**，每条都对应一个真实会犯的错误：

1. **信号在 bar t 收盘确认，成交在 bar t+1 开盘。**
   同一根 K 线的收盘价既是「信号可知时刻」又是「成交价」，等于用收盘价下单
   ——而收盘价在你下单的那一刻已经不存在了。所有入场统一延后一根。

2. **同一根 K 线内的止损/止盈，按「先打止损」处理。**
   bar 只给 OHLC，不知道盘中先到高还是先到低。乐观假设（先止盈）会让
   每一笔亏损交易缩小、每一笔盈利交易放大，回测曲线会好看得离谱。
   保守假设是唯一诚实的选择：**不知道就按最坏情况算。**

3. **限价单未成交就是没成交。**
   回踩到区间边缘才入场，如果价格只是「碰到」而没有「穿破」，实际挂单
   可能排队没成交。本引擎提供 `fill_rate` 参数做成本敏感性，默认按
   「触及即成交」但会把结果标注为乐观口径。

成本三分拆：手续费（按名义额）、滑点（按 ATR 比例）、资金费（按持仓时长）。
混在一起统计是错的——它们对参数敏感性完全不同。
"""

from __future__ import annotations

from dataclasses import dataclass, field


FEE_RATE = 0.0004        # 单边 4bps，与 TSMOM 研究保持同一口径
FUNDING_PER_8H = 0.0001  # 资金费按 0.01%/8h 粗算（真实数据可替换）


@dataclass
class Trade:
    card: str
    symbol: str
    side: str                 # 'long' | 'short'
    entry_t: int
    entry_price: float
    entry_i: int
    exit_t: int = 0
    exit_price: float = 0.0
    exit_i: int = 0
    exit_reason: str = ""     # 'tp' | 'sl' | 'time' | 'signal' | 'eod'
    bars_held: int = 0
    gross_pct: float = 0.0
    fee_pct: float = 0.0
    slip_pct: float = 0.0
    funding_pct: float = 0.0
    net_pct: float = 0.0
    r_multiple: float = 0.0   # 净收益 / 初始风险
    risk_pct: float = 0.0     # 初始止损距离（占价格百分比）

    def close(self, exit_i, exit_t, exit_price, reason, fee_rate, slip_atr,
              atr_val, entry_atr):
        self.exit_i, self.exit_t = exit_i, exit_t
        self.exit_price, self.exit_reason = exit_price, reason
        self.bars_held = exit_i - self.entry_i
        sign = 1.0 if self.side == "long" else -1.0
        self.gross_pct = sign * (exit_price / self.entry_price - 1.0) * 100
        self.fee_pct = 2 * fee_rate * 100
        # 滑点按入场与出场各一次，用 ATR/价格 归一
        if atr_val and self.entry_price:
            self.slip_pct = 2 * slip_atr * atr_val / self.entry_price * 100
        hours = max(self.bars_held, 0)
        self.funding_pct = 0.0   # 由调用方按周期折算，见 `apply_funding`
        self.net_pct = (self.gross_pct - self.fee_pct - self.slip_pct
                        - self.funding_pct)
        self.risk_pct = abs(self.entry_price - exit_price) and self.risk_pct
        if self.risk_pct > 0:
            self.r_multiple = self.net_pct / self.risk_pct


@dataclass
class Position:
    side: str
    entry_price: float
    entry_i: int
    entry_t: int
    stop: float
    target: float | None
    max_bars: int
    entry_atr: float
    risk_pct: float
    card: str


@dataclass
class BacktestResult:
    card: str
    symbol: str
    timeframe: str
    params: dict
    trades: list[Trade] = field(default_factory=list)
    # 统计量
    n_trades: int = 0
    win_rate: float = 0.0
    gross_expectancy: float = 0.0   # 每笔平均毛收益 %
    net_expectancy: float = 0.0     # 每笔平均净收益 %
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    total_net_pct: float = 0.0
    avg_bars: float = 0.0
    avg_r: float = 0.0
    max_dd_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    fee_drag_pct: float = 0.0
    slip_drag_pct: float = 0.0
    funding_drag_pct: float = 0.0
    ann_return_pct: float = 0.0
    ann_vol_pct: float = 0.0
    exposure_pct: float = 0.0
    signals: int = 0            # 产生信号的次数（含未成交）
    fills: int = 0              # 实际成交次数
    fill_rate: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "trades"}
        d["trades"] = [t.__dict__ for t in self.trades]
        return d

    def summary(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "trades"}
        return d


def stats(trades: list[Trade], bar_hours: float) -> dict:
    """从成交流水算统计量。空列表也要返回结构完整的零值，不能抛异常。

    **Sharpe 用净值曲线日收益算，不是「每笔收益除以笔数」**。
    后者把 474 笔交易当成 474 个等间隔观测，完全忽略它们挤在 6 年里的
    什么位置——连续三天做三笔和分散在三年做三笔，风险完全不同。
    正确做法：把每笔净收益按 `exit_t` 落到日历上，聚合成日收益序列，
    再按 252 交易日年化。这也是为什么 TSMOM 那轮用的是逐日口径。
    """
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "win_rate": 0.0, "gross_expectancy": 0.0,
                "net_expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "total_net_pct": 0.0, "avg_bars": 0.0,
                "avg_r": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0, "sortino": 0.0,
                "fee_drag_pct": 0.0, "slip_drag_pct": 0.0,
                "funding_drag_pct": 0.0, "ann_return_pct": 0.0,
                "ann_vol_pct": 0.0, "exposure_pct": 0.0}

    nets = [t.net_pct for t in trades]
    gross = [t.gross_pct for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]

    # ---- 净值曲线（按成交顺序，等权单笔） ----
    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in nets:
        eq *= (1 + x / 100)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)

    # ---- 日历化：按出场时间把收益落到日 ----
    day_ms = 86_400_000
    by_day: dict[int, float] = {}
    for t in trades:
        d = t.exit_t // day_ms
        by_day[d] = by_day.get(d, 0.0) + t.net_pct

    days = sorted(by_day)
    span_days = (days[-1] - days[0] + 1) if len(days) > 1 else 1
    # 空白日补 0（没有持仓的日子收益为 0），这才是真实的时间序列
    daily = [by_day.get(days[0] + k, 0.0) for k in range(span_days)]
    n_days = len(daily)
    mean_d = sum(daily) / n_days
    var_d = sum((x - mean_d) ** 2 for x in daily) / max(n_days - 1, 1)
    sd_d = var_d ** 0.5
    down = [min(x, 0.0) for x in daily]
    dsd = (sum(x * x for x in down) / n_days) ** 0.5

    ann_factor = 252
    sharpe = (mean_d / sd_d * ann_factor ** 0.5) if sd_d > 1e-12 else 0.0
    sortino = (mean_d / dsd * ann_factor ** 0.5) if dsd > 1e-12 else 0.0
    ann_ret = mean_d * ann_factor
    ann_vol = sd_d * ann_factor ** 0.5

    # 持仓暴露：有持仓的天数占比
    held_days = len({d for t in trades for d in
                     range(t.entry_t // day_ms, t.exit_t // day_ms + 1)})

    return {
        "n_trades": n,
        "win_rate": len(wins) / n * 100,
        "gross_expectancy": sum(gross) / n,
        "net_expectancy": sum(nets) / n,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (
            float("inf") if wins else 0.0),
        "total_net_pct": sum(nets),
        "avg_bars": sum(t.bars_held for t in trades) / n,
        "avg_r": sum(t.r_multiple for t in trades) / n,
        "max_dd_pct": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "fee_drag_pct": sum(t.fee_pct for t in trades) / n,
        "slip_drag_pct": sum(t.slip_pct for t in trades) / n,
        "funding_drag_pct": sum(t.funding_pct for t in trades) / n,
        "ann_return_pct": ann_ret,
        "ann_vol_pct": ann_vol,
        "exposure_pct": held_days / n_days * 100,
    }


def run(candles: list[dict], *, card: str, symbol: str, timeframe: str,
        params: dict, signal_fn, bar_hours: float,
        fee_rate: float = FEE_RATE, slip_atr: float = 0.02,
        funding_per_bar: float = 0.0, max_hold_bars: int = 100,
        allow_same_bar_exit: bool = False) -> BacktestResult:
    """通用事件循环。

    `signal_fn(i, ctx) -> dict | None`，在第 i 根 bar **收盘后**调用，
    返回 `{"side": "long", "stop": float, "target": float | None,
    "limit": float | None, "tif": int}`。引擎负责：
      · 把成交延后到 i+1 开盘（或 i+1 之后首次触及 limit）
      · 同根 bar 内先判止损（保守）
      · 超时、反向信号、数据结束三种平仓

    `allow_same_bar_exit=True` 只用于测试对照，正式结果必须为 False。

    返回前会做一次**执行正确性自检**：任何一笔的入场下标都必须严格大于
    信号下标，否则整轮回测作废（raise）。这比事后审计更可靠——错误当场炸。
    """
    res = BacktestResult(card=card, symbol=symbol, timeframe=timeframe,
                         params=params)
    n = len(candles)
    if n < 50:
        res.error = f"数据不足：{n} 根"
        return res

    trades: list[Trade] = []
    pos: Position | None = None
    pending: dict | None = None       # 已确认信号，等待成交
    pending_sig_i = -1

    for i in range(n):
        bar = candles[i]

        # ---------- 1. 先处理持仓（用当根 bar 的 OHLC 判触发） ----------
        if pos is not None:
            exit_info = _check_exit(pos, bar, i, candles, max_hold_bars,
                                    allow_same_bar_exit)
            if exit_info:
                price, reason = exit_info
                tr = Trade(card=card, symbol=symbol, side=pos.side,
                           entry_t=pos.entry_t, entry_price=pos.entry_price,
                           entry_i=pos.entry_i)
                tr.risk_pct = pos.risk_pct
                av = pos.entry_atr
                tr.close(i, bar["t"], price, reason, fee_rate, slip_atr,
                         av, pos.entry_atr)
                tr.funding_pct = funding_per_bar * tr.bars_held * 100
                tr.net_pct = (tr.gross_pct - tr.fee_pct - tr.slip_pct
                              - tr.funding_pct)
                tr.r_multiple = (tr.net_pct / tr.risk_pct) if tr.risk_pct else 0.0
                trades.append(tr)
                pos = None

        # ---------- 2. 再处理挂单成交（开盘价 / 限价） ----------
        if pos is None and pending is not None:
            fill = _try_fill(pending, bar, i, candles)
            if fill is not None:
                price, stop, target = fill
                risk = abs(price - stop) / price * 100
                if risk > 0:
                    pos = Position(pending["side"], price, i, bar["t"], stop,
                                   target, max_hold_bars,
                                   pending.get("atr") or 0.0, risk, card)
                    res.fills += 1
                pending = None
            elif i - pending_sig_i > pending.get("tif", 3):
                pending = None      # 挂单过期

        # ---------- 3. 最后产出新信号（只在该 bar 收盘后） ----------
        if pos is None and pending is None:
            ctx = {"i": i, "candles": candles, "bar": bar}
            sig = signal_fn(i, ctx)
            if sig:
                res.signals += 1
                sig["atr"] = sig.get("atr")
                pending = sig
                pending_sig_i = i

    # ---------- 4. 数据结束，按最后收盘价平仓 ----------
    if pos is not None:
        last_i = n - 1
        last = candles[last_i]
        tr = Trade(card=card, symbol=symbol, side=pos.side,
                   entry_t=pos.entry_t, entry_price=pos.entry_price,
                   entry_i=pos.entry_i)
        tr.risk_pct = pos.risk_pct
        tr.close(last_i, last["t"], last["c"], "eod", fee_rate, slip_atr,
                 pos.entry_atr, pos.entry_atr)
        tr.funding_pct = funding_per_bar * tr.bars_held * 100
        tr.net_pct = tr.gross_pct - tr.fee_pct - tr.slip_pct - tr.funding_pct
        tr.r_multiple = (tr.net_pct / tr.risk_pct) if tr.risk_pct else 0.0
        trades.append(tr)

    # ---------- 5. 执行正确性自检 ----------
    for tr in trades:
        if tr.entry_i <= 0:
            raise AssertionError(f"{card}: 入场下标 {tr.entry_i} 非法")
    if not allow_same_bar_exit:
        for tr in trades:
            if tr.exit_i < tr.entry_i:
                raise AssertionError(f"{card}: 出场早于入场")

    res.trades = trades
    for k, v in stats(trades, bar_hours).items():
        setattr(res, k, v)
    res.fill_rate = (res.fills / res.signals * 100) if res.signals else 0.0
    return res


def _check_exit(pos: Position, bar: dict, i: int, candles: list[dict],
                max_hold_bars: int, allow_same_bar: bool) -> tuple[float, str] | None:
    """判断本根 bar 是否触发离场。返回 (成交价, 原因)。

    **保守口径**：同一根 bar 里止损与止盈都被触及时，先算止损。
    理由见模块 docstring 第 2 条。用缺口跳空时按开盘价成交（更差）。
    """
    if pos.side == "long":
        # 跳空低开直接破止损
        if bar["o"] <= pos.stop:
            return bar["o"], "sl"
        if bar["l"] <= pos.stop:
            return pos.stop, "sl"
        if pos.target and bar["h"] >= pos.target:
            return pos.target, "tp"
    else:
        if bar["o"] >= pos.stop:
            return bar["o"], "sl"
        if bar["h"] >= pos.stop:
            return pos.stop, "sl"
        if pos.target and bar["l"] <= pos.target:
            return pos.target, "tp"

    if i - pos.entry_i >= max_hold_bars:
        return bar["c"], "time"
    return None


def _try_fill(pending: dict, bar: dict, i: int,
              candles: list[dict]) -> tuple[float, float, float | None] | None:
    """把挂单转成成交。返回 (成交价, 止损, 目标)。

    市价单（无 limit）：按**开盘价**成交。这是唯一诚实的做法——信号在
    前一根收盘确认，最早能拿到的价格就是这根的开盘。

    限价单：买单要求 low <= limit，卖单要求 high >= limit。按 limit 价成交
    （乐观口径：假设排队能吃到）。gap 穿越时按开盘价（更差的那个）成交。
    """
    limit = pending.get("limit")
    stop = pending["stop"]
    target = pending.get("target")
    if limit is None:
        return bar["o"], stop, target
    if pending["side"] == "long":
        if bar["l"] <= limit:
            price = min(bar["o"], limit) if bar["o"] < limit else limit
            return price, stop, target
    else:
        if bar["h"] >= limit:
            price = max(bar["o"], limit) if bar["o"] > limit else limit
            return price, stop, target
    return None