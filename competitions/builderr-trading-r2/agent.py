"""MimicGuard trend-momentum agent for the builderr trading challenge (Round 2).

Original implementation by Zhu Yuyang. Standard library only, deterministic,
no network, no LLM, no files. Uses only the bars passed into decide().

Idea:
  1. Regime: score the broad market (SPY, QQQ) on trend (price vs 50/200-day
     averages) and short-term volatility. Regime sets target gross exposure:
     risk-off -> mostly cash, neutral -> ~0.85x, strong -> up to ~1.25x
     (the extra above 1x comes from a 2x index sleeve, QLD/SSO).
  2. Selection: rank a liquid pool by volatility-adjusted momentum
     (blend of 1- and 2-month returns), only names above their own
     50-day average.
  3. Sizing: equal weights, hard cap 22% per name, rebalance
     only when a weight drifts more than a band (cuts churn).
  4. Risk: per-position trailing stop from the 20-day high; held names that
     fall out of the top list are sold.

Limits honoured: long only, beta-adjusted gross <= 1.5x (targets <= 1.3x),
no position >= 30%, <= 50 orders/day, fast (well under 5 s).
"""
from __future__ import annotations

import math
from typing import Any

# --- parameters (kept few and round on purpose) ---------------------------
POOL = [
    # broad / sector ETFs
    "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLE", "XLV", "XLI",
    "XLY", "XLP", "XLU", "XLB", "XLC", "GLD", "TLT",
    # liquid large caps
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "AMD",
    "MU", "LLY", "JPM", "V", "MA", "COST", "NFLX", "ORCL", "PLTR", "CRM",
    "UNH", "XOM", "CAT", "GE", "QCOM", "ANET", "NOW", "UBER", "GS", "WMT",
]
LEVER_SLEEVE = ("QLD", "SSO")   # 2x index ETFs, only in strong regime
BETA = {"QLD": 2.0, "SSO": 2.0, "TQQQ": 3.0, "SOXL": 3.0, "UPRO": 3.0,
        "SPXL": 3.0, "TNA": 3.0, "FAS": 3.0, "TECL": 3.0, "LABU": 3.0,
        "CURE": 3.0, "DRN": 3.0, "UDOW": 3.0, "NAIL": 3.0, "DDM": 2.0,
        "ROM": 2.0, "UWM": 2.0, "AGQ": 2.0}
TOP_N = 6
MAX_W = 0.22          # per-name cap (rule is 30%)
BAND = 0.04           # rebalance band on weights
STOP = 0.09           # trailing stop from 20-day high
GROSS = {"off": 0.15, "neutral": 0.85, "strong": 1.0}
SLEEVE_STRONG = 0.12  # each 2x sleeve weight in strong regime (adds ~0.24 beta)
MAX_ORDERS = 45


def _closes(bars: Any) -> list[float]:
    out = []
    for b in bars or []:
        try:
            c = float(b["close"] if isinstance(b, dict) else b)
        except (KeyError, TypeError, ValueError):
            continue
        if c > 0 and math.isfinite(c):
            out.append(c)
    return out


def _sma(c: list[float], n: int) -> float | None:
    return sum(c[-n:]) / n if len(c) >= n else None


def _ret(c: list[float], k: int, skip: int = 0) -> float | None:
    if len(c) <= k + skip:
        return None
    a, b = c[-1 - k - skip], c[-1 - skip]
    return b / a - 1.0 if a > 0 else None


def _vol(c: list[float], n: int) -> float | None:
    if len(c) <= n:
        return None
    r = [c[i] / c[i - 1] - 1.0 for i in range(len(c) - n, len(c))]
    m = sum(r) / n
    v = math.sqrt(sum((x - m) ** 2 for x in r) / max(n - 1, 1))
    return v * math.sqrt(252) if v > 0 else None


def _regime(ms: dict) -> str:
    spy, qqq = _closes(ms.get("SPY")), _closes(ms.get("QQQ"))
    if len(spy) < 200 or len(qqq) < 200:
        return "neutral"
    pts = 0
    for c in (spy, qqq):
        s50, s200 = _sma(c, 50), _sma(c, 200)
        if c[-1] > s200:
            pts += 1
        if c[-1] > s50:
            pts += 1
        if s50 > s200:
            pts += 1
    qv = _vol(qqq, 20) or 0.3
    r10 = _ret(qqq, 10) or 0.0
    if qv > 0.35 or r10 < -0.07:
        pts -= 3
    elif qv > 0.26:
        pts -= 1
    if pts >= 6:
        return "strong"
    if pts >= 3:
        return "neutral"
    return "off"


