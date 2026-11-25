"""SEC EDGAR 适配器：重点是防前视——reportDate 绝不能当市场可知时刻。"""
from decimal import Decimal
from pathlib import Path

import pytest

from research.intel.contracts import ADVISORY_ONLY
from research.intel.sec_edgar import (CIK_MAP, extract_8k, fetch_submissions,
                                      format_utc, latency_ms, to_news_events)

ACCEPTED = "2026-11-23T13:57:54.000Z"
ACCEPTED_MS = 1795442274000


def _payload():
    return {
        "name": "NVIDIA CORP", "cik": 1045810,
        "filings": {"recent": {
            "form": ["8-K", "10-Q", "8-K/A", "8-K"],
            "filingDate": ["2026-11-23", "2026-11-20", "2026-11-18", "2026-11-12"],
            "reportDate": ["2026-11-22", "2026-11-19", "2026-11-17", "2026-11-11"],
            "acceptanceDateTime": [ACCEPTED, "2026-11-20T12:00:00.000Z",
                                   "2026-11-18T10:00:00.000Z",
                                   "2026-11-12T13:52:41.000Z"],
            "accessionNumber": ["0001045810-26-000090", "0001045810-26-000089",
                                "0001045810-26-000088", "0001045810-26-000082"],
            "primaryDocument": ["nvda-20261122.htm", "q.htm", "amend.htm",
                                "nvda-20261111.htm"],
            "items": ["5.02,9.01", "", "2.02,9.01", "5.02,9.01"],
        }},
    }


def _target():
    """按 accession 精确取 11/23 复职那笔，不依赖排序位置。"""
    return [f for f in extract_8k(_payload())
            if f["accession"] == "0001045810-26-000090"][0]


def test_cik_registered():
    assert CIK_MAP["NVDA"] == "0001045810"


def test_acceptance_timestamp_exact():
    f = _target()
    assert f["accepted_ms"] == ACCEPTED_MS
    assert format_utc(f["accepted_ms"]).startswith("2026-11-23 13:57:54")


def test_report_date_is_day_precision_only():
    """决议日 11/22 vs SEC 接受 11/23 —— 两者不能混为一谈。"""
    f = _target()
    assert f["report_date"] == "2026-11-22"
    assert f["accepted_utc"].startswith("2026-11-23")
    # 决议日按 UTC 00:00，市场可知时刻晚约 1.6 天
    assert f["accepted_ms"] - f["report_day_ms"] > 86_400_000


def test_news_event_separation_prevents_lookahead():
    """核心测试：occurred_at 早于 first_seen；择时只能用 first_seen。"""
    f = _target()
    ev = to_news_events([f], symbol="NVDA")[0]
    assert ev.occurred_at_ms == f["report_day_ms"]
    assert ev.first_seen_ms == f["accepted_ms"]
    assert ev.first_seen_ms > ev.occurred_at_ms
    assert ev.sources[0].published_at_ms == f["accepted_ms"]
    assert ev.sources[0].first_seen_ms == f["accepted_ms"]


def test_event_is_advisory_only_and_neutral():
    f = _target()
    ev = to_news_events([f], symbol="NVDA")[0]
    assert ev.advisory_only is ADVISORY_ONLY is True
    assert ev.direction == "neutral"
    assert ev.credibility == Decimal("1.0")
    assert "official" in ev.tags and "5.02" in ev.tags


def test_filter_forms_and_since():
    rows = extract_8k(_payload(), since="2026-11-20")
    forms = [r["form"] for r in rows]
    assert forms == ["8-K"], forms
    plain = extract_8k(_payload(), forms=("8-K",))
    assert [r["form"] for r in plain] == ["8-K", "8-K"], plain
    assert all("8-K/A" not in r["form"] for r in plain)


def test_sorted_by_accepted_not_report_date():
    rows = extract_8k(_payload(), forms=("8-K", "8-K/A"))
    assert [r["accepted_ms"] for r in rows] == sorted(r["accepted_ms"] for r in rows)


def test_latency_positive_when_reference_later():
    f = _target()
    ev = to_news_events([f], symbol="NVDA")[0]
    # 财联社 2026-11-24 10:20 北京时间 = 02:20 UTC
    ref = 1795486800000
    d = latency_ms(ev, ref)
    assert d > 0
    assert d < 2 * 86_400_000


def test_fetch_uses_cache_without_network(tmp_path: Path):
    cache = tmp_path / "sec.json"
    cache.write_text(__import__("json").dumps(_payload()), encoding="utf-8")

    def forbidden(url):
        raise AssertionError("缓存命中时不得发请求")

    got = fetch_submissions("0001045810", fetch=forbidden, cache=cache)
    assert got["name"] == "NVIDIA CORP"


def test_fetch_uses_injected_fetch(tmp_path: Path):
    seen = []

    def fake(url):
        seen.append(url)
        return _payload()

    got = fetch_submissions("0001045810", fetch=fake, cache=tmp_path / "sec.json")
    assert got["cik"] == 1045810
    assert seen and "1045810.json" in seen[0]
