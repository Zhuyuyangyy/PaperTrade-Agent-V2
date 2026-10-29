"""The ladder: Replay -> Dry-run -> Probe -> Pilot -> Scale, as a state machine
whose every transition rule is written down before it is needed.

The pattern this encodes was learned the hard way across this repo's lineage:
a strategy that looked profitable in replay and lost money live (the
pump.fun momentum bot's own published finding) is not bad luck -- it is a
missing gate. So the gates live here, in configuration, before anyone is
excited about anything.

Four rules the machine enforces, in order:

1. RULES ARE PINNED FIRST. Thresholds come from a config file; nothing in
   this module reads the market's opinion of them. An arm without a
   falsify rule can never be retired -- same idea, higher stakes.
2. DEMOTION IS AUTOMATIC, PROMOTION IS NOT. A trigger fires and the ladder
   steps down (or halts) without asking. Stepping up requires an explicit
   human confirm -- the machine can lose confidence on its own but never
   gains it.
3. HALT OUTRANKS EVERYTHING. A halt freezes the book where it stands.
4. EVERY VERDICT CARRIES ITS REASON. An audit that cannot say why it
   promoted something is theatre.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

TIERS = ("replay", "dryrun", "probe", "pilot", "scale")

DEFAULT_RULES: dict[str, dict] = {
    "replay": {  # offline measurement; a pass here earns nothing but a dry-run
        "min_trades": 60, "min_pf": 1.2, "min_expectancy": 0.0,
        "max_drawdown": 1.0, "min_win_rate": 0.0, "max_unfilled": 1.0,
    },
    "dryrun": {  # live data, simulated fills, no money
        "min_trades": 100, "min_pf": 1.1, "min_expectancy": 0.0,
        "max_drawdown": 0.10, "min_win_rate": 0.0, "max_unfilled": 0.25,
    },
    "probe": {  # real money, an amount whose loss is a rounding error
        "min_trades": 20, "min_pf": 1.1, "min_expectancy": 0.0,
        "max_drawdown": 0.05, "min_win_rate": 0.35, "max_unfilled": 0.25,
        "capital_usd": 100.0,
    },
    "pilot": {  # real money, small
        "min_trades": 40, "min_pf": 1.15, "min_expectancy": 0.0005,
        "max_drawdown": 0.05, "min_win_rate": 0.40, "max_unfilled": 0.20,
        "capital_usd": 500.0,
    },
    "scale": {  # only reached by explicit human decision, never automatically
        "min_trades": 100, "min_pf": 1.2, "min_expectancy": 0.001,
        "max_drawdown": 0.08, "min_win_rate": 0.45, "max_unfilled": 0.15,
    },
}

# Conditions that fire regardless of the tier's statistics. A broken feed is
# not a strategy problem, but it is absolutely a reason to stop trading.
HALT_RULES = {
    "max_consecutive_losses": 8,
    "max_data_gap_s": 300,       # no fresh market data for 5 minutes
    "reconciliation_tolerance": 0,  # any unmatched fill between book and venue
}


@dataclass
class Verdict:
    """The outcome of one evaluation, with the reason it reached it."""

    action: str            # 'hold' | 'promote' | 'demote' | 'halt'
    reason: str
    stats: dict = field(default_factory=dict)


@dataclass
class Ladder:
    """One strategy's position on the ladder, and the evidence for it."""

    name: str
    tier: str = "replay"
    rules: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_RULES.items()})
    trades: list = field(default_factory=list)     # net fractions, chronological
    unfilled: int = 0
    signals: int = 0
    halted: bool = False
    halt_reason: str = ""
    history: list = field(default_factory=list)    # every verdict ever rendered
    capital_usd: float = 0.0
    peak_equity: float = 0.0

    # ---------- recording ----------
    def record(self, net_pct: float, *, filled: bool = True, equity: float | None = None) -> None:
        self.signals += 1
        if not filled:
            self.unfilled += 1
            return
        self.trades.append(float(net_pct))
        if equity is not None:
            self.peak_equity = max(self.peak_equity, equity)

    # ---------- statistics ----------
    def stats(self) -> dict:
        n = len(self.trades)
        if not n:
            return {"n": 0, "unfilled": self.unfilled, "signals": self.signals}
        wins = [x for x in self.trades if x > 0]
        losses = [x for x in self.trades if x <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        # longest losing streak: the number that decides whether a human can
        # actually sit through this strategy at 3am
        streak = worst = 0
        for x in self.trades:
            streak = streak + 1 if x <= 0 else 0
            worst = max(worst, streak)
        return {
            "n": n,
            "signals": self.signals,
            "unfilled": self.unfilled,
            "unfilled_rate": self.unfilled / max(self.signals, 1),
            "win_rate": len(wins) / n,
            "expectancy": sum(self.trades) / n,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "worst_streak": worst,
            "best": max(self.trades),
            "worst": min(self.trades),
        }

    # ---------- evaluation ----------
    def evaluate(self, *, equity: float | None = None,
                 data_age_s: float | None = None,
                 reconciliation_gap: int = 0) -> Verdict:
        """Render one verdict. Halt first, then demote, then promote, else hold."""
        s = self.stats()

        # ---- halt conditions: checked before any statistic matters ----
        if reconciliation_gap > HALT_RULES["reconciliation_tolerance"]:
            return self._verdict("halt", f"对账不平：{reconciliation_gap} 笔本地账本与交易所不匹配", s)
        if s.get("worst_streak", 0) >= HALT_RULES["max_consecutive_losses"]:
            return self._verdict("halt", f"连续 {s['worst_streak']} 笔亏损，触及停机线", s)
        if data_age_s is not None and data_age_s > HALT_RULES["max_data_gap_s"]:
            return self._verdict("halt", f"行情数据中断 {data_age_s:.0f} 秒", s)

        rule = self.rules.get(self.tier, {})
        tier_index = TIERS.index(self.tier)

        # ---- demotion: automatic, no permission asked ----
        # The sample gate for demotion counts SIGNALS, not fills: an unfilled
        # order is itself evidence about execution, and a maker strategy that
        # fills only 75% of its signals should not get a free pass for having
        # fewer than min_trades "trades". Promotion still uses fills -- losing
        # confidence is meant to be easier than gaining it.
        if s.get("signals", 0) >= rule.get("min_trades", 0) and s["n"] > 0:
            if s["profit_factor"] < rule.get("min_pf", 0):
                return self._verdict("demote", f"PF {s['profit_factor']:.2f} 低于本层门槛 {rule['min_pf']}", s)
            if s["expectancy"] < rule.get("min_expectancy", 0.0):
                return self._verdict("demote", f"笔均净 {s['expectancy']*100:+.3f}% 低于本层门槛", s)
            if s["win_rate"] < rule.get("min_win_rate", 0.0):
                return self._verdict("demote", f"胜率 {s['win_rate']*100:.0f}% 低于本层门槛", s)
            if s["unfilled_rate"] >= rule.get("max_unfilled", 1.0):
                return self._verdict("demote", f"未成交率 {s['unfilled_rate']*100:.0f}% 达到本层上限 {rule['max_unfilled']*100:.0f}%", s)
            if equity is not None and self.peak_equity > 0:
                dd = 1 - equity / self.peak_equity
                if dd > rule.get("max_drawdown", 1.0):
                    return self._verdict("demote", f"回撤 {dd*100:.1f}% 超过本层上限 {rule['max_drawdown']*100:.0f}%", s)

        # ---- promotion: the machine reports eligibility; a human confirms ----
        if s.get("n", 0) >= rule.get("min_trades", 0) and tier_index < len(TIERS) - 1:
            nxt = TIERS[tier_index + 1]
            nxt_rule = self.rules.get(nxt, {})
            if (s["profit_factor"] >= nxt_rule.get("min_pf", 0)
                    and s["expectancy"] >= nxt_rule.get("min_expectancy", 0.0)
                    and s["win_rate"] >= nxt_rule.get("min_win_rate", 0.0)
                    and s["unfilled_rate"] <= nxt_rule.get("max_unfilled", 1.0)):
                return self._verdict("promote", f"{s['n']} 笔样本满足 {nxt} 层全部门槛，等待人工确认", s)

        return self._verdict("hold", f"样本 {s.get('n', 0)}/{rule.get('min_trades', 0)} 笔，继续积累", s)

    def _verdict(self, action: str, reason: str, stats: dict) -> Verdict:
        if action == "halt" and not self.halted:
            self.halted = True
            self.halt_reason = reason
        v = Verdict(action=action, reason=reason, stats=stats)
        self.history.append({"tier": self.tier, "action": action, "reason": reason,
                             "at_ms": int(time.time() * 1000)})
        return v

    # ---------- transitions ----------
    def demote(self) -> str:
        """Step down one tier. Automatic; never requires permission."""
        i = TIERS.index(self.tier)
        if i == 0:
            return self.tier          # already at the bottom; nothing to demote to
        self.tier = TIERS[i - 1]
        return self.tier

    def halt(self) -> None:
        self.halted = True
        self.halt_reason = self.halt_reason or "manual halt"

    def promote(self, *, confirm: bool = False) -> tuple[bool, str]:
        """Step up one tier -- only with an explicit human confirmation."""
        if self.halted:
            return False, "已停机，先解除停机再谈升级"
        i = TIERS.index(self.tier)
        if i == len(TIERS) - 1:
            return False, "已在最高层"
        if not confirm:
            return False, "升级需人工确认：机器可以自己失去信心，不能自己获得信心"
        self.tier = TIERS[i + 1]
        return True, self.tier

    # ---------- persistence ----------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, name: str) -> "Ladder":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(name=name)
        data.pop("name", None)
        return cls(name=name, **data)
