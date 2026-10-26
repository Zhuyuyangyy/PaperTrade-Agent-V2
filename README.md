# PaperTrade-Agent V2 - Independent A/B Dry-run (NO REAL ORDERS)

A reproducible, paper-only comparison of (A) a deterministic 1-hour SMA baseline and
(B) the same baseline filtered by a risk-first multi-voter research committee, plus a separate exploratory 15-minute fast lane C.
All three are standalone Freqtrade **dry-run** accounts using public Kraken OHLCV data; C is excluded from the primary A/B results.
This project does not authenticate or place real exchange orders, does not require
identity verification, and is NOT a profitable-trading claim.

## What is actually integrated

* **Freqtrade**: separate baseline, hybrid, and fast Docker services, 1,000 USDT simulated
  starting capital EACH, BTC/USDT and ETH/USDT spot only, three separate SQLite ledgers
  in isolated writable runtime directories. Fast is an exploratory 15m EMA/RSI lane, not part of A/B. Entries use a 5 bps adverse custom price;
  exit and stoploss use limit orders so Freqtrade actually calls the custom exit-price
  hook. Limit exits can remain unfilled in a fast move, which is recorded as a model
  limitation. Stake is capped at 100 USDT and 10 percent of estimated wallet stake.
* **CCXT**: public-only Kraken OHLCV and ticker worker (no API keys), strict timestamps,
  hourly close/gap checks, atomic last-closed-candle signals, and separate short-lived
  mark-price files for valuing open spot positions.
* **FundAgents-inspired pattern**: rule-based trend and momentum voters, risk veto,
  observable votes, and missing-data fail-closed behavior. This is original integration
  code and does NOT import or modify the user's existing FundAgents repository.
* **TradingAgents**: OPTIONAL upstream research adapter `research/tradingagents_bridge.py`.
  It is NOT enabled by default and has NOT been exercised against live upstream APIs.
  After separate installation and LLM credentials, it can publish a strictly parsed,
  same-day daily research bias to `data/ta_bias`. Set `--ta-policy require_buy` in the
  worker to use that as an EXTRA entry veto; never as autonomous trade authorization.
* **Gate**: `PaperHybrid` can only veto baseline BUY entries; it cannot create trades
  independently. Exact ticker, time, candle, source, schema, vote and risk checks are
  repeated at the final Freqtrade entry authorization. Baseline exits/stoploss stay
  operational regardless of Agent connectivity.
* **Equity reporting**: `scripts/report.py` reads Freqtrade ledgers in read-only mode and
  marks open long positions to fresh public ticker prices, including entry/estimated exit
  fees. If any open position lacks a fresh valid mark, total equity is unavailable rather
  than guessed. `research.equity_sampler` appends 60-second equity snapshots; the dashboard
  plots those full-equity samples rather than reconstructing a realized-only curve.

## First run (Python 3.11 + Docker Compose)

```bash
python scripts/safety_check.py
python -m research.worker demo
python scripts/dashboard.py
python -m pytest -q tests
python -m unittest discover -s legacy_v1/tests -p test_paper_agent.py

# Docker requires access to public Kraken market APIs and images.
docker compose config
# Inspect user_data/config.baseline.json and config.hybrid.json first.
docker compose up -d --build
docker compose logs -f --tail=80 research baseline hybrid fast equity-sampler
```

The demo creates **synthetic** files under `data/synthetic_signals/`, a directory
NOT read by the trading strategy. Real research writes only to
`user_data/signals/`. Do not copy demo files to the production signal directory.
To stop the simulations: `docker compose down`. Account databases live under
`runtime/baseline/`, `runtime/hybrid/`, and `runtime/fast/`; do not delete those directories if you wish to
preserve results. Shared strategies are mounted read-only, and Hybrid reads public
signals from a separate read-only mount.

### Reports

```bash
python scripts/report.py
python scripts/dashboard.py
```

For a live-updating local dashboard, run this in a separate terminal after starting Docker:

```bash
python scripts/dashboard_server.py
```

Open `http://127.0.0.1:8765`. The server binds to localhost only and refreshes the read-only page every 15 seconds from local SQLite ledgers, fresh public marks, equity snapshots, and validated signal files. It has no order or credential endpoints and makes no outbound requests. Stop it with Ctrl+C.

For a one-off HTML snapshot that can be opened directly, run `python scripts/dashboard.py` and open `data/paper_dashboard.html`. The dashboard follows a compact trading-console layout: four KPI cards, full-equity history, activity log, primary baseline-vs-agent comparison, and nine module cards including Fast Experiment C. Click a module card to read its responsibility and safety boundary.

