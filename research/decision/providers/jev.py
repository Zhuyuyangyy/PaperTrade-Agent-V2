"""Jev（TypeSafe 风格）作为 DecisionProvider。

Jev 的公开 API 细节没有在这里硬编码：endpoint / model / key 由调用方提供，
默认没有配置 → UNAVAILABLE。响应必须是有边界的类型化判断（choice/score/
confidence/rationale），而不是一段自由文本。

注意：Jev 返回的 confidence **不是**市场收益概率；它只用于研究对照。
"""
from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal

from ..contracts import CHOICES, Decision, MarketSnapshot, unavailable

UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (research; read-only)"}


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


class JevProvider:
    name = "jev"
    version = "typesafe-v1"

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
        body = {"model": self.model, "input": snapshot.summary()}
        t0 = time.time()
        try:
            payload = self.fetch(self.endpoint, self.api_key, body, self.timeout_s)
            data = payload.get("output", payload) if isinstance(payload, dict) else {}
            choice = str(data.get("choice", data.get("decision", ""))).upper()
            if choice == "NOUL":
                choice = "HOLD"
            if choice not in CHOICES:
                raise ValueError(f"choice={choice}")
            score = max(-1.0, min(1.0, float(data.get("score", 0.0))))
            conf = Decimal(str(data.get("confidence", "0")))
            conf = max(Decimal("0"), min(Decimal("1"), conf))
            rationale = str(data.get("rationale", data.get("reason", "")))[:160]
            evidence = tuple(str(x)[:60] for x in (data.get("evidence") or [])[:4])
        except Exception as exc:  # noqa: BLE001
            return Decision(self.name, self.version, "ERROR", "HOLD",
                            rationale=f"jev_error: {type(exc).__name__}",
                            latency_ms=int((time.time() - t0) * 1000))
        return Decision(self.name, self.version, "OK", choice, score, conf, rationale,
                        evidence, int((time.time() - t0) * 1000))