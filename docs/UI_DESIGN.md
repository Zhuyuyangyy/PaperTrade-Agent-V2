# UI redesign: PaperTrade Agent dashboard

## Goal

The dashboard uses a bright, compact trading-console layout inspired by the user's reference: four KPI cards, an equity-history panel, a structured activity log, an A/B comparison strip, and nine module cards. The aim is to make the system understandable at a glance rather than to emphasize a decorative animation.

## Open-source references reviewed

- [IAMMARBIT/AgentHUD](https://github.com/IAMMARBIT/AgentHUD) — agent identity/status cards and visual observability.
- [agentic-oleki/agent-monitor](https://github.com/agentic-oleki/agent-monitor) — human-readable agent activity feed and status labels.
- [masakav3/Agent-Office-Dashboard](https://github.com/masakav3/Agent-Office-Dashboard) — visual states tied to agent activity.
- [Freqtrade FreqUI](https://www.freqtrade.io/en/stable/freq-ui/) — reference for the underlying trading engine's own detailed status UI.

These repositories are design references only; their code and assets are not copied into this project.

## Layout

- Header: product identity, Paper Trading, three accounts (A/B plus exploratory C), Dry Run, and local-snapshot status.
- KPI row: combined current equity (only when all three accounts are fully valued), total P&L, marked-equity drawdown, and closed-trade win rate; A/B comparison remains limited to A and B.
- Main row: A/B sampled account-equity history including unrealized P&L, and a filterable activity/status log.
- Comparison strip: baseline versus agent-assisted total-equity change, shown only when both local ledgers and all open-position marks are complete.
- Module row: Market Radar, Strategy Lab, Risk Guard, Paper Executor, Replay Analyst, Baseline Bot, Agent Bot, and Trade Journal.
- Responsive layouts: desktop, tablet, and mobile.

## Data truthfulness and limitations

- The HTML is self-contained and uses no external fonts, CDNs, network calls, order endpoints, or credentials.
- Missing ledgers and missing trades render as `--` or an empty state; the dashboard never seeds fictitious balances, profits, win rates, or trade events.
- The chart is built from the append-only `data/equity_snapshots.jsonl` journal. Each complete point includes configured starting cash, closed-trade realized P&L, and open-position mark-to-market P&L net of entry and estimated exit fees.
- A public ticker mark must be sourced from `ccxt-public:kraken`, be positive, and be no older than five minutes. If any open position cannot be valued, the account total is marked incomplete and that sample is not plotted; the UI never silently substitutes realized-only P&L.
- The drawdown KPI is calculated from complete sampled total-equity points since the sampler began, not from a reconstructed full account history before sampling started.
- Activity rows are generated from local ledger entries, validated signal files, and the current local snapshot status. They are not a real-time exchange websocket.
- The page is read-only. For auto-refresh, run `python scripts/dashboard_server.py` and open `http://127.0.0.1:8765`; for a static file, run `python scripts/dashboard.py` again after the bots write new ledger/signal data.

## Implementation

- `templates/dashboard.html` is the source template.
- `scripts/dashboard.py` embeds a validated read-only snapshot into the template for direct file opening.
- `scripts/dashboard_server.py` binds to `127.0.0.1` only and regenerates that snapshot per page request; the page refreshes every 15 seconds. It has no order/credential endpoints and makes no outbound requests.
- `scripts/report.py` reads Freqtrade SQLite in read-only mode and computes current realized/unrealized P&L using fresh public marks.
- `research/equity_sampler.py` records full-equity snapshots every 60 seconds. It has read-only access to both SQLite ledgers and can write only the snapshot journal.
- `data/paper_dashboard.html` is generated output.
