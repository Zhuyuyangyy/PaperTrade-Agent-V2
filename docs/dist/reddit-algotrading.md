# r/algotrading 发帖文案（Show 风格，英文）

**发帖时机**：美东上午（北京 21:00-23:00）。**账号要求**：先有几天正常评论历史再发；链接放评论区，正文讲方法论——直接甩链接是 spam 的典型姿势。

---

**Title**: I ran a 6-agent trading system for 36 hours, measured that it had no edge, killed it — then productized the instrument that killed it

**Body**:

Short version: I built a six-agent paper-trading pipeline (search → risk → smart-money → LLM judge → FOMO → exit, with four parallel experiment arms). After 36 hours the measurement said the whole thing had no edge, so I stopped it. Then I kept the instrument.

What the instrument found (all reproducible from the repo):

- Six agents fully on vs the dumbest rule ("buy the top 3 by organic_score"): -94.68 vs -102.75 over the same window. The agent stack's incremental value was about 8 dollars.
- 14 filter queues, bootstrap CIs on lift: every single one crosses zero. Not one gate demonstrated selectivity.
- Gross per trade +18bps against a 50bps LP fee floor — structurally negative, independent of pool depth.

The instruments (all open source, all offline-replayable):

1. **Path-aware replay.** Bars are walked, not sampled. A bar containing both stop and target resolves to the stop by default; the optimistic read is printed alongside. This matters more than it sounds: one repo's own notes show +469%/91% win from "did price touch TP", vs +132%/PF 1.62 from path-aware replay of the same signals. Same signals, 3.5x apart.
2. **Cost sensitivity.** Replay at 1x/2x/5x the assumed fee. Measured here: $100 order into a median-$799k pool costs 60bps round trip, 50bps of which is an LP floor. The 120bps in the config was priced for a $1,000 order — wrong ruler, not wrong number.
3. **Barrier reachability.** Are your TP/SL levels prices the market actually visits? In one test case the -8% stop was touched in 4.9% of windows. That "stop loss" was not working.
4. **Random-entry control.** Same window, same exits, same costs, entries drawn at random, bootstrap CI on the difference. TrendRider (8468 trades, PF 0.84) and CascadeFader (PF 1.12) both fail this.

Four pre-declared cases, all published including the ugly ones:

| Case | Trades | Net | vs random control | Verdict |
|---|---|---|---|---|
| RSI dip-buy BTC 1h | 1310 | -505.65% | refuted (-0.37%) | FALSIFIED |
| RSI momentum BTC 1h | 1638 | **+34.35%** | **unproven (+0.03%)** | FALSIFIED |
| RSI dip-buy ETH 1h | 1419 | -1144.10% | refuted (-0.80%) | FALSIFIED |
| Same, maker limits | 1301 | -453.20% | refuted (-0.40%) | FALSIFIED |

The second row is the one I care about. +34% over three years looks publishable. But a random-entry control in the same window differs by +0.03%/trade with a CI spanning zero. 2023-06 to 2026-10 was a crypto bull run — random entries made money too. That curve sells beta packaged as alpha. (For reference, Broker Tested Reviews' live-funded tests found 17 of 22 retail bots were just index beta, and a median 14.3pp gap between backtest and live returns.)

Verdicts come from `falsify_if` rules written into the spec before any number is read. Sample report on real cached Binance data: `docs/verify-sample-report.md`. Case matrix: `docs/cases/`.

I'm now offering the first 10 strategy-falsification reports free in exchange for publication rights, because I'd rather build the public case-study library than argue. No signals, no subscriptions, no managed capital — the only question the tool answers is whether your curve is real.

Repo: [link in comment]

Happy to answer anything about the methodology, especially where it's weakest (no walk-forward, no capacity modelling, one-price-series-at-a-time).

---

**评论区补充**（有人问"那你自己的 edge 是什么"时用）：

My edge claim is narrow and falsifiable: I'm better at killing strategies than at finding them. If that's worth money to someone, they'll pay for the kill. If it isn't, the tool stays open source and I keep it as portfolio work. Either way I'm not selling a curve.
