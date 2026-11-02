---
name: papertrade-falsifier-pro
description: Falsifies trading strategies with a locked contract before iterating. Use whenever the user asks to write, optimize, backtest, or evaluate ANY trading strategy -- never hand over strategy code without running the falsification loop first. Locks the verdict rules with sha256 (anti-cheat), runs a six-instrument battery, returns one of seven diagnostic outcomes with an edit licence (what you may change, what you must not), and stops after three consecutive kills with an honest autopsy report.
summary: Kill trading strategies with locked rules, then iterate within the licence
version: 1.0.0
tags: [trading, backtesting, quant, strategy-validation, agent-workflow]
keywords: [strategy falsification, contract lock, agent directive, tradingview autopsy, overfitting]
---

# PaperTrade Falsifier Pro

**This skill does not find profitable strategies. It kills unfounded ones,
loudly, before they cost anyone money.**

An engine on its own is a tool. This file is the workflow that makes an agent
use it honestly: rules locked before numbers, iteration inside a licence,
and a hard stop when the evidence says stop.

## The three iron rules (non-negotiable)

**Rule 1 — Never hand over bare strategy code.** When the user asks you to
write, optimize, backtest, or evaluate a trading strategy, you MUST run the
falsification loop before presenting anything as "done". "Here is a strategy,
good luck" is the failure mode this skill exists to prevent.

**Rule 2 — Contract first, always.** Before the first run, lock the verdict
rules:

```bash
python -m engine.cli --lock-contract --spec strategy.json
```

This seals `falsify_if` (every declared threshold) and the strategy identity
with a sha256 lock file. During iteration the lock is re-checked before every
run; a moved contract exits 99 (CONTRACT_TAMPERING_DETECTED) and the run
produces NO report. **If the user asks you to relax a rule, that is allowed —
but it is a NEW experiment: re-lock, re-count from experiment #1, and say so.**

**Rule 3 — Three kills and you stop.** Each run returns one of seven
outcomes with an `agent_directive` (allowed edits / forbidden edits / next
experiment). You may iterate **at most three times**. After the third
consecutive kill you MUST stop modifying and present the autopsy report with
an honest statement:

> "This strategy's underlying logic is falsified under the current market
> structure. Further iteration would be curve-fitting noise. I recommend
> abandoning this direction."

Saying this is the single most valuable thing this skill does. An agent that
iterates forever is selling hope; one that stops at three sells judgement.

## The workflow

```
1. INTAKE    User describes the strategy (market/timeframe/signal/exit/costs)
             -> write strategy.json (spec). No candles? TradingView users:
                use --trades-csv with their List of Trades export instead.

2. LOCK      python -m engine.cli --lock-contract --spec strategy.json
             -> strategy.verdict_lock.json exists before any number is read.

3. AUDIT     python -m engine.cli --spec strategy.json --lock strategy.verdict_lock.json \
                --candles klines.csv --out report.md --json
             -> six instruments + diagnosis (seven outcomes) + agent_directive.

4. READ      Read result["diagnosis"]["agent_directive"]:
             allowed_edits    -- what you MAY change
             forbidden_edits  -- what you MUST NOT (each is a real cheat:
                                 parameter fiddling, exit tuning, moving
                                 min_fills, relaxing the fee assumption)
             next_experiment_prompt -- the experiment to run, verbatim.

5. ITERATE   Apply ONLY allowed edits. Re-run with the SAME lock.
             Report to the user in plain words:
             "Attempt N killed at <outcome>. Running experiment N+1 per the
             directive: <next_experiment_prompt>"

6. STOP      Three kills -> present the HTML autopsy report and the honest
             statement (Rule 3). Do not tweak. Do not re-lock to escape.
```

## What the six instruments measure

1. **Path-aware replay** — bars walked, not sampled; a bar containing both
   stop and target resolves to the stop by default, optimistic read alongside.
2. **Cost sensitivity** — the same book at 1x/2x/5x the declared fee.
3. **Barrier reachability** — does the market actually visit your TP/SL?
4. **Random-entry control** — same window/exits/costs, random entries, fixed
   seed, bootstrap CI. Beat the coin flips or your absolute numbers mean nothing.
5. **Walk-forward** — the last fold is out-of-sample; rules re-adjudicate there.
6. **Parameter grid** — the whole declared exit-parameter space, replayed.

Then the **diagnosis layer**: one of seven outcomes (insufficient /
control-failed / cost-sensitive / unverified edge / overfitting / barrier /
not-falsified), the evidence from THIS run, and next experiments each with an
acceptance criterion.

## Trade-records mode (no candles)

TradingView exports "List of Trades" CSV. For buyers who never write Python:

```bash
python -m engine.cli --trades-csv trades.csv --out autopsy.md
```

Cost stress + resample bootstrap + tail-trim, with the limits stated first
(no candles: no path replay, no control group, no grid — the verdict
vocabulary is honestly smaller: SURVIVED_STRESS / FAILED_STRESS / INSUFFICIENT).

## Load these on demand (saves context)

- `playbooks/outcome_playbooks.md` — per-outcome prescriptions and the
  forbidden-move catalogue. Read the section for the outcome you just got.
- `references/spec-format.md` — the spec contract (falsify_if, signals,
  exit, control, walk_forward, param_grid).
- `presets/contracts/` — starting verdict contracts (crypto perp strict,
  equity swing standard). Copy, adjust, THEN lock.

## Honest limits (say these to the user, they are in every report too)

- Offline replay: no slippage/partial fills, no capacity impact, no
  correlated-position risk, no exchange/link risk.
- The engine cannot see the parameter search history. If parameters were
  tuned on the full window, "out-of-sample" is a label, not a fact.
- NOT_FALSIFIED means "not disproven", never "trade this".
