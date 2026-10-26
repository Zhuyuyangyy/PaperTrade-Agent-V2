# Security boundaries

1. No KYC, exchange account, API key, deposit, withdrawal, private key or seed phrase is required. Do not add any.
2. `research.worker` only invokes public `ccxt.kraken.load_markets`, `fetch_ohlcv`, and `fetch_ticker`. It has NO exchange order functions. Network requests may still expose your public IP to the data provider.
3. All Freqtrade config files specify `dry_run=true`, spot, empty exchange credentials, empty Telegram credentials, empty API username/password, disabled loopback-only API, no forced entry, and isolated SQLite DBs. The 32+ character `jwt_secret_key` value is an explicitly named non-secret schema placeholder; the API remains disabled and no port is published. Each account has its own writable runtime mount; shared strategies and Hybrid signal inputs are read-only. Each strategy checks paper-mode invariants at startup and final entry confirmation.
4. Research->strategy is one-way, local, strict JSON. It checks ticker, source, schema, closed UTC candle, expiration, future timestamps, votes and risk veto. The research service has no access to private trading APIs or credentials.
5. External LLM text is never executed. The optional TradingAgents adapter accepts only exact action enums; output influences *veto* only, not order size or exchange access.
6. There is NO user authorization or multi-tenant security model. Run on a private host. Do not expose Docker/SQLite data volumes or network endpoints to untrusted users.
7. `docker-compose.yml` exposes no inbound ports. Do not enable a remote API casually. Freqtrade is version-tag pinned to `2026.9`; for publication-grade reproduction, also record/pin the resolved image digest.
8. Treat data and model outputs as untrusted: malformed, missing, delayed or contradictory signals become HOLD/no new entry.
9. Freqtrade Dry-run is an execution simulator, not a guarantee that real fills / slippage / margin / exchange outages would be the same.
10. Real trading is out of scope; any future version would need explicit security, regulatory, permission and independent financial risk review.
