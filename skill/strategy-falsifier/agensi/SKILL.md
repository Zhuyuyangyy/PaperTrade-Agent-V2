---
name: strategy-falsifier
description: Falsifies trading strategies and prints the evidence. Feed it a strategy spec (signals + exit rules) and a candles CSV; it runs a six-instrument battery -- path-aware replay, cost sensitivity, barrier reachability, random-entry control group, walk-forward out-of-sample folds, and a parameter-sensitivity grid -- then returns a verdict from falsification rules declared BEFORE any number is read. Use when someone asks whether a backtest is real, whether a strategy is overfitted, whether it survives fee stress, or which of several strategy variants is the least overfitted.
summary: Kill trading strategies with reproducible evidence, not opinions
version: 1.0.0
tags: [trading, backtest, quant, due-diligence, overfitting, audit]
keywords: [backtest validation, strategy audit, walk-forward, random control group, overfitting detection]
use_cases:
  - "Is my backtest curve real or a coincidence?"
  - "Does this strategy still work when fees double?"
  - "Which of my strategy variants is the least overfitted?"
  - "Audit a trading strategy someone sent me before I pay for signals"
docs_url: https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
---

# Strategy Falsifier

**This skill does not find profitable strategies. It kills unfounded ones.**

A backtest curve is a claim. This engine tests the claim with six instruments and
returns a verdict from falsification rules that were written down *before* any
number was read -- the same discipline a scientific control requires. Failed
runs are published with the same prominence as successful ones.

## How to run it

The engine is a single self-contained Python file (stdlib only, no network, no
sibling files). Run it exactly as shipped:

```bash
# 1. 30-second demo: the built-in RSI-dip rule on deterministic synthetic candles
python verify.py --demo

# 2. Audit a real strategy: a spec JSON + a candles CSV
python verify.py --spec /abs/path/spec.json --candles /abs/path/klines.csv --out report.md --json

# 3. Compare variants: which of my strategies is the least overfitted?
python verify.py --compare /abs/path/a.json /abs/path/b.json --out compare.md --json

# 4. HTML report (self-contained single file with charts)
python verify.py --spec /abs/path/spec.json --out report.html
```

**Always use ABSOLUTE paths.** The script runs from a temporary file; relative
paths resolve against the agent's working directory, not the script's.

## Input contracts

### Candles CSV
Columns `t,o,h,l,c` (t = epoch milliseconds). Common aliases are accepted:
`date/time/timestamp/open_time/datetime` for t, `open/high/low/close` (or
`vol/amount`) for the rest. Extra columns are ignored. A Binance/kline export
works as-is.

### Spec JSON (the contract that matters)
```json
{
  "name": "my-strategy",
  "symbol": "BTCUSDT",
  "interval": "1h",
  "venue": "futures",
  "fee_rate": 0.0004,
  "fill_model": "taker",
  "funding": false,
  "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
  "signals": {"kind": "csv", "path": "/abs/path/signals.csv"},
  "control": {"enabled": true, "n_random": 200, "seed": 42},
  "walk_forward": {"folds": 3},
  "param_grid": {"axes": {"take_profit": [0.02, 0.03, 0.04], "stop_loss": [-0.06, -0.08, -0.10]}},
  "falsify_if": {
    "worst_case_net_negative": true,
    "net_negative_at_fee_multiple": 2,
    "control_lift_ci_includes_zero": true,
    "grid_positive_ratio_min": 0.5,
    "min_fills": 30
  }
}
```

Rules that make this an audit and not a screensaver:

- **`falsify_if` is declared before the run.** No rules declared means no verdict
  beyond `NOT_FALSIFIED` -- an unaudited audit.
- **`min_fills` is the master gate.** Below the declared evidence bar the verdict
  is `INCONCLUSIVE` and no other rule fires. An 8-trade sample cannot falsify.
