# Agensi Listing Copy — Strategy Falsifier (Free)

Agensi submission is a **free-only listing** for the complete open-source skill. Do not include Gumroad links, external checkout URLs, paid-tier prices, or off-platform upsells in the public listing.

## Listing title (≤ 60 chars)

```
Strategy Falsifier: test backtests against chance
```

## Tagline

```
An offline, MIT skill that tests trading backtests with six reproducible instruments and verdict rules declared before any result is read.
```

## Short description (≤ 300 chars)

```
Feed a strategy spec and OHLCV CSV. The offline engine runs conservative path-aware replay, 1x/2x/5x fee stress, barrier checks, random-entry controls, walk-forward folds and a parameter grid. It returns FALSIFIED / NOT_FALSIFIED / INCONCLUSIVE with evidence and next experiments. No API keys or live orders.
```

## Long description (listing page)

```
WHAT THIS IS

A complete, free strategy-falsification skill for agents. It does not search for profitable strategies or generate trading signals. It tests a strategy's stated edge against pre-declared rules and reports what the evidence does and does not support.

Six instruments run on the supplied strategy signals and OHLCV candles:

1. Path-aware replay: bars are walked, not sampled. If stop-loss and take-profit are both touched inside one bar, the conservative default resolves to the stop and prints the optimistic alternative alongside it.
2. Cost sensitivity: the same strategy is replayed at 1x, 2x and 5x the declared fee assumptions.
3. Barrier reachability: checks whether declared take-profit and stop-loss levels are actually reached inside the holding window.
4. Random-entry control: uses the same window, exit rules and costs, with fixed-seed random entries and bootstrap confidence intervals.
5. Walk-forward: evaluates the final declared fold out of sample and reapplies the pre-declared falsification rules.
6. Parameter grid: replays the declared exit-parameter grid to expose brittle results and parameter overfitting.

VERDICT AND DIAGNOSIS

The verdict is FALSIFIED, NOT_FALSIFIED or INCONCLUSIVE. The engine refuses to draw a conclusion when the declared minimum evidence threshold is not met. A NOT_FALSIFIED result means only that the strategy was not disproved under the declared rules; it is not proof of profitability and is never a recommendation to trade.

A seven-outcome diagnosis layer explains whether the evidence points to insufficient data, randomness, cost sensitivity, absent edge, overfitting, barrier reachability or survival under the declared rules. Each diagnosis includes evidence from the current run and a next experiment with an acceptance criterion.

REPRODUCIBLE EXAMPLE

The repository includes a public BTC 1h RSI momentum case with 1,638 trades and a reported +34.35% return over roughly three years. In the same window, the random-entry control differs by only +0.03% per trade and its confidence interval spans zero. Under the pre-declared rule, the strategy is marked FALSIFIED: a positive return during a broad bull market is not enough to show that the entry logic adds measurable value over random entries.

WHAT YOU GET

- A Markdown report with verdict, instrument results, diagnosis and reproduction command.
- Machine-readable JSON containing the measured values.
- Optional self-contained HTML report with charts.
- A comparison mode for multiple strategy variants.
- A bundled deterministic demo that runs without external market-data access.

REQUIREMENTS AND PRIVACY

Python 3.10 or later. The shipped engine uses only the Python standard library. It reads the strategy specification and user-supplied CSV files, writes reports to the requested local paths, makes no network calls, requests no API keys, and does not access trading accounts or place orders.

HONEST LIMITS

The report does not model slippage, partial fills, market impact, capacity, correlated concurrent positions, exchange outages or the user's full parameter-search history. If parameters were tuned on the full data window, an out-of-sample label is not proof of independence. This is a research and verification tool, not investment advice.

Source: https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2
```

## Demo / sample field (≥ 700 chars)

```
Try the bundled deterministic demo first: `python verify.py --demo`. It generates a fixed synthetic OHLCV series and runs the built-in RSI rule, so the same input and seed produce the same output without downloading market data. For a real audit, provide a JSON strategy specification and an OHLCV CSV with columns `t,o,h,l,c`; timestamps are epoch milliseconds. The specification declares the market, timeframe, fee rate, exit rules, minimum fill count, random-control settings, walk-forward folds, parameter grid and falsification conditions before the engine reads any result. The report starts with one of three verdicts: FALSIFIED, NOT_FALSIFIED or INCONCLUSIVE. It then prints path-aware replay with conservative stop/target precedence, fee stress at 1x/2x/5x, barrier reachability, a fixed-seed random-entry control with bootstrap confidence intervals, walk-forward results, and the declared parameter grid. The diagnosis section identifies which evidence pattern was observed and proposes a next experiment with an explicit acceptance criterion. For example, a BTC 1h RSI strategy can show a positive multi-year return and still be marked FALSIFIED if a same-window random-entry control is statistically indistinguishable. This is intentional: the skill measures whether the entry logic adds evidence beyond the market regime, not whether the raw equity curve happens to rise. No API keys, exchange account access, network calls or live orders are needed.
```

## Compatibility

- Python 3.10+.
- Python standard library only; no pip installation.
- Input: strategy JSON plus local OHLCV CSV; output: Markdown / JSON / optional HTML.
- No market-data API, exchange API key, account login or network connection required.

## Permissions

- Reads only the strategy spec and data files supplied by the user.
- Writes the requested report files locally.
- Does not read credentials, connect to exchanges, send orders, or transfer funds.

## Pricing

```
Free — full six-instrument battery, diagnosis layer, demo, Markdown / JSON / HTML reports. No crippled trial and no payment required.
```

## Tags / use cases

Suggested tags: `trading`, `backtest`, `quant`, `due-diligence`, `overfitting`, `audit`, `python`, `offline`.

Use cases:
- Is my backtest curve real or a coincidence?
- Does the strategy survive doubling or quintupling fees?
- Which of several variants is least overfitted?
- Can this strategy beat random entries under the same exits and costs?

## Submission checklist

1. ZIP: `skill/strategy-falsifier/agensi/strategy-falsifier-agensi-v1.0.0.zip`.
2. SHA256: use the adjacent `SHA256SUMS.txt` manifest.
3. Current release ZIP is approximately 68.5 KB, well below Agensi's 50 MB upload limit.
4. Package contains a plain-text `SKILL.md` and one self-contained Python script; no binary payload or external dependency.
5. Do not add Gumroad, Stripe payment, Lemon Squeezy, Toku paid-service or other off-platform checkout links to the Agensi listing.
6. Submit through <https://www.agensi.io/dashboard/submit> under the free listing option. A payout method is not needed for a free listing.

## Reviewer note — script-size recommendation

The bundled `scripts/verify.py` is approximately 187.8 KB (about 4,336 lines). Agensi's guidance recommends keeping each script below 20 KB for reviewability. This package exceeds that recommendation, although it remains far below the 50 MB ZIP limit. The reason for the single-file build is the execution model: scripts run from a temporary file and cannot read sibling files. The script is fully visible, commented, stdlib-only and makes no network calls. Treat this as a review risk to disclose, not something to hide.

## Submission status

The package is already present on `main` and was rebuilt with the free-release sync. The Agensi marketplace submission itself has **not** been made yet.