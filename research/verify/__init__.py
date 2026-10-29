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
  5. walk-forward          -- the window is split into N folds and the last one
                             is re-measured with the full battery as an
                             out-of-sample segment. A criterion firing there
                             falsifies the whole strategy: the industry's
                             number-one cause of backtest->live decay
                             (median -14.3pp) is an edge that only existed
                             where its parameters were chosen.

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

    With spec.walk_forward.enabled the window is split into N equal folds; the
    last fold is out-of-sample and gets its own full battery (replay, cost
    sensitivity, barrier reachability, control) next to the in-sample battery
    of the earlier folds. A criterion firing on the out-of-sample segment
    falsifies the whole strategy: surviving only where you looked is the
    failure mode this instrument exists to catch.
    """
    if not candles:
        raise ValueError("没有 K 线数据")
    if not entries:
        raise ValueError("没有入场信号")

    from ..audit.pathsim import Exit

    exit_rule = Exit(
        take_profit=spec.exit.take_profit,
        stop_loss=spec.exit.stop_loss,
        timeout_bars=spec.exit.timeout_bars,
        trailing=spec.exit.trailing,
        trailing_offset=spec.exit.trailing_offset,
        exit_signals=exit_signals or None,
    )

    full = _battery(spec, candles, entries, exit_rule, funding)
    result = {
        "spec": spec.to_dict(),
        "window": _window(candles),
        "n_signals": len(entries),
        "funding_applied": bool(spec.funding and funding),
        "runs": full["runs"],
        "cost_sensitivity": full["cost_sensitivity"],
        "barrier_reach": full["barrier_reach"],
        "control": full["control"],
        "verdict": full["verdict"],
        "walk_forward": None,
    }

    if spec.walk_forward.enabled:
        wf = _walk_forward(spec, candles, entries, exit_rule, funding)
        result["walk_forward"] = wf
        result["verdict"] = _verdict(
            spec, full["runs"], full["cost_sensitivity"], full["control"], wf=wf
        )
    return result


def _battery(spec: Spec, candles: list[dict], entries: dict[int, str], exit_rule,
             funding: list[dict] | None = None, *,
             control_range: tuple[int, int] | None = None,
             scope: str = "全窗口") -> dict:
    """The full instrument battery over ONE entry set (one segment or the
    whole window). Kept separate from verify() so the walk-forward segments
    run exactly the same measurements as the headline run -- an instrument
    that measures the sample differently from the sample-out is not a control.
    """
    from . import control, excursions, metrics
    from ..audit.pathsim import simulate

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
        try:
            ctrl = control.random_control(
                candles, exit_rule, spec, entries,
                n_random=spec.control.n_random, seed=spec.control.seed,
                funding=funding if spec.funding else None,
                bar_range=control_range,
            )
            ctrl = control.compare(
                runs["worst_case"]["stats"], ctrl, seed=spec.control.seed
            )
        except ValueError as exc:
            # A degenerate segment (e.g. a 1-bar out-of-sample fold) cannot
            # host a control group. Say so; never invent one.
            ctrl = {
                "n_random": 0, "seed": spec.control.seed, "side": None,
                "bar_range": list(control_range) if control_range else None,
                "note": f"该段无法抽取随机对照组：{exc}",
                "fills": 0, "unfilled": 0, "stats": metrics.equity_stats([]),
                "strategy_mean_pct": None, "control_mean_pct": None,
                "diff_pct": None, "diff_ci": None, "selectivity": "unknown",
            }

    return {
        "n_signals": len(entries),
        "runs": runs,
        "cost_sensitivity": cost_rows,
        "barrier_reach": reach,
        "control": ctrl,
        "verdict": _verdict(spec, runs, cost_rows, ctrl, scope=scope),
    }


def _walk_forward(spec: Spec, candles: list[dict], entries: dict[int, str],
                  exit_rule, funding: list[dict] | None) -> dict:
    """Split the window into N folds and battery the last one as out-of-sample.

    In-sample = entries in folds 1..N-1, out-of-sample = entries in fold N.
    Both batteries simulate over the FULL candle series: a trade entered in
    the last fold exits whenever its exit rule says, and the bars after the
    fold are still the future relative to that entry -- truncating them would
    manufacture 'unresolved' trades that real trading would have closed.
    """
    folds = spec.walk_forward.folds
    if len(candles) < folds:
        raise ValueError(
            f"窗口只有 {len(candles)} 根 K 线，不够切 {folds} 折"
            f"（walk_forward.folds 必须 ≤ 窗口 K 线数）"
        )
    fold_bars = len(candles) // folds
    boundary = (folds - 1) * fold_bars
    is_entries = {i: s for i, s in entries.items() if i < boundary}
    oos_entries = {i: s for i, s in entries.items() if i >= boundary}
    return {
        "folds": folds,
        "fold_bars": fold_bars,
        "boundary_bar": boundary,
        "boundary_ts": _fmt_ts(candles[boundary]["t"]),
        "in_sample": _battery(spec, candles, is_entries, exit_rule, funding,
                              scope="样本内") if is_entries else None,
        # the out-of-sample control may only draw entries from the out-of-
        # sample segment, or it is not a control for that segment
        "out_sample": _battery(spec, candles, oos_entries, exit_rule, funding,
                               control_range=(boundary, len(candles) - 2),
                               scope="样本外") if oos_entries else None,
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


def _fmt_ts(ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _window(candles: list[dict]) -> dict:
    return {"from": _fmt_ts(candles[0]["t"]), "to": _fmt_ts(candles[-1]["t"]), "bars": len(candles)}


def _outcome(checks: list[dict]) -> str:
    if any(c.get("triggered") for c in checks):
        return "FALSIFIED"
    if any(c.get("insufficient") for c in checks):
        return "INCONCLUSIVE"
    return "NOT_FALSIFIED"


def _evaluate_criteria(spec: Spec, runs: dict, cost_rows: list[dict], ctrl: dict | None,
                       scope: str, prefix: str = "") -> list[dict]:
    """Apply the spec's PRE-DECLARED criteria to one battery.

    The criteria live in the spec and were fixed before the report existed;
    this function only reads them and the numbers, it never invents a new
    rule. `scope` labels which segment the numbers came from so the report
    can show it; the same declared rule applied to the out-of-sample segment
    is the same rule, not a new one.
    """
    checks = []
    worst = runs["worst_case"]["stats"]

    if worst["n"] < spec.falsify_if.get("min_fills", 30):
        checks.append({
            "rule": "min_fills",
            "scope": scope,
            "detail": f"{prefix}成交 {worst['n']} 笔 < 下限 {spec.falsify_if.get('min_fills', 30)}，样本不足以下结论",
            "triggered": False,
            "insufficient": True,
        })

    if spec.falsify_if.get("worst_case_net_negative") and worst["sum_pct"] <= 0:
        checks.append({
            "rule": "worst_case_net_negative",
            "scope": scope,
            "detail": f"{prefix}最坏优先口径净收益 {worst['sum_pct']:+.2f}% ≤ 0",
            "triggered": True,
        })

    mult = spec.falsify_if.get("net_negative_at_fee_multiple")
    if mult is not None:
        row = next((r for r in cost_rows if r["fee_multiple"] == mult), None)
        if row is not None and row["net_sum_pct"] <= 0:
            checks.append({
                "rule": f"net_negative_at_fee_multiple={mult}",
                "scope": scope,
                "detail": f"{prefix}成本 ×{mult:g}（{row['fee_bps']:.0f}bps 往返）时净收益 {row['net_sum_pct']:+.2f}% ≤ 0，edge 付不起 toll",
                "triggered": True,
            })

    if spec.falsify_if.get("control_lift_ci_includes_zero") and ctrl is not None:
        lo, hi = ctrl.get("diff_ci") or [None, None]
        if lo is not None and lo <= 0 <= hi:
            checks.append({
                "rule": "control_lift_ci_includes_zero",
                "scope": scope,
                "detail": f"{prefix}策略 − 随机对照组 = {ctrl['diff_pct']:+.2f}%，95% 区间 [{lo:+.2f}, {hi:+.2f}] 跨 0，未能证明选择性",
                "triggered": True,
            })
    return checks


def _verdict(spec: Spec, runs: dict, cost_rows: list[dict], ctrl: dict | None,
             scope: str = "全窗口", wf: dict | None = None) -> dict:
    """Evaluate the pre-declared falsification criteria. Criteria live in the
    spec and were fixed before the report existed; this function only reads
    them and the numbers, it never invents a new rule.

    When a walk-forward split is present, the same declared criteria are also
    evaluated on the out-of-sample segment, and a rule firing THERE falsifies
    the whole strategy -- surviving only where you looked is the failure mode
    the walk-forward instrument exists to catch. Too few out-of-sample fills
    is INCONCLUSIVE, never a pass.
    """
    checks = _evaluate_criteria(spec, runs, cost_rows, ctrl, scope=scope)

    if wf is not None:
        oos = wf.get("out_sample")
        if oos is None:
            checks.append({
                "rule": "min_fills",
                "scope": "样本外",
                "detail": (f"样本外段（第 {wf['boundary_bar']} 根起）没有任何入场信号"
                           f"——规则在该段未触发，无法评估样本外表现"),
                "triggered": False,
                "insufficient": True,
            })
        else:
            checks.extend(_evaluate_criteria(
                spec, oos["runs"], oos["cost_sensitivity"], oos["control"],
                scope="样本外",
                prefix=f"样本外段（第 {wf['boundary_bar']} 根起）：",
            ))

    triggered = [c for c in checks if c.get("triggered")]
    insufficient = [c for c in checks if c.get("insufficient")]
    if triggered:
        outcome = "FALSIFIED"
        reason = f"{len(triggered)} 条事前证伪规则被触发：" + "；".join(c["detail"] for c in triggered)
    elif insufficient:
        outcome = "INCONCLUSIVE"
        scopes = "、".join(dict.fromkeys(c["scope"] for c in insufficient))
        reason = (f"样本量不足（{scopes}），既不能证实也不能证伪"
                  f"——先去拿更多数据，不要凭这份报告做任何决定。")
    else:
        outcome = "NOT_FALSIFIED"
        reason = (
            f"在本窗口、本成本假设、本对照组下未触发任何事前证伪规则"
            f"（最坏口径净 {runs['worst_case']['stats']['sum_pct']:+.2f}% / "
            f"最好口径 {runs['best_case']['stats']['sum_pct']:+.2f}%）。"
        )
        if wf is not None:
            reason += "样本外段同样未触发任何规则。"
            reason += "注意：未被证伪 ≠ 被证实；参数是否曾用全窗口数据调优，引擎无法验证。"
        else:
            reason += "注意：未被证伪 ≠ 被证实，样本外表现与参数过拟合不在本引擎的射程内。"
    return {"outcome": outcome, "reason": reason, "checks": checks}
