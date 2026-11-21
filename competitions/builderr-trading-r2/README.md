# MimicGuard Trader — builderr Trading Round 2 entry

Original `decide()` agent (standard-library Python, deterministic, no network, no LLM).

- **Regime filter:** SPY/QQQ vs 50/200-day averages + QQQ 20-day volatility and 10-day drawdown → target gross 0.15× / 0.85× / 1.0× (+ small QLD/SSO sleeve only in the strong regime).
- **Selection:** top 6 of a fixed liquid pool by 1- and 2-month momentum, only names above their 50-day average.
- **Sizing:** equal weight, 22% hard cap per name, 4% rebalance band.
- **Risk:** 9% trailing stop from the 20-day high; names leaving the top list are sold.

Limits: long-only, beta-adjusted gross ≤ 1.5×, no position ≥ 30%, ≤ 45 orders/day.

## Local preview (builderr sample windows, illustrative only)

| window | return | max DD |
|---|---|---|
| calm_uptrend | +9.83% | 4.10% |
| moderate_selloff | −2.56% | 5.94% |
| vol_spike_snapback | −9.51% | 11.24% |

All admission checks pass. Parameters moved ±20% (momentum windows, top-N, stop) keep all three windows within a few points — no cliff.

Run: copy `agent.py` into the builderr template and `python preview.py agent.py`.

Author: Zhu Yuyang (@Zhuyuyangyy). MIT License.
