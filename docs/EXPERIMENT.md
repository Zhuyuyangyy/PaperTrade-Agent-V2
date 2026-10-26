# Experiment protocol (pre-registered before seeing future results)

## Question
Does a closed-candle rule-based research committee, optionally augmented by TradingAgents as an additional entry veto, improve *risk-adjusted* 1h spot paper trading results over an identical SMA20/SMA50 baseline?

## Common controls

- Same exchange endpoint (Kraken), symbols (BTC/USDT, ETH/USDT), candle granularity (1h), start time, virtual cash (1000 USDT each), fill/fee assumptions, stake limits and protective exits.
- Two independent Dry-run Freqtrade wallets/databases. Keep both online concurrently. Avoid editing a strategy after the pre-registration; use a new experiment ID instead.
- Hybrid **cannot** create BUYs the baseline strategy does not propose. This makes the experiment a clean *entry veto* study.
- Strategy parameters: SMA20/SMA50, closed candles only, maximum 100 USDT entry stake, 2 open positions, 3% stoploss.

## Metrics

1. Net equity change (including unrealized P&L) relative to starting cash, with marked positions valued consistently.
2. Maximum peak-to-trough drawdown of equity snapshots.
3. Realized trade P&L, win rate, number of trades, average hold time, fees and approximate slippage.
4. Baseline vs Hybrid exposure-hours (a veto changes market exposure, an important confounder).
5. Relative performance against buy-and-hold BTC/ETH equal-weighted, with stated rebalancing assumptions.
6. Agent veto rate and reasons; no-signal rate; worker uptime and restart count; latency from candle close to signal publish.
7. If TradingAgents is enabled: LLM prompts/tokens/costs, data-as-of time and exchange/Yahoo discrepancy.

## No-lookahead policy

- Offline historical testing must only use information that existed by candle close. External news must be timestamped by *publication/receipt*, not inferred retroactively.
- The shipped Hybrid live file is **not** a historical signal tape. Do not use it for historical backtests.
- Future replay requires versioned, point-in-time JSONL records and a replay adapter indexed by original candle timestamp.

## Stop/review conditions

- Public OHLCV stale, gaps, wrong pair, negative/malformed prices, or unavailable API: no new Hybrid entry.
- Duplicate/replayed/malformed JSON: no new Hybrid entry.
- Missing TradingAgents bias when `require_buy`: no new Hybrid entry.
- Day realized loss <=-20 USDT: block new entries on each account; exiting positions remains allowed. Fast C is excluded from the primary A/B result set.
- Longer term: add a total-equity drawdown kill switch (the sampler is measurement only; the existing closed-trade guard is not enough).
- After >=30 days and several market regimes, review out-of-sample metrics. Do not infer profitability from demo or a few trades.

## Protocol amendment — 2026-10-24 (before the first recorded fills)

This amendment is recorded before evaluating any trading outcome. If either ledger already
contains a fill in the operator's live workspace, do not silently mix this protocol with
that run; export the old run and start a new run ID.

- **Adverse fill model:** 5 bps on entry and 5 bps on exit for approved long spot pairs.
  Freqtrade's `custom_entry_price` hook is used for entries. In the pinned Freqtrade
  implementation, `custom_exit_price` is only invoked for limit exits, so exit and stoploss
  order types are limit. This makes the hook active, but marketable limits can remain
  unfilled during fast moves; unfilled order count and exit delay must be reviewed alongside
  P&L. This is a fixed approximation, not an order-book impact model.
- **Equity valuation:** every 60 seconds, the equity sampler records starting cash plus
  realized P&L plus open-position mark-to-market P&L after entry and estimated exit fees.
  Mark prices must come from `ccxt-public:kraken`, be positive, and be no older than five
  minutes. If any open position lacks a fresh valid mark, that account's sample is marked
  incomplete and excluded from the plotted equity curve. The UI must not fall back to a
  realized-only curve while labelling it total equity.
- **Account isolation:** Baseline and Hybrid have separate writable runtime directories and
  separate SQLite ledgers. Strategy code is mounted read-only; Hybrid receives research
  signals through a read-only mount. The public mark directory is read-only for the sampler.
- **Runtime pinning:** Freqtrade is pinned to image tag `2026.9`, not the mutable `stable` tag.
- **Reproducibility:** do not commit active SQLite databases or public mark snapshots to Git.
  Use `python scripts/export_experiment.py` to produce a redacted ZIP with SQLite-backup
  copies, config/strategy/protocol hashes, signals, marks, decisions and equity snapshots.

The sampler provides a measured equity series, but no total-equity drawdown kill switch is
claimed by this amendment. The daily realized-loss guard still only considers closed-trade
P&L. These limitations must remain visible in reports.


## Protocol amendment — 2026-10-24: exploratory fast lane C

The primary experiment remains the pre-registered 1h comparison between Baseline A and Agent-assisted B. A third isolated account, Fast Experiment C, is added only to shorten engineering feedback cycles. It uses 15m candles, EMA9/EMA21 trend entries, confirmed RSI(14) rebound entries, a 1.5% ROI target, and a -2% hard stop. It retains the same Kraken public market, 0.1% configured fee, and 5 bps adverse price hooks.

C has its own writable `runtime/fast/` directory and `fast.dryrun.sqlite` ledger. C is exploratory only: its trades, P&L, win rate, and drawdown must not be pooled into the primary A/B results. It may be used to test signal-to-order-to-ledger plumbing and to collect a separate strategy sample. More frequent decisions do not imply profitability; the faster timeframe is more exposed to noise and fee/slippage drag.
