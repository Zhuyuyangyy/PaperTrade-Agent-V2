# Fast Experiment C

Fast Experiment C is a third, independent 1,000 USDT Freqtrade dry-run account for quicker engineering feedback. It is not part of the primary A/B comparison.

- Timeframe: 15m
- Entry candidates: EMA9 > EMA21 and close > EMA9, or RSI(14) <= 38 with a confirmed bounce above EMA9
- Exit: EMA9 < EMA21 or close < EMA21; ROI target 1.5%; hard stop -2%
- Same approved Kraken spot pairs: BTC/USDT and ETH/USDT
- Same configured fee and 5 bps adverse entry/exit price model
- Local heartbeat: `runtime/fast/fast_heartbeat.json`, written by the actual Freqtrade strategy loop
- Separate DB: `runtime/fast/fast.dryrun.sqlite`
- Separate writable Freqtrade user directory: `runtime/fast/`

The strategy loop writes `fast_heartbeat.json` on each Freqtrade loop. The read-only dashboard also displays fresh Kraken public marks, so it can show live system activity without inventing fills or P&L. This lane may still remain flat when no entry condition is satisfied. Never lower thresholds merely to force trades. Do not pool its results with Baseline A or Agent-assisted B.

Start/stop with the rest of the stack: `docker compose up -d --build fast` and `docker compose stop fast`. To view its state, use the local dashboard; it appears as `Fast Experiment C`.