- **The control rule is "not proven better is falsified."** A CI spanning zero
  (indistinguishable from random entries) OR entirely below zero (significantly
  WORSE than random) both trip it. Only a fully positive CI passes.
- **Signals can come from a CSV** (`kind: "csv"`, columns `t[,side]`) or from the
  built-in rules (`kind: "rule"`, `rsi_below`/`rsi_above`). A strategy that
  cannot state its entry rule cannot be audited.

A spec may carry its own data: `candles_path`, `funding_path`,
`signals.path`, `exit.exit_signals_path` all resolve against the spec's own
directory, so a spec folder is one reproducible experiment.

## What the report says

Verdict first (`FALSIFIED` / `NOT_FALSIFIED` / `INCONCLUSIVE`), then the six
instruments:

| Instrument | Question it answers |
|---|---|
| Path-aware replay | Bars are walked, not sampled; a bar containing both stop and target resolves to the stop by default with the optimistic read alongside. Are you earning spread, or assumptions? |
| Cost sensitivity | Replayed at 1x/2x/5x your declared fee. Can the edge pay its own toll? |
| Barrier reachability | Do your TP/SL levels actually get visited inside the hold window -- or is it a lottery structure? |
| Random-entry control | Same window, same exit rules, same costs, random entry bars (fixed seed). Beat it or your absolute numbers mean nothing. |
| Walk-forward | The last fold is out-of-sample; the declared rules re-adjudicate there. Edge that only exists where you looked is not edge. |
| Parameter grid | The whole declared exit-parameter grid, replayed. Only a few cells positive = curve fitting, not edge. |

Every report ends with its own reproduction command and a **"not modelled"**
section (slippage/partial fills, capacity impact, correlated concurrent
positions, exchange/link risk) -- read it before trusting any number.

## The diagnosis layer

Verdict alone answers "was it falsified". The diagnosis section (section 8 of
every report) answers **what the evidence points to, and what to test next**:

- **Seven diagnostic outcomes** (not "causes of death" -- a NOT_FALSIFIED
  strategy did not die, and an INCONCLUSIVE one was never measured):
  `insufficient` (non-measurement), `randomness` (cannot beat / loses to
  random entries -- the most fundamental reading), `cost` (real edge, toll
  eats it), `edge_absent`, `overfitting` (real in-sample, gone out-of-sample
  or grid-brittle), `barrier` (take-profit never visited -- lottery
  structure), `survived` (not falsified under the declared rules -- which is
  the absence of disproof, not the presence of proof).
- **Secondary outcomes** for strategies that trip several at once.
- **Evidence** -- every claim quotes numbers from THIS run, no new assumptions.
- **Next experiments** -- each is a hypothesis + an acceptance criterion:
  diagnosis, the falsifiable bet ("add one orthogonal filter, expect ~1/5 the
  signals -- a hypothesis, not a target"), and what result counts as an
  answer ("control CI lower bound >= 0 with fills still >= min_fills").

The classification rules are declared in code before any strategy is
classified -- the same rules-before-numbers discipline as `falsify_if`. In
`--compare` mode every row of the matrix carries its outcome label, so a set
of variants can be read at a glance: which points to fees, which to
randomness, which to overfitting.

## Honest limits

- The engine is **offline and stdlib-only**: no market data is fetched, no
  orders are sent, nothing touches an account.
- It **cannot see your parameter search history**. If the parameters were tuned
  on the full window, "out-of-sample" is a label, not a fact. The report says so.
- It replays **one signal at a time, equal weight**: no portfolio construction,
  no position sizing, no compounding across overlapping trades.
- Verdicts are about **statistical evidence, not profitability advice**. A
  `NOT_FALSIFIED` verdict is not a recommendation to trade.

## Free vs paid

The engine is MIT-licensed and public. This skill packages it for one-command
use inside agent workflows. If you need a strategy ported, data cleaned, or a
human walkthrough of what the numbers mean, the author takes a limited number
of audit engagements (see the listing page).
