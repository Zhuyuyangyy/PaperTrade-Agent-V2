"""Jev（TypeSafe 风格）作为 DecisionProvider。

协议按公开资料：请求 ``{model, state, questions}``，判断在 ``answers`` 里；
``noul`` 是"信息不足"的答案类型，映射为 HOLD，**不是** LONG/SHORT 的同义词。
endpoint / key 由操作者提供；没有可验证访问权限时保持 UNAVAILABLE，不为它付费
也不让它阻塞实验。

响应兼容：
  - ``answers`` 为 dict（按 question 索引）或 list（每项含 question/answer）；
  - 旧式 ``output`` 也接受；
  - ``choice/answer/decision`` 支持 LONG/SHORT/HOLD/BUY/SELL/NOUL 等写法。
"""
from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal

from ..contracts import CHOICES, Decision, MarketSnapshot, unavailable

UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (research; read-only)"}
QUESTIONS = ("regime", "news_alignment", "trade_or_hold")


def _default_fetch(url: str, api_key: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        **UA,
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _normalize_choice(v) -> str | None:
    s = str(v or "").strip().upper()
    if s in ("LONG", "BUY", "BULLISH", "YES"):
        return "LONG"
    if s in ("SHORT", "SELL", "BEARISH", "NO"):
        return "SHORT"
    if s in ("HOLD", "NOUL", "NO_DATA", "UNCERTAIN", "UNKNOWN", "FLAT", "ABSTAIN"):
        return "HOLD"
    return None


def _as_answer(a) -> dict:
    if isinstance(a, str):
        return {"choice": a}
    if isinstance(a, dict):
        return {
            "choice": a.get("choice", a.get("answer", a.get("decision"))),
            "score": a.get("score", a.get("probability", 0.0)),
            "confidence": a.get("confidence", a.get("probability", 0)),
            "rationale": a.get("rationale", a.get("reason", "")),
            "evidence": a.get("evidence", []),
        }
    return {}


def _extract(resp) -> dict:
    if not isinstance(resp, dict):
        return {}
    answers = resp.get("answers")
    if isinstance(answers, dict):
        for key in ("trade_or_hold", "trade", "decision"):
            if key in answers:
                return _as_answer(answers[key])
        if answers:
            return _as_answer(next(iter(answers.values())))
    if isinstance(answers, list):
        for a in answers:
            if isinstance(a, dict) and "trade" in str(a.get("question", "")).lower():
                return _as_answer(a)
        if answers:
            return _as_answer(answers[0])
    out = resp.get("output")
    if isinstance(out, dict):
        return _as_answer(out)
    return _as_answer(resp)


class JevProvider:
    name = "jev"
    version = "typesafe-questions-v1"

    def __init__(self, endpoint: str = "", api_key: str = "", model: str = "jev",
                 timeout_s: float = 20.0, fetch=None):
        self.endpoint = endpoint.strip()
        self.api_key = api_key.strip()
        self.model = model
        self.timeout_s = timeout_s
        self.fetch = fetch or _default_fetch

    def available(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        if not self.available():
            return unavailable(self.name, self.version,
                               "JEV_UNAVAILABLE: endpoint/api key not configured")
        body = {"model": self.model, "state": snapshot.summary(), "questions": list(QUESTIONS)}
        t0 = time.time()
        try:
            payload = self.fetch(self.endpoint, self.api_key, body, self.timeout_s)
            data = _extract(payload)
            choice = _normalize_choice(data.get("choice"))
            if choice is None:
                raise ValueError(f"choice={data.get('choice')!r}")
            score = max(-1.0, min(1.0, float(data.get("score", 0.0) or 0.0)))
            conf = Decimal(str(data.get("confidence", "0") or "0"))
            conf = max(Decimal("0"), min(Decimal("1"), conf))
            rationale = str(data.get("rationale", ""))[:160]
            evidence = tuple(str(x)[:60] for x in (data.get("evidence") or [])[:4])
        except Exception as exc:  # noqa: BLE001
            return Decision(self.name, self.version, "ERROR", "HOLD",
                            rationale=f"jev_error: {type(exc).__name__}",
                            latency_ms=int((time.time() - t0) * 1000))
        return Decision(self.name, self.version, "OK", choice, score, conf, rationale,
                        evidence, int((time.time() - t0) * 1000))