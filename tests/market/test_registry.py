"""市场准入测试（Sprint 1）：过期 / 规则不明 / 视界不匹配全部 fail-closed。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from research.cross_market.market_mapping import MarketMapping
from research.market.registry import assess, assess_all, usable

NOW = 1_795_000_000_000
DAY = 86_400_000


def _iso(offset_hours: float) -> str:
    dt = datetime.fromtimestamp(NOW / 1000, tz=timezone.utc) + timedelta(hours=offset_hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _m(**over) -> MarketMapping:
    base = MarketMapping(
        market_id=over.pop("market_id", "m"),
        underlying_asset="BTC",
        event_threshold=100_000.0,
        event_comparator="ABOVE",
        event_expiry="2026-12-31T00:00:00Z",
        resolution_source="uma",
        target_horizons=("5m",),
        primary_horizon="5m",
    )
    return replace(base, **over) if over else base


def test_short_horizon_market_is_researchable() -> None:
    a = assess(_m(event_expiry=_iso(6)), NOW)         # 6 小时后到期
    assert a.researchable is True and a.horizon_match is True
    assert a.reasons == ("ok",)


def test_long_dated_market_fails_horizon_match() -> None:
    a = assess(_m(event_expiry=_iso(24 * 200)), NOW)
    assert a.researchable is False
    assert a.horizon_match is False
    assert any("horizon_mismatch" in r for r in a.reasons)


def test_expired_market_not_researchable() -> None:
    a = assess(_m(event_expiry="2020-01-01T00:00:00Z"), NOW)
    assert a.researchable is False
    assert "market_expired" in a.reasons or "market_expired_by_clock" in a.reasons


def test_expiry_unknown_fail_closed() -> None:
    a = assess(_m(event_expiry="不是日期"), NOW)
    assert a.researchable is False and "expiry_unparseable" in a.reasons


def test_missing_resolution_source_fail_closed() -> None:
    a = assess(_m(resolution_source="  ", event_expiry=_iso(6)), NOW)
    assert a.researchable is False and "resolution_source_missing" in a.reasons


def test_unknown_primary_horizon_fail_closed() -> None:
    a = assess(_m(primary_horizon="7m", event_expiry=_iso(6)), NOW)
    assert a.researchable is False
    assert any("unknown_primary_horizon" in r for r in a.reasons)


def test_usable_filters_all_three_conditions() -> None:
    mapping = {
        "ok": _m(market_id="ok", event_expiry=_iso(6)),
        "long": _m(market_id="long", event_expiry=_iso(24 * 200)),
        "bad": _m(market_id="bad", event_expiry="不是日期"),
        "nores": _m(market_id="nores", resolution_source="", event_expiry=_iso(6)),
    }
    assert list(usable(mapping, NOW)) == ["ok"]
    assert len(assess_all(mapping, NOW)) == 4


def test_ratio_is_reported_in_days() -> None:
    a = assess(_m(event_expiry=_iso(6)), NOW)
    assert a.days_to_expiry is not None and a.days_to_expiry > 0
    assert a.to_dict()["horizon_ratio"] is not None


def test_repo_config_is_rejected_for_short_horizon_research() -> None:
    """仓库现有映射全是 2026 年底到期的长期事件，与 5m 视界不匹配。

    这条测试是审计结论的回归：不是"市场存在就能研究"。等 Sprint 1 Track A
    换成短周期市场后，这条会失败——那正是提醒更新此断言的时刻。
    """
    from pathlib import Path

    from research.cross_market.market_mapping import load_mapping

    cfg = Path(__file__).resolve().parents[2] / "config" / "market_mapping.yaml"
    if not cfg.exists():
        return
    assessments = assess_all(load_mapping(cfg), NOW)
    assert assessments
    for a in assessments:
        assert a.researchable is False
        assert any(("horizon_mismatch" in r) or ("market_expired" in r) for r in a.reasons), a.reasons