# Trade Status Panel

This is an incremental dashboard/strategy telemetry patch. It must be merged into the current deployed working tree; do not replace the current project with an older full archive.

## What the panel shows

- **Last fill:** reads the most recent non-null `orders.order_filled_date` from each account's own read-only Freqtrade SQLite database. If the database or `orders` table is unavailable, it says so rather than claiming there were no trades.
- **Active pair locks:** reads the `pairlocks` table, filters `active=1`, and independently checks that `lock_end_time` is still in the future. If the table cannot be read, the status is `unknown`, not `unlocked`.
- **Next strategy evaluation:** computes the next UTC 1-hour boundary for A/B and 15-minute boundary for C, then displays it in the browser's local timezone. This is a scheduled candle boundary, **not a prediction that a signal will occur**.
- **Runtime heartbeat:** the strategies atomically publish a tiny local JSON heartbeat only in `dry_run` mode. The dashboard validates strategy identity, timeframe, mode, and freshness.
- **Pair strategy status:** A/B/C publish their latest real per-pair `BUY` candidate or `HOLD` reason. These status files are telemetry only and are not consumed by order authorization.

## Files changed

- `scripts/dashboard.py`
- `templates/dashboard.html`
- `user_data/strategies/PaperBaseline.py`
- `user_data/strategies/PaperHybrid.py`
- `user_data/strategies/PaperFast.py`
- `tests/test_dashboard.py`
- `tests/test_strategies.py`

The patch intentionally does **not** change strategy entry/exit prices, order types, account configs, or SQLite ledgers. Keep any newer slippage and limit-order fixes already present in the deployed branch.

## Apply to the current project

From the project root, inspect first:

```powershell
git status --short
git apply --check trade-status-panel.patch
```

If the patch check passes:

```powershell
git apply trade-status-panel.patch
pytest -q
python scripts/safety_check.py
docker compose config
docker compose up -d --force-recreate baseline hybrid fast
```

Then refresh the existing local dashboard. If `git apply --check` reports a conflict because the working tree has moved since the patch base, do not reset or overwrite the project; merge the same additions into the current files and preserve all existing fixes.

## Safety boundaries

- The dashboard remains read-only and loopback-only.
- Telemetry write failures are swallowed so they cannot block trading logic.
- Strategy status files are not execution signals.
- Missing/stale heartbeat, missing lock table, or unknown database schema are displayed as unknown/unavailable.
- No live API credentials, order endpoints, or real orders are introduced.
