"""JUDGE: an LLM risk opinion on surviving candidates, recorded and replayable.

What this is NOT: a price predictor. Nothing here forecasts returns; with 11
labelled candidates that would be fitting noise. What it does is read the same
structured facts a human analyst would (holder distribution, authority flags,
liquidity depth, wash-volume signals, smart-money crowding) and return a
bounded opinion with reasons.

Two hard boundaries:

* It can never override a RISK veto. A veto is a survival line; an LLM opinion
  is a tie-breaker. Vetoed candidates are not even sent to the model.
* Every call is logged with its prompt hash, raw response and latency, so the
  judgement becomes one more replayable column rather than a black box.

Failures degrade to None: the rule score stands and the desk keeps trading.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

JUDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    symbol TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    score INTEGER,
    flags TEXT,
    reason TEXT,
    latency_ms INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    judged_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgments_candidate ON llm_judgments(candidate_id);
CREATE INDEX IF NOT EXISTS idx_judgments_time ON llm_judgments(judged_at_ms);
"""

DEFAULT_CONFIG: dict = {
    "enabled": True,
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
    "model": "deepseek-chat",
    "timeout_seconds": 25,
    "max_per_round": 3,
    # The opinion only reorders survivors; the rule score keeps 60% of the say.
    "rule_weight": 0.6,
    "llm_weight": 0.4,
    "max_tokens": 1500,
}

SYSTEM_PROMPT = """你是保守的 meme 币风控分析师。只依据给定事实判断，不臆测。
输出严格 JSON：{"score": 0-100 的整数, "flags": [字符串], "reason": "不超过80字"}。
score 含义：100=结构健康可小仓，0=明显高危。事实不足时给低分并说明。"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(JUDGE_SCHEMA)
    conn.commit()


def _facts(token) -> str:
    """The structured facts, and only the facts recorded at scan time."""
    return (
        f"symbol={token.symbol} price=${token.price_usd:.8g} "
        f"liquidity=${token.liquidity_usd:,.0f} fdv=${token.fdv_usd:,.0f} "
        f"holders={token.holder_count} top10={token.top_holders_pct:.1f}% "
        f"mint_disabled={token.mint_disabled} freeze_disabled={token.freeze_disabled} "
        f"chg5m={token.chg_5m:+.1f}% chg1h={token.chg_1h:+.1f}% "
        f"organic_score={token.organic_score:.0f} vol1h=${token.vol_1h:,.0f} "
        f"net_buyers_1h={token.net_buyers_1h}"
    )


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _local_override(root: Path | None = None) -> dict:
    """user_data/judge.local.json holds the operator's key on this machine.

    The file is git-ignored; the environment variable still wins when set, so a
    deployed box can override a developer machine without touching the repo.
    """
    base = root or Path(__file__).resolve().parents[2]
    path = base / "user_data" / "judge.local.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve(cfg: dict) -> tuple[str, str, str]:
    """(api_key, base_url, model) with env var first, local file second."""
    import os

    local = _local_override()
    key = os.environ.get(cfg.get("api_key_env", "STEPFUN_API_KEY"), "") or str(local.get("api_key", ""))
    base_url = str(local.get("base_url") or cfg.get("base_url", "https://api.stepfun.com/step_plan/v1"))
    model = str(local.get("model") or cfg.get("model", "step-5-preview"))
    return key, base_url, model


def _extract_json(text: str) -> dict:
    """Last JSON object in a blob of text. Raises when there is none."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


def judge_token(token, cfg: dict) -> dict | None:
    """One judgement, or None when unavailable. Never raises."""
    key, base_url, model = _resolve(cfg)
    if not key:
        return None
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _facts(token)},
        ],
        "temperature": 0.2,
        "max_tokens": int(cfg.get("max_tokens", 1500)),
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    started = _now_ms()
    try:
        with urllib.request.urlopen(request, timeout=float(cfg.get("timeout_seconds", 25))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to "no opinion"
        return None
    latency = _now_ms() - started
    try:
        message = payload["choices"][0]["message"]
        # Reasoning models (step-5-preview) leave content empty and put the
        # answer, plus its working, in reasoning. Take whichever is non-empty
        # and pull the last JSON object out of it.
        content = message.get("content") or ""
        if not content.strip():
            content = message.get("reasoning") or ""
        parsed = _extract_json(content)
        score = int(parsed.get("score", 0))
        score = max(0, min(100, score))
        flags = parsed.get("flags") or []
        flags = [str(f)[:60] for f in flags][:6] if isinstance(flags, list) else []
        reason = str(parsed.get("reason", ""))[:200]
    except (KeyError, IndexError, TypeError, ValueError):
        return {"ok": False, "latency_ms": latency, "raw": json.dumps(payload)[:2000]}
    return {
        "ok": True,
        "score": score,
        "flags": flags,
        "reason": reason,
        "latency_ms": latency,
        "raw": json.dumps(payload)[:2000],
    }


def record_judgment(conn: sqlite3.Connection, token, candidate_id: int | None,
                    cfg: dict, result: dict | None) -> None:
    """Log every call, including the ones that failed or returned nothing."""
    if result is None:
        return
    prompt_hash = hashlib.sha256(_facts(token).encode("utf-8")).hexdigest()[:16]
    conn.execute(
        "INSERT INTO llm_judgments (candidate_id, symbol, model, prompt_hash, response_json,"
        " score, flags, reason, latency_ms, ok, judged_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            candidate_id, token.symbol, cfg.get("model", "?"), prompt_hash,
            result.get("raw", ""), result.get("score"), json.dumps(result.get("flags", []), ensure_ascii=False),
            result.get("reason", ""), int(result.get("latency_ms", 0)), 1 if result.get("ok") else 0,
            _now_ms(),
        ),
    )
    conn.commit()


def candidate_id_for(conn: sqlite3.Connection, mint: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM candidates WHERE mint = ? ORDER BY scanned_at_ms DESC LIMIT 1", (mint,)
    ).fetchone()
    return int(row[0]) if row else None


def blend(rule_score: float, llm_score: float | None, cfg: dict) -> float:
    """Combine rule and opinion. No opinion means the rule score stands."""
    if llm_score is None:
        return rule_score
    w_rule = float(cfg.get("rule_weight", 0.6))
    w_llm = float(cfg.get("llm_weight", 0.4))
    return w_rule * rule_score + w_llm * float(llm_score)
