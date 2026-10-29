# X/Twitter thread 文案（英文，8 条）

发布时间：北京 21:00-23:00（美东上午）。第一条发完 pin。数据全部可复现，链接放最后一条。

---

**1/**
I spent 36 hours measuring a 6-agent trading system.

It had no edge. So I killed it.

Then I kept the instrument that killed it — and it turns out the instrument is worth more than the system.

Thread on what it measures, and the +34% strategy it murdered:

**2/**
The system: search → risk → smart-money → LLM judge → FOMO → exit. Six agents, four parallel experiment arms, WAL ledgers, watchdogs. Real engineering.

The verdict: six agents fully on vs the dumbest rule ("buy top 3 by organic_score") = -94.68 vs -102.75.

The agent stack's incremental value was $8.

**3/**
14 filter queues. Bootstrap CIs on lift.

Every single one crosses zero.

Not one gate in the whole stack demonstrated selectivity. The filters felt smart. The measurement said otherwise.

**4/**
Then I pointed the same instruments at the industry.

- Backtest→live gap: median -14.3pp annual return (Broker Tested Reviews, funded accounts)
- 17 of 22 retail bots = index beta with marketing
- Alpha Arena: 4 of 6 frontier models lost 30-63% in 17 days of real money

The pattern: everyone sells curves nobody can verify.

**5/**
So I built the verifier. Four instruments:

1. Path-aware replay (bar containing stop+target → stop wins; optimistic read printed alongside)
2. Cost sensitivity at 1x/2x/5x fees
3. Barrier reachability (are your TP/SL prices the market actually visits?)
4. Random-entry control (same window, random entries, bootstrap CI)

**6/**
Ran it on 4 pre-declared strategies. Real Binance data, 29k bars each, 2023-2026.

3 were obvious losers.

The 4th made +34% over three years. 1638 trades. Publishable curve.

And the control group beat the explanation out of it: random entries in the same window differ by +0.03%/trade. CI spans zero.

**7/**
+34% in a bull market is what random entry looks like.

That's the whole product thesis: not killing losers (they die on their own) — killing the ones you can't tell from luck.

Verdicts come from falsification rules written BEFORE the numbers exist. Post-hoc rules are marketing.

**8/**
Engine is open source (MIT), 38 tests, offline replay, sample report on real data in the repo.

First 10 strategy-falsification reports free in exchange for publication rights. No signals, no subscriptions, no managed capital — the only question it answers is whether your curve is real.

Repo + case matrix: [link]
