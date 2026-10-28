"""Run the candidates and their controls, and say which is which.

Every strategy is measured against a control that shares its instruments, its
window and its exit rules but not its signal: entries land on random bars.
A strategy that cannot beat its own control is not a strategy, it is a
coincidence with a thesis. The control is drawn with a fixed seed so a rerun
reproduces it exactly.

Each run prints both the worst-case and best-case bar resolution, because a
strategy that only works when the bar resolves in its favour is telling you
something. Nothing here tunes anything: thresholds are the ports' defaults,
and the ports carry their own published numbers for comparison.
"""
from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import sentiment
from .binance import Market
from .pathsim import Exit, simulate
from .strategies import CascadeFader, FundingRateEnhanced, TrendRider
from .strategies.base import rsi

SEED = 20261028
CONTROL_ENTRIES = 60          # random entries per control run, per pair


@dataclass
class Book:
    """One strategy's result across all its pairs."""

    label: str
    source: str
    fills: list = field(default_factory=list)
    control: list = field(default_factory=list)
    open_signals: int = 0
    pairs: int = 0
    notes: list = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.fills)

    def net(self, trades: list | None = None) -> list[float]:
        return [f.net_pct for f in (trades if trades is not None else self.fills)]

    def summary(self) -> dict:
        net = self.net()
        if not net:
            return {"n": 0}
        wins = [x for x in net if x > 0]
        losses = [x for x in net if x <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        ctrl = self.net(self.control)
        return {
            "n": len(net),
            "win_rate": len(wins) / len(net),
            "net_mean": statistics.mean(net),
            "net_median": statistics.median(net),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "best": max(net),
            "worst": min(net),
            "ex_best": sum(net) - max(net),
            "control_mean": statistics.mean(ctrl) if ctrl else None,
            "control_n": len(ctrl),
        }


def _random_entries(candles: list[dict], exits: dict[int, str], n: int, side: str = "long") -> dict[int, str]:
    """Same instruments, same window, same exits -- no signal."""
    rng = random.Random(SEED)
    lo, hi = 250, len(candles) - 50
    if hi <= lo:
        return {}
    picks = rng.sample(range(lo, hi), min(n, hi - lo))
    return {p: side for p in sorted(picks)}


def run(market: Market, strategy, *, since_ms: int, worst_case: bool = True,
        control: bool = True, log=print) -> Book:
    book = Book(label=strategy.name, source=strategy.source, pairs=len(strategy.pairs))
    exit_rule = strategy.exit_rule
    venue = getattr(strategy, "venue", "spot")

    # Regime gates are real dependencies, not decoration: TrendRider filters on
    # BTC's own RSI and the Fear & Greed index. If they cannot be fetched the
    # run says so instead of quietly defaulting the gate to "pass".
    if strategy.name == "TrendRider":
        btc = market.klines("BTC/USDT:USDT", "1h", since_ms, venue="futures")
        if len(btc) >= 250:
            strategy.btc_rsi = rsi(pd.Series([c["c"] for c in btc]), 14)
        else:
            book.notes.append("BTC 1h 数据不足，regime 门（btc_rsi>35）未启用")
        fng = sentiment.fetch(Path(market.path).parent / "fear_greed.sqlite")
        strategy.fng = fng
        if not fng:
            book.notes.append("恐惧贪婪指数不可达，情绪门（25<=fng<=85）未启用")

    for pair in strategy.pairs:
        candles = market.klines(pair, strategy.timeframe, since_ms, venue=venue)
        if len(candles) < 300:
            book.notes.append(f"{pair}: 数据不足（{len(candles)} 根），跳过")
            continue
        funding = None
        if strategy.name == "FundingRateEnhanced":
            funding = market.funding(pair, since_ms)
        try:
            signals = strategy.signals(candles, funding=funding)
            exits = strategy.exit_signals(candles, funding=funding) \
                if hasattr(strategy, "exit_signals") else {}
        except Exception as exc:  # noqa: BLE001 - one bad pair must not kill the run
            book.notes.append(f"{pair}: 信号计算失败 {type(exc).__name__}: {exc}")
            continue
        rule = Exit(**{**exit_rule.__dict__, "exit_signals": exits})
        res = simulate(candles, signals, rule, symbol=pair, worst_case=worst_case,
                       funding_series=funding)
        book.fills.extend(res.fills)
        book.open_signals += res.open_signals
        if control:
            ctrl_rule = Exit(stop_loss=exit_rule.stop_loss,
                             take_profit=exit_rule.take_profit,
                             trailing=exit_rule.trailing,
                             trailing_offset=exit_rule.trailing_offset,
                             timeout_bars=exit_rule.timeout_bars,
                             exit_signals=exits)
            cres = simulate(candles, _random_entries(candles, exits, CONTROL_ENTRIES),
                            ctrl_rule, symbol=pair, worst_case=worst_case,
                            funding_series=funding)
            book.control.extend(cres.fills)
        log(f"  {pair:<14} 信号 {len(signals):>3} → 成交 {len(res.fills):>3} 笔")
    return book


def format_book(book: Book, worst_case: bool) -> str:
    s = book.summary()
    if not s.get("n"):
        return f"{book.label}: 无成交\n" + ("\n".join(f"  ! {n}" for n in book.notes) if book.notes else "")
    ctrl = f"{s['control_mean']*100:+.3f}%" if s["control_mean"] is not None else "—"
    lines = [
        f"{book.label}（{'最坏' if worst_case else '最好'} bar 解析）",
        f"  成交 {s['n']} 笔 · 胜率 {s['win_rate']*100:.0f}% · 笔均净 {s['net_mean']*100:+.3f}%"
        f" · 中位 {s['net_median']*100:+.3f}% · PF {s['profit_factor']:.2f}",
        f"  最好 {s['best']*100:+.2f}% · 最差 {s['worst']*100:+.2f}%"
        f" · 去掉最好一笔 {s['ex_best']*100:+.2f}%",
        f"  对照组（随机入场 {s['control_n']} 笔）笔均 {ctrl}"
        + (f" → 策略 {s['net_mean']*100 - s['control_mean']*100:+.3f}%"
           if s["control_mean"] is not None else ""),
    ]
    for n in book.notes:
        lines.append(f"  ! {n}")
    return "\n".join(lines)


def default_candidates() -> list:
    return [CascadeFader(), TrendRider(), FundingRateEnhanced()]
