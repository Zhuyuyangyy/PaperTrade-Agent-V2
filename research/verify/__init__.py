"""Strategy falsification engine: path-aware replay with pre-declared kill criteria.

This package is the productised form of everything the PaperTrade research line
learned the hard way. A strategy comes in as a spec; what comes out is a report
that tries to KILL it, using the four instruments that survived contact with
real data:

  1. path-aware replay    -- bars are walked, not sampled; a bar containing both
                             a stop and a target resolves to the stop (worst
                             case) unless you ask for the optimistic run too.
  2. cost sensitivity     -- the toll is re-priced at 1x/2x/5x the assumed fee,
                             because the fee is the one parameter nobody
                             stress-tests and everybody inherits.
  3. barrier reachability -- are the take-profit and stop levels prices the
                             market actually visits inside the hold window? A
                             strategy whose barriers are never touched is a
                             lottery ticket, not an edge.
  4. random-entry control -- the same window, the same exit rule, entries drawn
                             at random. If the strategy cannot separate itself
                             from coin flips, its selectivity is unproven,
                             whatever the backtest curve says.

The verdict is not a score. It is the outcome of falsification criteria that
were written into the spec BEFORE the numbers were read. That ordering is the
whole point: a criterion chosen after seeing the report is marketing.

Nothing here predicts anything. It measures whether a claimed edge survives
honest execution, honest costs, and an honest control group.
"""
from __future__ import annotations

from .spec import Spec, SpecError

__all__ = ["Spec", "SpecError", "verify", "verify_file"]


def verify(spec: Spec, candles: list[dict], entries: dict[int, str],
           funding: list[dict] | None = None,
           exit_signals: dict[int, str] | None = None) -> dict:
    """Run the full falsification battery for one strategy over one price series.

    candles : [{'t': ms, 'o','h','l','c': float, 'v': float}, ...] ascending.
    entries : bar index -> 'long' | 'short', as produced by a signal source.
    exit_signals : optional bar index -> reason, for strategies whose exit
    logic is itself a signal (fills at the bar close).
    Returns a plain dict (JSON-serialisable) ready for report rendering.
    """
    from . import control, excursions, metrics
    from ..audit.pathsim import Exit, simulate

    if not candles:
        raise ValueError("没有 K 线数据")
    if not entries:
        raise ValueError("没有入场信号")

    exit_rule = Exit(
        take_profit=spec.exit.take_profit,
        stop_loss=spec.exit.stop_loss,
        timeout_bars=spec.exit.timeout_bars,
        trailing=spec.exit.trailing,
        trailing_offset=spec.exit.trailing_offset,
        exit_signals=exit_signals or None,
    )

    # --- 1. path-aware replay, worst case and best case side by side ---------
    runs = {}
    for case_name, worst in (("worst_case", spec.worst_case), ("best_case", not spec.worst_case)):
        sim = simulate(
            candles, entries, exit_rule,
            symbol=spec.symbol, fee_rate=spec.fee_rate,
            funding_series=funding if spec.funding else None,
            worst_case=worst, fill_model=spec.fill_model,
        )
        nets = sim.net_pct_series()
        runs[case_name] = {
            "fills": sim.closed,
            "unfilled": sim.unfilled_entries,
            "open_signals": sim.open_signals,
            "bars": sim.bars,
            "stats": metrics.equity_stats(nets),
            "by_reason": _by_reason(sim),
        }

    # --- 2. cost sensitivity: does the edge survive its own toll? -----------
    cost_rows = []
    for mult in spec.fee_multiples:
        sim = simulate(
            candles, entries, exit_rule,
            symbol=spec.symbol, fee_rate=spec.fee_rate * mult,
            funding_series=funding if spec.funding else None,
            worst_case=spec.worst_case, fill_model=spec.fill_model,
        )
        st = metrics.equity_stats(sim.net_pct_series())
        cost_rows.append({
            "fee_multiple": mult,
            "fee_bps": spec.fee_rate * mult * 10_000,
            "net_sum_pct": st["sum_pct"],
            "profit_factor": st["profit_factor"],
            "win_pct": st["win_pct"],
        })

    # --- 3. barrier reachability -------------------------------------------
    reach = excursions.barrier_reach(candles, entries, exit_rule, fill_model=spec.fill_model)

    # --- 4. random-entry control -------------------------------------------
    ctrl = None
    if spec.control.enabled:
        ctrl = control.random_control(
            candles, exit_rule, spec, entries,
            n_random=spec.control.n_random, seed=spec.control.seed,
            funding=funding if spec.funding else None,
        )
        ctrl = control.compare(
            runs["worst_case"]["stats"], ctrl, seed=spec.control.seed
        )

    verdict = _verdict(spec, runs, cost_rows, ctrl)

    return {
        "spec": spec.to_dict(),
        "window": _window(candles),
        "n_signals": len(entries),
        "runs": runs,
        "cost_sensitivity": cost_rows,
        "barrier_reach": reach,
        "control": ctrl,
        "verdict": verdict,
    }


