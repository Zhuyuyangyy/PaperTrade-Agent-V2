# PaperTrade-Agent V2 — Run Log

## 2026-10-24 — Equity correctness, runtime isolation, and reproducibility

### Changes

1. **Marked equity replaces realized-only curve.** `scripts/report.py` now reads `dry_run_wallet` from each account config, values approved open spot-long positions from fresh `ccxt-public:kraken` ticker marks, includes the recorded entry fee and estimated exit fee, and emits total equity only when every open position has a valid mark no older than five minutes. Missing/stale marks make the total equity unavailable; no realized-only fallback is presented as total equity.
2. **One-minute equity journal.** Added `research.equity_sampler`, which reads both SQLite ledgers in read-only mode and appends account-specific snapshots to `data/equity_snapshots.jsonl`. Incomplete samples are retained for diagnostics but excluded from the chart. The dashboard computes drawdown on full-resolution samples and renders a bounded, extrema-preserving set of points. A missing open-trade close-fee uses the configured `fee`, not zero.
3. **Account filesystem isolation.** Baseline and Hybrid now have separate writable `runtime/baseline` and `runtime/hybrid` mounts. Strategy files are mounted read-only; Hybrid's signal directory is separately mounted read-only; the equity sampler receives read-only ledger and mark mounts. Freqtrade image tag is pinned to `2026.9`.
4. **Experiment export.** Added `scripts/export_experiment.py`; it creates a credential-redacted ZIP with SQLite-backup copies, configs, strategies, protocol, decisions, marks, signals, equity snapshots, SHA-256 hashes and a manifest. Runtime ledgers remain excluded from Git.
5. **Safe ledger migration.** Added `scripts/migrate_ledgers.py` to move old V2 account DBs from `user_data/` into isolated runtime directories using SQLite backup, verify open/closed counts, and refuse to overwrite targets.
6. **Pricing-hook activation audit.** Freqtrade's source calls `custom_exit_price` only for limit exits. Strategy exit and stoploss order types are therefore limit so the 5 bps adverse exit hook is actually invoked. This can leave a limit order unfilled during a fast move; the experiment protocol now records this limitation. Entry custom pricing remains a 5 bps adverse adjustment on approved long spot pairs.
7. **Ticker mark validation.** Marks require an approved pair, positive finite last price, a non-future exchange timestamp, and a non-stale exchange timestamp when the exchange provides one.

### Verification in this package build

- `python -m pytest -q tests`: **57 passed**.
- `python -m unittest discover -s legacy_v1/tests -p test_paper_agent.py`: **6 passed**.
- `python scripts/safety_check.py`: **PASS**.
- Python YAML parse of `docker-compose.yml`: **PASS**, services are `research`, `baseline`, `hybrid`, `equity-sampler`.
- `python scripts/report.py`: correctly reports that the two runtime ledgers are absent in this build workspace; it does not fabricate data.
- `python scripts/dashboard.py`: generated the static dashboard snapshot.
- `python scripts/export_experiment.py --no-dbs`: generated a manifest-backed export archive in the test workspace.

### Not claimed

Docker is not available in this build workspace, so this run log does **not** claim a successful `docker compose up`, live Kraken connectivity, or real Freqtrade-container fill verification for this rebuilt archive. Run `docker compose config`, `python scripts/safety_check.py`, then start the services on the target machine and inspect all four service logs before treating it as an active experiment.

### Remaining limitation

The `-20 USDT` daily loss guard still uses closed-trade realized P&L only. The sampler now records total equity but does not yet enforce a total-equity drawdown kill switch. Limit exits can remain open during fast price moves, so fill rate and exit delay must be reviewed alongside P&L.


## 2026-10-24 — Fast Experiment C

- Added a third isolated 15m exploratory Freqtrade dry-run account with EMA9/EMA21 trend and confirmed RSI(14) rebound entries.
- Preserved the original 1h Baseline A and Agent-assisted B unchanged; C is explicitly excluded from primary A/B statistics.
- Added fast account equity snapshots, dashboard card/curve/log, safety preflight, migration and reproducible export support.
- No forced trades or synthetic P&L are generated; C may remain flat if its rules are not satisfied.

- Added validated Freqtrade-loop heartbeat and fresh public-mark events to the dashboard so a flat account is visibly alive without fabricating trades.


## 2026-10-24 — Fast feedback build verification

- Added the isolated 15m Fast Experiment C account, separate `runtime/fast/` ledger, EMA9/EMA21 trend + confirmed RSI(14) rebound strategy, 1.5% ROI target, and -2% hard stop. The primary 1h A/B strategies were not changed.
- Added a real `bot_loop_start` heartbeat written only in dry-run mode, and validated Kraken mark + heartbeat events in the read-only dashboard. Flat account P&L is not faked or forcibly changed.
- Migration now prefers the current `runtime/{baseline,hybrid,fast}/` ledger paths and falls back to older `user_data/*.dryrun.sqlite` files, preserving the operator's existing A/B ledgers when moving to a new project folder.
- Export redaction now clears exchange, Telegram, and API username/password/JWT fields; runtime API stays disabled and loopback-only with a clearly named non-secret schema placeholder.
- Verification: `pytest -q` **68 passed**; legacy V1 unittest suite **6 passed**; safety preflight **PASS**; Docker Compose YAML parse **PASS**; dashboard JavaScript syntax **PASS**.
- Docker CLI is not available in this build environment, so the new third container and live Kraken connectivity still require on-machine validation.