When no ledger exists, metrics show `--` and the chart shows an empty state rather than invented profits. Equity history is sampled once per minute and includes open-position mark-to-market P&L. If any open position lacks a valid public mark no complete-equity value is emitted for that sample. A chart is not a live exchange performance statement. See `docs/UI_DESIGN.md` for layout and data-truthfulness rules.

The HTML dashboard is generated from the current template and local snapshot. No synthetic
balances, wins, trades, or P&L are inserted. The earlier checked-in PNGs showed the previous
realized-only labels, so they are not included as current screenshots in this release.

Fast Experiment C also writes a local heartbeat from Freqtrade's actual `bot_loop_start` callback. The activity feed shows that heartbeat and fresh Kraken public marks, so a flat account can still visibly show that the process and market feed are alive. The heartbeat is not a trade signal and does not change account P&L.

### Optional TradingAgents integration (NOT a default dependency)

Read the upstream documentation at https://github.com/TauricResearch/TradingAgents
and choose supported LLM providers and market data; API subscriptions may cost money.
Install upstream in a separate compatible Python environment. Set the variables
`TA_LLM_PROVIDER`, `TA_QUICK_MODEL`, `TA_DEEP_MODEL` and the vendor's required API key.
Run a daily analysis with:

```bash
python -m research.tradingagents_bridge --pair BTC/USDT
python -m research.tradingagents_bridge --pair ETH/USDT
```

This emits `data/ta_bias/BTC_USDT.json` and `ETH_USDT.json`. To use the optional
veto inside the researcher, set the `research` Docker Compose command option from
`--ta-policy off` to `--ta-policy require_buy`, but remember that the optional
adapter is not installed in the researcher Docker image: run it separately, make
its output accessible at the mounted `data/ta_bias` path, and verify its daily
input data provider. If missing or ambiguous, new Hybrid entries will be blocked.
The trading engine MUST NOT use LLM API keys or handle any secret.

## Upgrade from the earlier V2 folder

Stop the old stack first, but keep the old project directory intact. The migration uses SQLite's backup API and never overwrites an existing target. It prefers current ledgers under `runtime/{baseline,hybrid}/` and falls back to legacy `user_data/*.dryrun.sqlite` files. The fast lane is new and normally has no legacy ledger.

```bash
# First run `docker compose down` in the OLD project directory.
# Then run these commands from the NEW project directory after extracting the new ZIP.
python scripts/migrate_ledgers.py --legacy-root "/path/to/old/PaperTrade-Agent-V2" --target-root .
python scripts/safety_check.py
docker compose config
docker compose up -d --build
```

Use the actual old project path for `--legacy-root`; do not delete or overwrite its `runtime/` folder. Run migration before the new services create target ledgers. A missing old fast ledger is expected and that exploratory account will start fresh.

## Safety invariants

1. All three dry-run config files must keep `dry_run: true`, `trading_mode: spot`,
   empty exchange credentials, and disabled remote API/forced entries.
2. `PaperBaseline.require_paper_mode()` independently rejects live mode and
   unexpected exchange/symbol/stake parameters at startup and before entries.
3. Data is untrusted: missing, stale, out-of-order or demo signals never approve BUY.
4. Historical Freqtrade backtests CANNOT consume the live `signals/*.json` file,
   since that would introduce hindsight. First implement point-in-time signal tape.
5. Local Docker services do not expose ports. Run on a private host, no public UI.
6. Do not add seed phrases, exchange signing keys, transfer tools or credentials.

## Limits, not hidden

* This release was validated using **offline mocked integrations**, not actual
  live Kraken connectivity or full Freqtrade Docker startup in the build environment.
* Freqtrade dry-run models fills with simplifying assumptions, and does not prove
  real exchange execution, profitability or safety. Different research polling
  latency can confound A/B fill comparisons.
* The -20 USDT daily loss cutoff covers **closed-trade realized P&L only**,
  not floating P&L or a full daily total-equity circuit breaker. The equity sampler
  now records marked account equity, but it does not yet stop new entries on a total-equity
  drawdown breach. Freqtrade's built-in protections are additional safeguards, not a
  substitute for a dedicated total-equity circuit breaker.
* LLM agent debate and news/social market reasoning are not enabled in this V2.
  There is a tested, optional handoff contract, not a claimed deployed LLM system.
