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
  6. parameter grid        -- the exit parameters are gridded (values declared
                             in the spec, before the numbers) and every cell
                             is replayed. An edge that survives at one exact
                             TP/SL combination and nowhere else is a curve
                             fit; the distribution across cells, not the best
                             cell, is the measurement.

The verdict is not a score. It is the outcome of falsification criteria that
were written into the spec BEFORE the numbers were read. That ordering is the
whole point: a criterion chosen after seeing the report is marketing.

Nothing here predicts anything. It measures whether a claimed edge survives
honest execution, honest costs, and an honest control group.
"""
from __future__ import annotations

from .spec import Spec, SpecError
from .signals import CONTROL_WARMUP

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

    With spec.param_grid.enabled the declared exit parameters are gridded and
    every cell replayed. The grid follows the same scope convention as the
    rest of the engine: the full entry set when walk-forward is off, the
    in-sample and out-of-sample segments when it is on.
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
        "param_grid": None,
    }

    wf = None
    if spec.walk_forward.enabled:
        wf = _walk_forward(spec, candles, entries, exit_rule, funding)
        result["walk_forward"] = wf

    if spec.param_grid.enabled:
        result["param_grid"] = _param_grid_result(spec, candles, entries, exit_rule, funding, wf)

    result["verdict"] = _verdict(
        spec, full["runs"], full["cost_sensitivity"], full["control"],
        wf=wf,
        grids=result["param_grid"]["scopes"] if result["param_grid"] else None,
    )
    # Diagnosis: HOW it died and what to change. Same measurements, no new
    # assumptions -- the classification rules live in diagnosis.py, declared
    # before any strategy is classified.
    from .diagnosis import diagnose

    result["diagnosis"] = diagnose(result)
    return result


def _param_grid_result(spec: Spec, candles: list[dict], entries: dict[int, str],
                       exit_rule, funding: list[dict] | None, wf: dict | None) -> dict:
    """Assemble the param_grid block: the declaration plus one grid per scope
    (the full entry set, or the walk-forward segments when it is on)."""
    from . import grid as grid_instrument

    if wf is not None:
        scopes = [g for g in (wf.get("in_sample_grid"), wf.get("out_sample_grid")) if g]
    else:
        scopes = [grid_instrument.run(spec, candles, entries, exit_rule, funding, scope="全窗口")]
    return {
        "axes": {k: list(v) for k, v in spec.param_grid.axes.items()},
        "axis_order": list(spec.param_grid.axes),
        "max_cells": spec.param_grid.max_cells,
        "scopes": scopes,
    }


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
            # Round trip = both sides. The simulator deducts fee_rate*2 per
            # trade; showing the one-side number under a "round-trip" label
            # understated every toll in the table by exactly half.
            "fee_bps": spec.fee_rate * mult * 2 * 10_000,
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
                "fills": 0, "unfilled": 0, "open_signals": 0,
                "stats": metrics.equity_stats([]),
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

    When the parameter grid is declared it runs per segment too (same entry
    split), and each segment's own verdict is recomputed with its grid --
    otherwise the WF table's per-segment verdict would disagree with the
    criteria table whenever a grid rule fires on one segment.
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
    wf = {
        "folds": folds,
        "fold_bars": fold_bars,
        "boundary_bar": boundary,
        "boundary_ts": _fmt_ts(candles[boundary]["t"]),
        # the in-sample control may only draw from the in-sample segment: a
        # control that reaches into the out-of-sample fold would book that
        # fold's random losses as "control" and inflate the in-sample
        # selectivity it is supposed to be blind to
        "in_sample": _battery(spec, candles, is_entries, exit_rule, funding,
                              control_range=(CONTROL_WARMUP, boundary - 1),
                              scope="样本内") if is_entries else None,
        # the out-of-sample control may only draw entries from the out-of-
        # sample segment, or it is not a control for that segment
        "out_sample": _battery(spec, candles, oos_entries, exit_rule, funding,
                               control_range=(boundary, len(candles) - 2),
                               scope="样本外") if oos_entries else None,
        "in_sample_grid": None,
        "out_sample_grid": None,
    }
    if spec.param_grid.enabled:
        from . import grid as grid_instrument

        if is_entries:
            wf["in_sample_grid"] = grid_instrument.run(
                spec, candles, is_entries, exit_rule, funding, scope="样本内")
        if oos_entries:
            wf["out_sample_grid"] = grid_instrument.run(
                spec, candles, oos_entries, exit_rule, funding, scope="样本外")
        for seg, grid_key in (("in_sample", "in_sample_grid"),
                              ("out_sample", "out_sample_grid")):
            battery, seg_grid = wf.get(seg), wf.get(grid_key)
            if battery is not None and seg_grid is not None:
                battery["verdict"] = _verdict(
                    spec, battery["runs"], battery["cost_sensitivity"], battery["control"],
                    scope=seg_grid["scope"], grids=[seg_grid],
                )
    return wf


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

    # The spec declared its own evidence bar (min_fills). Below it, NOTHING
    # else is evaluated: a net rule firing on 8 fills would let a weak sample
    # produce a strong verdict the spec itself said it did not want, and
    # "insufficient" would stop being a verdict and become a footnote.
    min_fills = spec.falsify_if.get("min_fills", 30)
    if worst["n"] < min_fills:
        checks.append({
            "rule": "min_fills",
            "scope": scope,
            "detail": (f"{prefix}成交 {worst['n']} 笔 < 声明下限 {min_fills}，"
                       f"样本不足以下结论（其余规则未评估）"),
            "triggered": False,
            "insufficient": True,
        })
        return checks

    # A segment with ZERO fills has no measured net: sum_pct is 0.0 because
    # nothing traded, not because the strategy broke even. Firing a net rule
    # there would falsify on no evidence -- the same line the grid draws
    # ("无证据不算输"). With at least one fill the net IS measured and the
    # declared rule stands.
    measured = worst["n"] > 0

    if spec.falsify_if.get("worst_case_net_negative") and measured and worst["sum_pct"] <= 0:
        checks.append({
            "rule": "worst_case_net_negative",
            "scope": scope,
            "detail": f"{prefix}最坏优先口径净收益 {worst['sum_pct']:+.2f}% ≤ 0",
            "triggered": True,
        })

    mult = spec.falsify_if.get("net_negative_at_fee_multiple")
    if mult is not None:
        row = next((r for r in cost_rows if r["fee_multiple"] == mult), None)
        if row is not None and measured and row["net_sum_pct"] <= 0:
            checks.append({
                "rule": f"net_negative_at_fee_multiple={mult}",
                "scope": scope,
                "detail": f"{prefix}成本 ×{mult:g}（往返 {row['fee_bps']:.0f}bps）时净收益 {row['net_sum_pct']:+.2f}% ≤ 0，edge 付不起 toll",
                "triggered": True,
            })

    if spec.falsify_if.get("control_lift_ci_includes_zero") and ctrl is not None:
        lo, hi = ctrl.get("diff_ci") or [None, None]
        # "Includes zero" was too narrow: an interval entirely BELOW zero is
        # not a pass, it is the strategy being significantly WORSE than coin
        # flips. The rule fires unless selectivity is PROVEN (lo > 0).
        if lo is not None and not lo > 0:
            if hi is not None and hi < 0:
                detail = (f"{prefix}策略 − 随机对照组 = {ctrl['diff_pct']:+.2f}%，"
                          f"95% 区间 [{lo:+.2f}, {hi:+.2f}] 整体为负——显著差于随机入场")
            else:
                detail = (f"{prefix}策略 − 随机对照组 = {ctrl['diff_pct']:+.2f}%，"
                          f"95% 区间 [{lo:+.2f}, {hi:+.2f}] 跨 0，未能证明选择性")
            checks.append({
                "rule": "control_lift_ci_includes_zero",
                "scope": scope,
                "detail": detail,
                "triggered": True,
            })
    return checks


def _grid_checks(spec: Spec, grid: dict, scope: str, prefix: str = "") -> list[dict]:
    """Apply the PRE-DECLARED grid criterion to one grid scope.

    The rule (`falsify_if.grid_positive_ratio_min`) lives in the spec and was
    fixed before the report existed; this function only reads it and the
    measured ratio -- it never invents a threshold after seeing the cells. A
    grid where nothing traded is INCONCLUSIVE: no evidence is not a failure,
    and it is certainly not a pass.
    """
    checks: list[dict] = []
    min_ratio = spec.falsify_if.get("grid_positive_ratio_min")
    if min_ratio is None:
        return checks
    if grid["cells_with_fills"] == 0:
        checks.append({
            "rule": "grid_positive_ratio_min",
            "scope": scope,
            "detail": (f"{prefix}参数网格 {grid['n_cells']} 格全部无成交，"
                       f"无法评估参数敏感性"),
            "triggered": False,
            "insufficient": True,
        })
        return checks
    if grid["positive_ratio"] < float(min_ratio):
        no_fill = (f"（另有 {grid['no_fill_cells']} 格无成交，不计入占比）"
                   if grid["no_fill_cells"] else "")
        if grid["positive_cells"] == 0:
            tail = (f"参数网格 0/{grid['cells_with_fills']} 格为正{no_fill}——"
                    f"网格内没有任何参数组合赚钱，edge 在这个参数空间里不存在，更谈不上稳健")
        else:
            tail = (f"参数网格 {grid['positive_cells']}/{grid['cells_with_fills']} 格为正"
                    f"（{grid['positive_ratio']:.0%}）< 声明下限 {float(min_ratio):.0%}{no_fill}——"
                    f"edge 只在一小撮参数组合下存在，是曲线拟合不是 edge")
        checks.append({
            "rule": "grid_positive_ratio_min",
            "scope": scope,
            "detail": f"{prefix}{tail}",
            "triggered": True,
        })
    return checks


def _verdict(spec: Spec, runs: dict, cost_rows: list[dict], ctrl: dict | None,
             scope: str = "全窗口", wf: dict | None = None,
             grids: list[dict] | None = None) -> dict:
    """Evaluate the pre-declared falsification criteria. Criteria live in the
    spec and were fixed before the report existed; this function only reads
    them and the numbers, it never invents a new rule.

    When a walk-forward split is present, the same declared criteria are also
    evaluated on the out-of-sample segment, and a rule firing THERE falsifies
    the whole strategy -- surviving only where you looked is the failure mode
    the walk-forward instrument exists to catch. Too few out-of-sample fills
    is INCONCLUSIVE, never a pass.

    `grids` carries one parameter-grid result per scope (the full entry set,
    or the walk-forward segments). The declared grid criterion is evaluated
    on every scope; a knife-edge grid falsifies wherever it was measured.
    """
    checks = _evaluate_criteria(spec, runs, cost_rows, ctrl, scope=scope)
    for grid in (grids or []):
        prefix = ""
        if wf is not None and grid["scope"] == "样本外":
            prefix = f"样本外段（第 {wf['boundary_bar']} 根起）："
        checks.extend(_grid_checks(spec, grid, scope=grid["scope"], prefix=prefix))

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
        # Group by scope and say the scope ONCE: four lines each starting
        # "样本外段（第 19564 根起）：" read like a broken record, and a
        # record that skips is how readers skim past the actual findings.
        by_scope: dict[str, list[str]] = {}
        for c in triggered:
            by_scope.setdefault(c.get("scope", "全窗口"), []).append(c["detail"])
        parts = []
        for scope, details in by_scope.items():
            if scope == "全窗口":
                parts.extend(details)
            else:
                stripped = [d.split("：", 1)[1] if "：" in d else d for d in details]
                parts.append(f"{scope}段：" + "；".join(stripped))
        reason = f"{len(triggered)} 条事前证伪规则被触发：" + "；".join(parts)
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
        elif not grids:
            reason += "注意：未被证伪 ≠ 被证实，样本外表现与参数过拟合不在本引擎的射程内。"
        else:
            reason += "注意：未被证伪 ≠ 被证实；参数网格只测量参数脆弱性（且在窗口内），不构成样本外证据。"
        for grid in (grids or []):
            if grid["cells_with_fills"]:
                reason += (f"参数网格（{grid['scope']}）"
                           f"{grid['positive_cells']}/{grid['cells_with_fills']} 格为正。")
    return {"outcome": outcome, "reason": reason, "checks": checks}
