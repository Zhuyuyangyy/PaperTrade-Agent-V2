"""新闻情报（Sprint 3）——只做结构化与去重，不做自由推理。

规则：
  - 去重键 = 归一化标题 + 来源 + 事件发生分钟；同一事件重复抓取只保留最早可见的一条；
  - ``first_seen_ms`` 是"我们第一次看到它"的时间，任何研究都只能用它做可得性门禁，
    不能用出版时间假装当时已知；
  - 文本只当数据：疑似注入只标记，不执行；
  - 可信度由来源数量与来源类型给出（调用方可覆盖），缺来源 → 0。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from .contracts import Fact, NewsEvent, SourceRef
from .text_safety import detect_injection, extract_facts, sanitize_text, text_hash

_WS = re.compile(r"\s+")


@dataclass
class NewsStore:
    """内存新闻库（Sprint 3 离线研究用；接线 sqlite 时保持同一接口）。"""

    _by_key: dict[str, NewsEvent] | None = None

    def __post_init__(self) -> None:
        self._by_key = {}

    def add(self, *, headline: str, raw_text: str, occurred_at_ms: int,
            first_seen_ms: int, sources: tuple[SourceRef, ...],
            credibility: Decimal | None = None, horizon_ms: int = 24 * 3_600_000,
            tags: tuple[str, ...] = ()) -> NewsEvent | None:
        clean = sanitize_text(headline)
        norm = _WS.sub(" ", clean).strip().lower()
        if not norm:
            return None
        publisher = sources[0].publisher if sources else "unknown"
        minute = occurred_at_ms // 60_000
        key = hashlib.sha256(f"{norm}|{publisher}|{minute}".encode("utf-8")).hexdigest()[:16]
        if key in self._by_key:
            return None
        injected, _hits = detect_injection(raw_text or headline)
        facts: tuple[Fact, ...] = extract_facts(raw_text or headline)
        cred = credibility if credibility is not None else _default_credibility(sources)
        event = NewsEvent(
            event_id=key,
            headline=clean,
            facts=facts,
            occurred_at_ms=occurred_at_ms,
            first_seen_ms=first_seen_ms,
            sources=sources,
            credibility=cred,
            horizon_ms=horizon_ms,
            tags=tags,
            injection_suspected=injected,
            advisory_only=True,
        )
        self._by_key[key] = event
        return event

    def events(self, as_of_ms: int, horizon_ms: int | None = None) -> tuple[NewsEvent, ...]:
        """只返回在 as_of 时刻已可见、且仍在窗口内的事件。"""
        out = []
        for e in self._by_key.values():
            if e.first_seen_ms > as_of_ms:
                continue
            h = horizon_ms if horizon_ms is not None else e.horizon_ms
            if as_of_ms - e.occurred_at_ms > h:
                continue
            out.append(e)
        return tuple(sorted(out, key=lambda e: (e.occurred_at_ms, e.event_id)))

    def injection_flags(self) -> tuple[NewsEvent, ...]:
        return tuple(e for e in self._by_key.values() if e.injection_suspected)

    def count(self) -> int:
        return len(self._by_key)


def _default_credibility(sources: tuple[SourceRef, ...]) -> Decimal:
    if not sources:
        return Decimal("0")
    base = Decimal("0.5") + Decimal("0.25") * min(len(sources), 2)
    return min(Decimal("1"), base)


def headline_hash(raw: str) -> str:
    return text_hash(raw)