* No live API, no on-chain wallet, no KYC, no Binance integration.

## Experiment plan and extensions

Read `docs/EXPERIMENT.md` and `docs/SECURITY.md`. Record at least 30 days of
forward paper behavior and enough separate market regimes before comparing strategies.
Compare to BTC/ETH buy-and-hold with the same clock, fees and exposure. Do not treat
synthetic demo, previous months' hindsight, or a single profitable week as evidence.

Export an auditable experiment snapshot before reviewing or sharing results:

```bash
python scripts/export_experiment.py
```

The exporter uses SQLite's backup API for a consistent copy of active ledgers, redacts
exchange credential fields, and records SHA-256 hashes for included configs, strategies,
protocol, public marks, decisions, and equity snapshots. Export archives are ignored by
Git; do not commit runtime ledgers. The preserved original V1 is under `legacy_v1/`.
This V2 is an independent sandbox and makes no changes to any connected GitHub repository.
External projects are dependencies or optional inspirations, not vendored source.


## Fast Experiment C

A separate 15-minute EMA/RSI paper strategy is available for faster feedback on the order-to-ledger path. Its results are exploratory and are explicitly excluded from the primary Baseline A vs Agent-assisted B comparison. See `docs/FAST_LANE.md`.

## CopyWatch D (event-driven on-chain follow simulation)

A/B/C wait for indicator conditions, so a flat line is the correct output of a quiet market.
D is a different experiment: it watches **real DEX swaps** on public pools, promotes wallets by
verifiable on-chain activity, and only records a paper fill when a real buy event passes every
risk gate.

- Signal source: public pool trade API (no key, read-only). See `docs/COPYWATCH.md`.
- Separate ledger and separate cash: `runtime/copywatch/copywatch.sqlite`, 500 USDT, excluded from A/B/C totals.
- Paper fill = pool price **at discovery time** + 5 bps adverse slippage; fees and gas are charged on both sides.
- Every skipped signal is stored with a reason code (`WALLET_UNVERIFIED`, `COST_TOO_HIGH`, ...), so "why no trade" is answerable.
- No wallet, no private key, no signing code, no order endpoint.

```bash
python -m research.copywatch.agent once     # one poll
python -m research.copywatch.agent loop     # 45 s polling
python scripts/copywatch.py status          # equity, open positions, skip breakdown
docker compose up -d copywatch              # same as the other services, no exposed ports
```

Live evidence from the first ten minutes on 2026-10-24 (real public data, not a mock):
613 swaps ingested, 320 wallets observed, 32 promoted, 13 entered risk review, 1 paper fill,
8 skipped for an unverified wallet and 1 rejected for `COST_TOO_HIGH` (500 bps on Ethereum
mainnet vs a 60 bps limit) — the cost gate fired on a real signal from the most active tracked
wallet.


## MemeHunter E (self-directed meme picks)

A/B/C wait for conditions and D copies wallets. E is the third question: **given an asset class
where the program must form its own view, does the research pipeline survive contact with costs?**

Meme tokens are the harshest test bed available: hundreds of candidates per hour, a very high
base rate of structurally broken contracts, and terrible round-trip costs.

- Universe: four Jupiter rankings (`toporganicscore` 1h/5m/24h + `recent`), deduped, ~100-150 tokens per scan.
- **Survival vetoes** (mint/freeze authority, holder concentration, holder count, pool depth, recent
  volume, FDV ceiling, wash-traded volume share, collapse in progress) are absolute.
- Everything else is **scored 0-100** and ranked; only the top of the list is bought. An all-must-pass
  conjunction was tried first and selected nothing, because most meme tokens are defective by nature.
- Paper fill = Jupiter quote **at discovery time** plus slippage estimated from pool depth; 120 bps
  round trip charged on top. Exits: +55% / -22% / 30% peak giveback / 180 min timeout.
- Separate ledger and cash: `runtime/memehunter/memehunter.sqlite`, 1,000 USDT, excluded from every other total.
- Config lives in `user_data/config.memehunter.json`, so thresholds are auditable and tunable without code edits.

```bash
python scripts/memehunter.py scan --dry-run   # candidates and reasons, open nothing
python scripts/memehunter.py scan             # scan, then open qualifying positions
python scripts/memehunter.py update           # mark to market, apply exits
python scripts/memehunter.py status           # equity, cash, open book
python scripts/memehunter.py candidates       # every token from the last scan, with its veto code
```

Design notes and known gaps: `docs/MEMEHUNTER.md`.
