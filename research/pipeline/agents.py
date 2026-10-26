"""Six independent agents, one job each.

The division of labour mirrors the public "Grok bot" breakdown that inspired
this desk, but every step below is our own code against our own paper ledger:

  SEARCH   scans the universe, hands back raw candidates. Never touches money.
  RISK     one-vote survival veto plus a score, hands back survivors. Never
           touches money.
  GMGN     tiers wallets seen in real on-chain swaps (smart-money watchlist).
           Never touches money.
  GROK     orchestration only: it routes targets between agents and records the
           hand-off. It cannot open or close anything.
  FOMO     the ONLY agent allowed to open a position. Every entry carries the
           configured hard stop (default -18%).
  EXIT     the ONLY agent allowed to close a position (take profit, trailing
           giveback, timeout, stop).

That last boundary is the point of the whole file: four agents analyse, two
agents move paper money, and nothing else can.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from ..memehunter import Desk, Token, ensure_candidate_columns, fetch_universe, record_candidates, score, veto
from . import judge as judge_mod

PIPELINE_VERSION = 1

# Wallets whose cumulative observed volume reaches this are treated as "smart
# money" by the GMGN agent. Mirrors copywatch's wallet_policy.whale_usd.
DEFAULT_CONFIG: dict = {
    "schema_version": PIPELINE_VERSION,
    "mode": "dry_run",
    "search": {"max_candidates": 400},
    "risk": {
        "top_n": 3,
        "min_score": 45.0,
        # When the funnel yields nothing for this many rounds, the score
        # floor decays by adaptive_decay per round down to adaptive_floor.
        # The veto list never moves -- it is the survival line.
        "adaptive_dry_rounds": 3,
        "adaptive_decay": 4.0,
        "adaptive_floor": 28.0,
    },
    "smart_money": {"min_notional_usd": 20_000.0, "recent_hours": 24, "limit": 12, "min_net_buyers_1h": 15},
    "entry": {"stop_loss_pct": -18.0, "max_entries_per_round": 2},
    "exit": {"enabled": True},
    # The C account: same candidates, same moment, simplest possible rule.
    "counterpart": {"enabled": True, "top_n": 3},
    # JUDGE: an LLM risk opinion that reorders survivors. It never overrides a
    # veto and never predicts price; it is one more replayable column.
    # Quality gates become a smaller position instead of a rejection. Hard
    # vetoes (authority, concentration, collapse, wash volume) never move.
    "tiers": {
        "probe_enabled": True,
        "probe_notional_usdt": 40.0,
        "probe_min_llm": 55.0,
        # Only these reject reasons are survivable-by-sizing; everything else
        # stays a hard rejection inside veto().
        "probe_reasons": [
            "TOO_FEW_HOLDERS",
            "LIQUIDITY_TOO_THIN",
            "NO_LIQUIDITY_FLOW",
            "TOO_LARGE_TO_BE_MEME",
            "SCORE_BELOW_FLOOR",
        ],
    },
    "judge": {
        "enabled": True,
        "base_url": "https://api.stepfun.com/v1",
        "api_key_env": "STEPFUN_API_KEY",
        "model": "step-5-preview",
        "timeout_seconds": 25,
        "max_per_round": 3,
        "rule_weight": 0.6,
        "llm_weight": 0.4,
    },
}


@dataclass
class Context:
    """Everything an agent is allowed to see for one pipeline round."""

    root: Path
    cfg: dict
    started_ms: int
    desk: Desk
    artifacts: dict = field(default_factory=dict)

    def put(self, key: str, value: Any) -> None:
        self.artifacts[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.artifacts.get(key, default)


@dataclass
class AgentResult:
    agent: str
    ok: bool
    summary: str
    produced: int = 0
    rejected: int = 0
    elapsed_ms: int = 0
    detail: dict = field(default_factory=dict)


class BaseAgent:
    """An agent does one thing and reports what it did. Nothing more."""

    name = "AGENT"

    def run(self, ctx: Context) -> AgentResult:  # pragma: no cover - interface
        raise NotImplementedError

    # ---------- helpers ----------
    def _timed(self, ctx: Context, fn):
        started = time.monotonic()
        try:
            result = fn()
            return result, int((time.monotonic() - started) * 1000), True
        except Exception as exc:  # noqa: BLE001 - an agent failure must not kill the desk
            return {"error": f"{type(exc).__name__}: {exc}"}, int((time.monotonic() - started) * 1000), False


class SearchAgent(BaseAgent):
    """SEARCH: pull the universe and hand back raw candidates."""

    name = "SEARCH"

    def run(self, ctx: Context) -> AgentResult:
        limit = int(ctx.cfg["search"]["max_candidates"])

        def work():
            tokens, errors = fetch_universe()
            # Persist the snapshot, rejects included: the candidates table is the
            # replay engine's only source of truth and must not go quiet when the
            # pipeline is what produced the round.
            scan_id = ctx.desk.conn.execute(
                "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors) "
                "VALUES (?,?,?,?)",
                (ctx.started_ms, len(tokens), 0, json.dumps(errors, ensure_ascii=False)),
            ).lastrowid
            record_candidates(ctx.desk.conn, scan_id, ctx.started_ms, tokens, ctx.desk.cfg)
            return tokens[:limit], errors

        (tokens, errors), elapsed, ok = self._timed(ctx, work)
        ctx.put("tokens", tokens)
        ctx.put("source_errors", errors)
        summary = f"{len(tokens)} 个代币入池"
        if errors:
            summary += f" · {len(errors)} 个数据源异常"
        return AgentResult(
            agent=self.name,
            ok=ok,
            summary=summary,
            produced=len(tokens),
            elapsed_ms=elapsed,
            detail={"sources": len(errors)},
        )


class RiskAgent(BaseAgent):
    """RISK: one-vote survival veto, then a score. No money, no overrides."""

    name = "RISK"

    def run(self, ctx: Context) -> AgentResult:
        tokens: list[Token] = ctx.get("tokens", [])
        top_n = int(ctx.cfg["risk"]["top_n"])
        min_score = float(ctx.cfg["risk"]["min_score"])
        # Adaptive floor: only the soft score requirement moves, never a veto.
        streak = int(ctx.get("dry_streak", 0) or 0)
        trigger = int(ctx.cfg["risk"].get("adaptive_dry_rounds", 3))
        decay = float(ctx.cfg["risk"].get("adaptive_decay", 4.0))
        floor = float(ctx.cfg["risk"].get("adaptive_floor", 28.0))
        effective_min = min_score
        if streak >= trigger:
            effective_min = max(min_score - decay * (streak - trigger + 1), floor)

        def work():
            ranked: list[tuple[float, Token]] = []
            rejects: dict[str, int] = {}
            probe: list[tuple[float, Token, str]] = []
            probe_reasons = set(ctx.cfg.get("tiers", {}).get("probe_reasons", []))
            for token in tokens:
                ok, reason = veto(token, ctx.desk.cfg)
                if not ok:
                    rejects[reason] = rejects.get(reason, 0) + 1
                    if reason in probe_reasons:
                        # A soft gate, not a survival line: keep it as a probe
                        # candidate. veto() already refused it, so it never
                        # reaches tier 1 -- JUDGE decides if it is worth $40.
                        probe.append((score(token, ctx.desk.cfg), token, reason))
                    continue
                value = score(token, ctx.desk.cfg)
                if value < effective_min:
                    rejects["SCORE_BELOW_FLOOR"] = rejects.get("SCORE_BELOW_FLOOR", 0) + 1
                    probe.append((value, token, "SCORE_BELOW_FLOOR"))
                    continue
                ranked.append((value, token))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            probe.sort(key=lambda item: item[0], reverse=True)
            return ranked[:top_n], rejects, effective_min, probe[: int(ctx.cfg.get("tiers", {}).get("probe_pool", 6))]

        (ranked, rejects, effective_min, probe), elapsed, ok = self._timed(ctx, work)
        ctx.put("survivors", ranked)
        ctx.put("rejects", rejects)
        ctx.put("probe_pool", probe)
        ctx.put("effective_min_score", effective_min)
        summary = f"{len(tokens)} → {len(ranked)} 个通过否决与分数线"
        if probe and ctx.cfg.get("tiers", {}).get("probe_enabled"):
            summary += f" · {len(probe)} 个软门槛候选待研判"
        if effective_min < min_score:
            summary += f"（分数线自适应 {min_score:.0f}→{effective_min:.0f}，连续 {streak} 轮空漏斗）"
        if rejects:
            top = sorted(rejects.items(), key=lambda kv: kv[1], reverse=True)[0]
            summary += f" · 主因 {top[0]}×{top[1]}"
        return AgentResult(
            agent=self.name,
            ok=ok,
            summary=summary,
            produced=len(ranked),
            rejected=sum(rejects.values()),
            elapsed_ms=elapsed,
            detail={"rejects": rejects},
        )


class SmartMoneyAgent(BaseAgent):
    """GMGN: tier wallets from real swaps, list what they are buying.

    Read-only against the CopyWatch ledger. A wallet crosses into the smart
    tier once its cumulative observed notional reaches the configured floor, so
    the tier is earned by on-chain behaviour rather than by a hardcoded list.
    """

    name = "GMGN"

    def run(self, ctx: Context) -> AgentResult:
        cfg = ctx.cfg["smart_money"]
        floor = float(cfg["min_notional_usd"])
        db_path = ctx.root / "runtime" / "copywatch" / "copywatch.sqlite"

        def work():
            if not db_path.is_file():
                return [], [], "未发现 CopyWatch 账本"
            uri = f"file:{db_path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            conn.row_factory = sqlite3.Row
            try:
                wallets = list(
                    conn.execute(
                        "SELECT address, network, swaps, buys, sells, notional_usd, tracked"
                        " FROM wallets WHERE notional_usd >= ? ORDER BY notional_usd DESC LIMIT ?",
                        (floor, int(cfg["limit"])),
                    )
                )
                cutoff = int(time.time() * 1000) - int(cfg["recent_hours"]) * 3_600_000
                buying = list(
                    conn.execute(
                        "SELECT symbol, COUNT(*) n, SUM(usd) v FROM swaps"
                        " WHERE kind='buy' AND block_ts_ms >= ? AND usd >= ?"
                        " GROUP BY symbol ORDER BY v DESC LIMIT 6",
                        (cutoff, floor / 4.0),
                    )
                )
            finally:
                conn.close()
            return wallets, buying, ""

        (wallets, buying, note), elapsed, ok = self._timed(ctx, work)

        # The on-chain swap ledger only covers the pools CopyWatch watches
        # (WETH/USDC and friends), which never overlap the meme universe E
        # trades. Reporting "smart money is buying WETH" therefore changed
        # nothing: the boost could never hit a survivor. So this agent also
        # reads a smart-money proxy from the *same* pool E trades -- a high
        # count of independent net buyers in one hour is what a coordinated
        # entry looks like -- and merges the two lists.
        pool = list(ctx.get("tokens", []))
        min_buyers = int(cfg.get("min_net_buyers_1h", 15))
        crowd = sorted(pool, key=lambda t: t.net_buyers_1h, reverse=True)
        from_pool = [
            {"symbol": t.symbol, "n": int(t.net_buyers_1h), "v": round(t.organic_buy_1h, 0)}
            for t in crowd[:6] if t.net_buyers_1h >= min_buyers
        ]
        seen = {str(row["symbol"]).upper() for row in buying}
        merged = list(buying) + [row for row in from_pool if str(row["symbol"]).upper() not in seen]

        ctx.put("smart_wallets", wallets)
        ctx.put("smart_buying", merged)
        ctx.put("smart_from_pool", [row["symbol"] for row in from_pool])
        summary = f"{len(wallets)} 个链上聪明钱地址（≥ ${floor:,.0f}）"
        if from_pool:
            summary += " · 同池多地址抢入 " + ", ".join(str(r["symbol"]) for r in from_pool[:3])
        elif buying:
            summary += " · 在买 " + ", ".join(str(row["symbol"]) for row in buying[:3])
        # A missing CopyWatch ledger is "waiting for data", not an error: the
        # agent still ran, it just had nothing to tier yet.
        if note:
            summary += f" · {note}"
        return AgentResult(
            agent=self.name,
            ok=ok,
            summary=summary,
            produced=len(wallets) + len(from_pool),
            elapsed_ms=elapsed,
            detail={"buying": merged, "from_pool": [row["symbol"] for row in from_pool]},
        )


class JudgeAgent(BaseAgent):
    """JUDGE: an LLM opinion on the survivors, recorded for replay.

    Only reorders. A vetoed candidate is never sent to the model, so the
    survival line stays a rule, not an opinion. No API key or a failed call
    means "no opinion" and the rule score stands.
    """

    name = "JUDGE"

    def run(self, ctx: Context) -> AgentResult:
        cfg = dict(ctx.cfg.get("judge") or {})
        survivors = list(ctx.get("survivors", []))
        probe_pool = list(ctx.get("probe_pool", []))
        if not cfg.get("enabled") or (not survivors and not probe_pool):
            return AgentResult(agent=self.name, ok=True, summary="未启用或无候选可研判",
                               produced=0)
        limit = int(cfg.get("max_per_round", 3))
        judged: dict[str, dict] = {}
        probe = probe_pool

        def work():
            judge_mod.ensure_schema(ctx.desk.conn)
            ensure_candidate_columns(ctx.desk.conn)
            out: dict[str, dict] = {}
            queue = [(v, t, "tier1") for v, t in survivors[:limit]]
            queue += [(v, t, "tier2") for v, t, _r in probe[:limit]]
            for value, token, tier in queue:
                candidate_id = judge_mod.candidate_id_for(ctx.desk.conn, token.mint)
                result = judge_mod.judge_token(token, cfg)
                if result is None:
                    continue
                judge_mod.record_judgment(ctx.desk.conn, token, candidate_id, cfg, result)
                if result.get("ok"):
                    entry = dict(result)
                    entry["tier"] = tier
                    out[token.mint] = entry
                    ctx.desk.conn.execute(
                        "UPDATE candidates SET llm_score=?, llm_flags=?, llm_reason=? WHERE id=?",
                        (result["score"], json.dumps(result.get("flags", []), ensure_ascii=False),
                         result.get("reason", ""), candidate_id),
                    )
            ctx.desk.conn.commit()
            return out

        judged, elapsed, ok = self._timed(ctx, work)
        # _timed turns a raised error into {"error": ...}; never treat that as
        # a judgement, and surface the message instead of crashing on it.
        opinions = {k: v for k, v in judged.items() if isinstance(v, dict) and v.get("ok")}
        ctx.put("llm_judgments", opinions)
        if not ok:
            summary = f"研判失败：{judged.get('error', 'unknown')}"
        else:
            summary = f"研判 {len(opinions)}/{min(limit, len(survivors)) + min(limit, len(probe))} 个候选"
            if opinions:
                best = max(opinions.values(), key=lambda r: r.get("score", 0))
                summary += f" · 最高 {best.get('score')} 分"
                flags = [f for r in opinions.values() for f in (r.get("flags") or [])][:3]
                if flags:
                    summary += " · " + ", ".join(flags)
        return AgentResult(
            agent=self.name,
            ok=ok,
            summary=summary,
            produced=len(opinions),
            elapsed_ms=elapsed,
            detail={"judged": {k: {"score": v.get("score"), "flags": v.get("flags")} for k, v in opinions.items()}},
        )


class EntryAgent(BaseAgent):
    """FOMO: the only agent that opens a position. Hard stop on every entry."""

    name = "FOMO"

    def run(self, ctx: Context) -> AgentResult:
        survivors = list(ctx.get("survivors", []))
        max_entries = int(ctx.cfg["entry"]["max_entries_per_round"])

        # GROK routing: a survivor that smart money is also buying gets a score
        # boost, so the GMGN agent's output genuinely changes what FOMO does.
        # The boost never overrides a RISK veto -- it only reorders survivors.
        boost = float(ctx.cfg["smart_money"].get("score_boost", 6.0))
        buying = {str(row["symbol"]).upper() for row in ctx.get("smart_buying", [])}
        judgments = ctx.get("llm_judgments", {}) or {}
        judge_cfg = ctx.cfg.get("judge") or {}
        tiers_cfg = ctx.cfg.get("tiers") or {}
        notional = float(ctx.cfg["entry"].get("notional_usdt", 100.0))
        probe_quota = int(tiers_cfg.get("max_probe_positions", 2))
        probe_notional = float(tiers_cfg.get("probe_notional_usdt", 40.0))
        probe_min_llm = float(tiers_cfg.get("probe_min_llm", 55.0))
        boosted = []
        for value, token in survivors:
            opinion = judgments.get(token.mint)
            llm_score = float(opinion["score"]) if opinion and opinion.get("ok") else None
            base = judge_mod.blend(value, llm_score, judge_cfg)
            mark = base + (boost if token.symbol.upper() in buying else 0.0)
            boosted.append((mark, base, token, llm_score, notional))
        # Tier 2: soft-gate survivors, probe size, and only with a good opinion.
        # A quota caps how much of the book may be probes at once, so a run of
        # soft-gate tokens cannot crowd out full-size convictions.
        probe_reason: dict[str, str] = {}
        if tiers_cfg.get("probe_enabled"):
            for value, token, reason in ctx.get("probe_pool", []):
                opinion = judgments.get(token.mint)
                if not opinion or not opinion.get("ok"):
                    continue
                llm_score = float(opinion["score"])
                if llm_score < probe_min_llm:
                    continue
                mark = judge_mod.blend(value, llm_score, judge_cfg)
                boosted.append((mark, mark, token, llm_score, probe_notional))
                probe_reason[token.mint] = reason
        ctx.put("probe_reason", probe_reason)
        boosted.sort(key=lambda item: item[0], reverse=True)

        def work():
            opened, skipped = [], []
            probes_opened = 0
            for mark, base, token, llm_score, size in boosted:
                if len(opened) >= max_entries:
                    skipped.append({"symbol": token.symbol, "reason": "ROUND_QUOTA"})
                    continue
                if size < notional and probes_opened >= probe_quota:
                    skipped.append({"symbol": token.symbol, "reason": "PROBE_QUOTA"})
                    continue
                why = f"PIPELINE·rule={base:.1f}"
                if llm_score is not None:
                    why += f"·llm={llm_score:.0f}"
                if mark > base:
                    why += "·SMART_MONEY"
                if size < notional:
                    why += f"·PROBE${size:.0f}({ctx.get('probe_reason', {}).get(token.mint, 'soft_gate')})"
                result = ctx.desk.open_position(token, why, notional=size)
                if result.get("opened"):
                    result["score"] = round(base, 1)
                    result["llm_score"] = llm_score
                    result["notional"] = size
                    result["tier"] = "probe" if size < notional else "full"
                    result["smart_boosted"] = mark > base
                    if size < notional:
                        probes_opened += 1
                    opened.append(result)
                else:
                    skipped.append({"symbol": token.symbol, "reason": result.get("reason")})
            return opened, skipped

        (opened, skipped), elapsed, ok = self._timed(ctx, work)
        ctx.put("entries", opened)
        summary = f"开仓 {len(opened)} 笔"
        probes = [e for e in opened if e.get("tier") == "probe"]
        if probes:
            summary += f" · 其中 {len(probes)} 笔试探仓（软门槛 + LLM 放行）"
        if any(e.get("smart_boosted") for e in opened):
            summary += " · 含聪明钱加成"
        if skipped:
            summary += " · 跳过 " + ", ".join(f"{s['symbol']}:{s['reason']}" for s in skipped[:3])
        return AgentResult(
            agent=self.name,
            ok=ok,
            summary=summary,
            produced=len(opened),
            rejected=len(skipped),
            elapsed_ms=elapsed,
            detail={"entries": opened, "skipped": skipped},
        )


class ExitAgent(BaseAgent):
    """EXIT: the only agent that closes a position (trailing giveback etc.)."""

    name = "EXIT"

    def run(self, ctx: Context) -> AgentResult:
        if not ctx.cfg["exit"]["enabled"]:
            return AgentResult(agent=self.name, ok=True, summary="已禁用")

        def work():
            return ctx.desk.update()

        result, elapsed, ok = self._timed(ctx, work)
        closed = result.get("closed", []) if isinstance(result, dict) else []
        marked = result.get("marked", 0) if isinstance(result, dict) else 0
        ctx.put("exits", closed)
        summary = f"标记 {marked} 仓 · 平仓 {len(closed)} 笔"
        if closed:
            summary += " · " + ", ".join(f"{c['symbol']} {c['reason']}" for c in closed[:3])
        return AgentResult(
            agent=self.name,
            ok=ok,
            summary=summary,
            produced=len(closed),
            elapsed_ms=elapsed,
            detail={"result": result},
        )


AGENT_ORDER = [SearchAgent, RiskAgent, SmartMoneyAgent, JudgeAgent, EntryAgent, ExitAgent]


def load_config(path: Path) -> dict:
    """Merge the pipeline config over the defaults. Paper only, always."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    return cfg


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
