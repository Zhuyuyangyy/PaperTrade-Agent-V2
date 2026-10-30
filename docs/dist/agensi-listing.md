# Agensi Listing Copy · Strategy Falsifier

Draft listing text for the Agensi Creator Studio submission. Pricing follows the
external reviewer's advice: a genuinely runnable FREE base first, paid tier only
where it adds obvious value. Competitor reference points found on the platform:
Backtest-Validator ($5), Trading Strategy Backtest Auditor ($9.99).

---

## Listing title (≤ 60 chars)

```
Strategy Falsifier — kill a backtest with evidence, not opinions
```

## Tagline

```
Six-instrument falsification battery for trading strategies: path-aware replay,
cost stress, random-entry control, walk-forward, parameter grid. Verdicts from
rules declared before any number is read.
```

## Short description (listing card)

```
Feed it a strategy spec + candles CSV. It tries to KILL the strategy with six
instruments and prints the evidence: worst/best-case replay, fee stress at
1x/2x/5x, barrier reachability, a random-entry control group with bootstrap CI,
out-of-sample walk-forward folds, and a parameter-sensitivity grid. The verdict
comes from falsification rules declared BEFORE any number is read — the same
discipline a scientific control requires. Failed runs are published with the
same prominence as successes; a strategy that earns +34% over three years and
still cannot beat random entries gets marked FALSIFIED. Offline, stdlib-only,
single self-contained script. Free tier runs the full battery.
```

## Long description (listing page)

```
WHAT THIS IS

A falsification engine for trading strategies, packaged as an agent skill.

A backtest curve is a claim. This engine tests the claim. Six instruments run
over your signals and candles:

  1. Path-aware replay — bars are walked, not sampled. A bar containing both
     your stop and your target resolves to the stop by default, with the
     optimistic read printed alongside. You see what you earn from spread and
     what you earn from assumptions.
  2. Cost sensitivity — the same book replayed at 1x/2x/5x your declared fee.
     If the edge dies at 2x, it was paying its profits to the exchange.
  3. Barrier reachability — are your TP/SL levels prices the market actually
     visits inside the hold window, or is your "stop loss" a lottery ticket?
  4. Random-entry control group — same window, same exit rules, same costs,
     random entry bars, fixed seed, bootstrap CI on the difference. Beat the
     coin flips or your absolute numbers mean nothing.
  5. Walk-forward — the last fold is out-of-sample; the declared rules
     re-adjudicate there. Edge that only exists where you looked is not edge.
  6. Parameter grid — the whole declared exit-parameter space, replayed. Only
     a few cells positive = curve fitting, not edge.

The verdict (FALSIFIED / NOT_FALSIFIED / INCONCLUSIVE) comes from rules YOU
declare in the spec before the run. Below your own evidence bar the answer is
INCONCLUSIVE — the engine refuses to conclude from an 8-trade sample.

WHY IT EXISTS

The median backtest-to-live gap in retail quant is roughly -14 percentage
points of return. Most "validation" tools draw prettier curves. This one tries
to kill yours and shows its work. The author used it to falsify their own
six-agent trading system before building anything on top of it, then open-
sourced the instrument (MIT). Public case matrix includes a strategy that made
+61% over 3.3 years and was still marked FALSIFIED — statistically
indistinguishable from random entries.

WHAT YOU GET

  - Markdown report (verdict first, then the six instruments, then the exact
    reproduction command, then a "not modelled" disclosure section)
  - Machine-readable JSON of every number
  - Optional self-contained HTML report with charts (equity curves, cost bars,
    parameter-grid heatmap)
  - Multi-strategy comparison mode: which of your variants is least overfitted,
    ranked by a rule declared before the table

HONEST LIMITS (also printed in every report)

  - Offline replay: no slippage/partial fills, no capacity impact, no
    correlated-position risk, no exchange/link risk
  - It cannot see your parameter search history — if you tuned on the full
    window, "out-of-sample" is a label, not a fact
  - NOT_FALSIFIED means "not disproven", never "trade this"

REQUIREMENTS

Python 3.10+. The engine is a single stdlib-only script: no installs, no
network, no account, no data leaves your machine.
```

## Pricing plan

| Tier | Price | Contents |
|---|---|---|
| **Falsifier (base)** | **Free** | Full six-instrument battery, single strategy or comparison, md/json/html outputs. The engine is MIT and public; the free tier is genuinely complete — no crippled demo. |
| **Falsifier Pro** | $9 one-time | Everything in base, plus: bundled reference exporter (turn a freqtrade-style strategy into engine signal CSVs), the public case matrix as worked examples, priority re-runs when the engine updates. |
| **Human audit** | $99 / $299 (off-platform, author's discretion) | The author ports YOUR strategy, cleans the data, runs the battery, and walks you through what the numbers mean. Limited slots; enquiry via the GitHub repo. |

Rationale: the base tier being complete and free is the marketing — the paid
tiers sell convenience and human time, not a key that unlocks the verdict.

## Submission checklist (for the owner)

1. ZIP: `skill/strategy-falsifier/agensi/strategy-falsifier-agensi-v1.0.0.zip`
   (SHA256 in `SHA256SUMS.txt` next to it; verified by extract-and-run)
2. Submit at https://www.agensi.io/dashboard/submit
3. Free tier can go live immediately; the $9 tier and any payout need the
   payout method verified first (Zoneless USDC / Solana route for creators in
   mainland China — still requires provider verification)
4. Do not submit the paid tier as "mature" until at least a few free-tier runs
   have been observed (the 30-day gate from docs/dist/README.md applies here too)

## What was NOT done (per the reviewer's instruction)

- No market submission was made on the owner's behalf
- The main branch was not modified: everything lives on
  `feature/agensi-release` and ships as a PR
- No obfuscation, encoding, or scan-evasion of any kind: the script is plain,
  complete, commented source