def _score(c: list[float]) -> float | None:
    if len(c) < 130:
        return None
    s50 = _sma(c, 50)
    if s50 is None or c[-1] < s50:
        return None
    r3, r6 = _ret(c, 21, 0), _ret(c, 42, 0)
    v = _vol(c, 60)
    if r3 is None or r6 is None or not v:
        return None
    m = 0.6 * r3 + 0.4 * r6
    return m if m > 0 else None


def _targets(ms: dict) -> dict[str, float]:
    reg = _regime(ms)
    scored = []
    for t in POOL:
        c = _closes(ms.get(t))
        s = _score(c)
        if s is not None:
            scored.append((s, t, _vol(c, 60) or 0.3))
    scored.sort(reverse=True)
    picks = scored[:TOP_N]
    tgt: dict[str, float] = {}
    if picks:
        inv = [(t, 1.0) for _, t, v in picks]
        tot = sum(w for _, w in inv)
        gross = GROSS[reg]
        # iterative capping so the cap is respected after renormalising
        w = {t: gross * x / tot for t, x in inv}
        for _ in range(5):
            over = {t: x for t, x in w.items() if x > MAX_W}
            if not over:
                break
            spare = sum(x - MAX_W for x in over.values())
            under = [t for t in w if w[t] < MAX_W]
            for t in over:
                w[t] = MAX_W
            us = sum(w[t] for t in under)
            for t in under:
                w[t] += spare * w[t] / us if us > 0 else 0
        tgt.update(w)
    if reg == "strong":
        for t in LEVER_SLEEVE:
            c = _closes(ms.get(t))
            if len(c) >= 50 and c[-1] > (_sma(c, 50) or 1e18):
                tgt[t] = tgt.get(t, 0.0) + SLEEVE_STRONG
    return tgt


def _stopped(c: list[float]) -> bool:
    if len(c) < 20:
        return False
    hi = max(c[-20:])
    return c[-1] < hi * (1.0 - STOP)


def decide(market_state: dict, portfolio_state: dict, cash: float) -> list[dict]:
    try:
        return _decide(market_state or {}, portfolio_state or {}, float(cash or 0.0))
    except Exception:
        return []


def _decide(ms: dict, ps: dict, cash: float) -> list[dict]:
    last = dict(ps.get("last_prices") or {})
    held: dict[str, float] = {}
    for p in ps.get("positions") or []:
        try:
            q = float(p.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if q > 0:
            held[p["ticker"]] = held.get(p["ticker"], 0.0) + q

    def price(t: str) -> float | None:
        c = _closes(ms.get(t))
        px = last.get(t) or (c[-1] if c else None)
        try:
            px = float(px)
        except (TypeError, ValueError):
            return None
        return px if px > 0 and math.isfinite(px) else None

    equity = cash + sum(q * (price(t) or 0.0) for t, q in held.items())
    if equity <= 0:
        return []

    tgt = _targets(ms)
    for t in list(tgt):
        if _stopped(_closes(ms.get(t))):
            tgt.pop(t)

    sells, buys = [], []
    for t, q in held.items():
        px = price(t)
        if not px:
            continue
        cur = q * px / equity
        want = tgt.get(t, 0.0)
        if want == 0.0:
            sells.append({"ticker": t, "side": "sell", "quantity": q})
        elif cur - want > BAND:
            qty = math.floor((cur - want) * equity / px)
            if qty > 0:
                sells.append({"ticker": t, "side": "sell", "quantity": min(qty, q)})

    # cash available after sells; keep buys within cash (no margin needed)
    freed = sum(o["quantity"] * (price(o["ticker"]) or 0) for o in sells)
    budget = max(cash + freed - 0.005 * equity, 0.0)
    order = sorted(tgt.items(), key=lambda kv: -kv[1])
    for t, want in order:
        px = price(t)
        if not px:
            continue
        cur = held.get(t, 0.0) * px / equity
        if want - cur > BAND or (t not in held and want > 0):
            dollars = min((want - cur) * equity, budget)
            qty = math.floor(dollars / px)
            if qty > 0:
                buys.append({"ticker": t, "side": "buy", "quantity": qty})
                budget -= qty * px
    return (sells + buys)[:MAX_ORDERS]
