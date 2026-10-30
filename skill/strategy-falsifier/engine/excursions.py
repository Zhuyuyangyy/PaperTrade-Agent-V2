"""Barrier reachability: are the exit levels prices the market actually visits?

A take-profit at +3% and a stop at -8% are not parameters, they are claims
about the world: "inside my hold window, price travels at least this far this
often". This module checks those claims directly on the candles, independent
of the simulator, so a strategy whose barriers are never touched shows up as a
lottery ticket instead of an edge.

Entry price convention: the next bar's open for taker fills (the signal is only
knowable at the bar close), the signal bar's close for maker limit fills. Both
are disclosed in the report; the maker convention is an approximation of the
limit price, the crossing test itself lives in pathsim.
"""
from __future__ import annotations

from .pathsim import Exit


def barrier_reach(candles: list[dict], entries: dict[int, str], rule: Exit, *,
                  fill_model: str = "taker", horizon_bars: int | None = None) -> dict:
    """For every signal, measure MFE/MAE inside its hold window.

    The hold window ends at the first of: the simulator's exit firing,
    timeout_bars, or the end of data -- the same window pathsim would use, so
    the percentages describe the trades the replay actually took.
    """
    tp, sl = rule.take_profit, rule.stop_loss
    cap = horizon_bars if horizon_bars is not None else rule.timeout_bars
    mfes, maes, reached_tp, reached_sl, neither = [], [], 0, 0, 0
    n_usable = 0

    for i, side in sorted(entries.items()):
        j = i + 1
        if j >= len(candles):
            continue
        if fill_model == "maker":
            limit = candles[i]["c"]
            if not limit or not _crossed(candles[j], limit, side):
                continue  # never filled: not a trade, not a sample
            entry = limit
        else:
            entry = candles[j]["o"]
        if not entry:
            continue
        n_usable += 1
        mfe = mae = 0.0
        hit_tp = hit_sl = False
        for k in range(j, len(candles)):
            bar = candles[k]
            if side == "long":
                mfe = max(mfe, bar["h"] / entry - 1.0)
                mae = min(mae, bar["l"] / entry - 1.0)
                if tp is not None and bar["h"] >= entry * (1 + tp):
                    hit_tp = True
                if sl is not None and bar["l"] <= entry * (1 + sl):
                    hit_sl = True
            else:
                mfe = max(mfe, 1.0 - bar["l"] / entry)
                mae = min(mae, 1.0 - bar["h"] / entry)
                if tp is not None and bar["l"] <= entry * (1 - tp):
                    hit_tp = True
                if sl is not None and bar["h"] >= entry * (1 - sl):
                    hit_sl = True
            if (hit_tp and hit_sl) or (cap is not None and k - j >= cap):
                break
        mfes.append(mfe)
        maes.append(mae)
        if hit_tp:
            reached_tp += 1
        if hit_sl:
            reached_sl += 1
        if not hit_tp and not hit_sl:
            neither += 1

    if not n_usable:
        return {"n": 0, "note": "没有可用的入场（maker 限价单从未被穿越）"}

    def _pct(vals: list[float], q: float) -> float:
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * q))] * 100

    return {
        "n": n_usable,
        "tp_level_pct": tp * 100 if tp is not None else None,
        "sl_level_pct": sl * 100 if sl is not None else None,
        "reached_tp_pct": reached_tp / n_usable * 100,
        "reached_sl_pct": reached_sl / n_usable * 100,
        "reached_neither_pct": neither / n_usable * 100,
        "mfe": {"median": _pct(mfes, 0.5), "p90": _pct(mfes, 0.9)},
        "mae": {"median": _pct(maes, 0.5), "p10": _pct(maes, 0.1)},
        "note": (
            "MFE/MAE 按成交后窗口实测；taker 入场价取信号次根开盘，maker 取信号根收盘（近似挂单价）。"
            "窗口止于退出触发、超时或数据结束。"
        ),
    }


def _crossed(bar: dict, price: float, side: str) -> bool:
    return bar["l"] < price if side == "long" else bar["h"] > price