def verify_file(spec_path, candles, entries, funding=None) -> dict:
    """Convenience: load a spec from JSON and run the battery."""
    return verify(Spec.load(spec_path), candles, entries, funding=funding)


def _by_reason(sim) -> dict:
    out: dict[str, dict] = {}
    for f in sim.fills:
        b = out.setdefault(f.reason, {"n": 0, "net_sum_pct": 0.0})
        b["n"] += 1
        b["net_sum_pct"] += f.net_pct * 100
    return out


def _window(candles: list[dict]) -> dict:
    import datetime as _dt

    def _fmt(ms: int) -> str:
        return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

    return {"from": _fmt(candles[0]["t"]), "to": _fmt(candles[-1]["t"]), "bars": len(candles)}


def _verdict(spec: Spec, runs: dict, cost_rows: list[dict], ctrl: dict | None) -> dict:
    """Evaluate the pre-declared falsification criteria. Criteria live in the
    spec and were fixed before the report existed; this function only reads
    them and the numbers, it never invents a new rule."""
    checks = []
    worst = runs["worst_case"]["stats"]
    best = runs["best_case"]["stats"]

    if worst["n"] < spec.falsify_if.get("min_fills", 30):
        checks.append({
            "rule": "min_fills",
            "detail": f"成交 {worst['n']} 笔 < 下限 {spec.falsify_if.get('min_fills', 30)}，样本不足以下结论",
            "triggered": False,
            "insufficient": True,
        })

    if spec.falsify_if.get("worst_case_net_negative") and worst["sum_pct"] <= 0:
        checks.append({
            "rule": "worst_case_net_negative",
            "detail": f"最坏优先口径净收益 {worst['sum_pct']:+.2f}% ≤ 0",
            "triggered": True,
        })

    mult = spec.falsify_if.get("net_negative_at_fee_multiple")
    if mult is not None:
        row = next((r for r in cost_rows if r["fee_multiple"] == mult), None)
        if row is not None and row["net_sum_pct"] <= 0:
            checks.append({
                "rule": f"net_negative_at_fee_multiple={mult}",
                "detail": f"成本 ×{mult:g}（{row['fee_bps']:.0f}bps 往返）时净收益 {row['net_sum_pct']:+.2f}% ≤ 0，edge 付不起 toll",
                "triggered": True,
            })

    if spec.falsify_if.get("control_lift_ci_includes_zero") and ctrl is not None:
        lo, hi = ctrl.get("diff_ci") or [None, None]
        if lo is not None and lo <= 0 <= hi:
            checks.append({
                "rule": "control_lift_ci_includes_zero",
                "detail": f"策略 − 随机对照组 = {ctrl['diff_pct']:+.2f}%，95% 区间 [{lo:+.2f}, {hi:+.2f}] 跨 0，未能证明选择性",
                "triggered": True,
            })

    triggered = [c for c in checks if c.get("triggered")]
    insufficient = any(c.get("insufficient") for c in checks)
    if triggered:
        outcome = "FALSIFIED"
        reason = f"{len(triggered)} 条事前证伪规则被触发：" + "；".join(c["detail"] for c in triggered)
    elif insufficient:
        outcome = "INCONCLUSIVE"
        reason = "样本量不足，既不能证实也不能证伪——先去拿更多数据，不要凭这份报告做任何决定。"
    else:
        outcome = "NOT_FALSIFIED"
        reason = (
            f"在本窗口、本成本假设、本对照组下未触发任何事前证伪规则"
            f"（最坏口径净 {worst['sum_pct']:+.2f}% / 最好口径 {best['sum_pct']:+.2f}%）。"
            "注意：未被证伪 ≠ 被证实，样本外表现与参数过拟合不在本引擎的射程内。"
        )
    return {"outcome": outcome, "reason": reason, "checks": checks}
