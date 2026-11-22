"""历史 5m 行情：按时间分页、缓存、校验，遵守全局请求预算。

只访问 Binance 公开只读 klines 端点；不用账户接口、不下单。
缓存命中后完全离线，回放阶段零 REST。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from typing import Callable

from research.market.budget import GLOBAL_BUDGET
from research.market.contracts import Candle

KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
STEP_MS = 300_000


@dataclass
class BarQuality:
    requested_days: int = 0
    bars: int = 0
    gaps: int = 0
    max_gap_ms: int = 0
    first_open_ms: int = 0
    last_open_ms: int = 0
    duplicate_open_ms: int = 0
    cache_hit: bool = False
    requests: int = 0

    def to_dict(self) -> dict:
        return {
            "requested_days": self.requested_days, "bars": self.bars,
            "gaps": self.gaps, "max_gap_ms": self.max_gap_ms,
            "first_open_ms": self.first_open_ms, "last_open_ms": self.last_open_ms,
            "duplicate_open_ms": self.duplicate_open_ms,
            "cache_hit": self.cache_hit, "requests": self.requests,
        }


@dataclass
class BarSet:
    bars: list[Candle]
    quality: BarQuality = field(default_factory=BarQuality)


def _fetch_page(start_ms: int, limit: int = 1000,
                fetch: Callable[[str], object] | None = None) -> list[list]:
    url = f"{KLINE_URL}?symbol=BTCUSDT&interval=5m&limit={limit}&startTime={start_ms}"
    if fetch is not None:
        return list(fetch(url) or [])
    for attempt in range(4):
        GLOBAL_BUDGET.acquire()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=20) as resp:
                return list(json.load(resp))
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                GLOBAL_BUDGET.note_rate_limited()
            if exc.code in (418, 429) and attempt < 3:
                continue
            raise
    return []


def load_bars(*, days: int = 60, cache: Path,
              fetch: Callable[[str], object] | None = None,
              end_ms: int | None = None) -> BarSet:
    """从缓存读，或分页拉取到一个连续时间窗。cache 命中时不发请求。"""
    q = BarQuality(requested_days=days)
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        rows = payload.get("rows", [])
        bars = [_row_to_candle(r) for r in rows]
        q.cache_hit = True
        q.bars = len(bars)
        return _validate(bars, q)

    end = int(end_ms or (time.time() * 1000))
    end = end // STEP_MS * STEP_MS
    start = end - days * 86_400_000
    start = start // STEP_MS * STEP_MS
    rows: dict[int, list] = {}
    cursor = start
    for _ in range(500):
        page = _fetch_page(cursor, 1000, fetch=fetch)
        q.requests += 1
        if not page:
            break
        for r in page:
            rows[int(r[0])] = list(r)
        nxt = int(page[-1][0]) + STEP_MS
        if nxt <= cursor or nxt >= end:
            break
        cursor = nxt
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "symbol": "BTCUSDT", "interval": "5m", "start_ms": start, "end_ms": end,
        "fetched_at_ms": int(time.time() * 1000),
        "rows": [rows[k] for k in sorted(rows)],
    }, ensure_ascii=False), encoding="utf-8")
    return _validate([_row_to_candle(rows[k]) for k in sorted(rows)], q)


def _row_to_candle(r: list) -> Candle:
    return Candle(int(r[0]), D(str(r[1])), D(str(r[2])), D(str(r[3])),
                  D(str(r[4])), D(str(r[5])), STEP_MS)


def _validate(bars: list[Candle], q: BarQuality) -> BarSet:
    q.bars = len(bars)
    if not bars:
        return BarSet(bars, q)
    q.first_open_ms = int(bars[0].open_ms)
    q.last_open_ms = int(bars[-1].open_ms)
    seen = set()
    for a, b in zip(bars, bars[1:]):
        if int(b.open_ms) in seen:
            q.duplicate_open_ms += 1
        seen.add(int(a.open_ms))
        gap = int(b.open_ms - a.open_ms)
        if gap > STEP_MS:
            q.gaps += 1
            q.max_gap_ms = max(q.max_gap_ms, gap)
    return BarSet(bars, q)
