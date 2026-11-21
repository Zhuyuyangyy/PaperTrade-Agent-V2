"""多周期 K 线测试（Sprint 1）：只吃收盘、对齐、缺口、聚合完整性。"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from research.market.contracts import Candle, TF_1H_MS, TF_5M_MS
from research.market.klines import (
    aggregate,
    closed_only,
    ensure_contiguous,
    gaps,
    load_csv,
    resample,
)

ONE_MIN = 60_000
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _c(open_ms: int, close: str = "100", tf: int = ONE_MIN, high: str = "101",
       low: str = "99", open_: str = "100") -> Candle:
    return Candle(open_ms, Decimal(open_), Decimal(high), Decimal(low),
                  Decimal(close), Decimal("1"), tf)


def _write_csv(path: Path, rows: list[tuple]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for r in rows:
            w.writerow(r)


def test_candle_rejects_unaligned_and_inconsistent() -> None:
    with pytest.raises(ValueError):
        _c(BASE + 1)
    with pytest.raises(ValueError):
        Candle(BASE, Decimal("100"), Decimal("99"), Decimal("98"), Decimal("100"), Decimal("1"), ONE_MIN)


def test_closed_only_filters_unclosed() -> None:
    candles = [_c(BASE), _c(BASE + ONE_MIN), _c(BASE + 2 * ONE_MIN)]
    assert len(closed_only(candles, BASE + 2 * ONE_MIN)) == 2
    assert len(closed_only(candles, BASE + 2 * ONE_MIN, settle_ms=ONE_MIN)) == 1


def test_gaps_reports_missing_bars() -> None:
    candles = [_c(BASE), _c(BASE + ONE_MIN), _c(BASE + 5 * ONE_MIN)]
    g = gaps(candles)
    assert len(g) == 1 and g[0]["missing_bars"] == 3


def test_ensure_contiguous_raises() -> None:
    with pytest.raises(ValueError, match="缺口"):
        ensure_contiguous([_c(BASE), _c(BASE + 2 * ONE_MIN)])


def test_aggregate_5m_from_1m() -> None:
    base = []
    for i in range(10):
        o = Decimal(100 + i)
        c = Decimal(100 + i) + Decimal("0.5")
        base.append(Candle(BASE + i * ONE_MIN, o, c + Decimal("0.5"),
                           o - Decimal("0.5"), c, Decimal("1"), ONE_MIN))
    out, incomplete = aggregate(base, TF_5M_MS)
    assert incomplete == []
    assert len(out) == 2
    first = out[0]
    assert first.open_ms == BASE
    assert first.close == Decimal("104.5")
    assert first.high == Decimal("105.0")
    assert first.low == Decimal("99.5")
    assert first.volume == Decimal("5")


def test_aggregate_drops_incomplete_bucket() -> None:
    base = [_c(BASE + i * ONE_MIN) for i in range(7)]   # 5 + 2
    out, incomplete = aggregate(base, TF_5M_MS)
    assert len(out) == 1
    assert len(incomplete) == 1 and incomplete[0]["bars"] == 2


def test_resample_identity_when_same_tf() -> None:
    base = [_c(BASE, tf=TF_5M_MS), _c(BASE + TF_5M_MS, tf=TF_5M_MS)]
    out, inc = resample(base, TF_5M_MS)
    assert out == base and inc == []


def test_load_csv_drops_unaligned_and_duplicates(tmp_path: Path) -> None:
    p = tmp_path / "k.csv"
    _write_csv(p, [
        (BASE, 100, 101, 99, 100, 1),
        (BASE + 1, 100, 101, 99, 100, 1),          # 未对齐
        (BASE, 100, 102, 99, 101, 2),              # 重复 open_ms
        (BASE + ONE_MIN, 101, 102, 100, 101, 1),
    ])
    res = load_csv(p, ONE_MIN)
    assert len(res.candles) == 2
    assert res.dropped_unaligned == 1
    assert res.dropped_duplicate == 1
    assert res.candles[0].close == Decimal("101")


def test_load_csv_respects_as_of_and_settle(tmp_path: Path) -> None:
    p = tmp_path / "k.csv"
    _write_csv(p, [(BASE, 100, 101, 99, 100, 1),
                   (BASE + ONE_MIN, 100, 101, 99, 101, 1)])
    res = load_csv(p, ONE_MIN, as_of_ms=BASE + 2 * ONE_MIN, settle_ms=ONE_MIN)
    assert len(res.candles) == 1


def test_load_csv_skips_malformed(tmp_path: Path) -> None:
    p = tmp_path / "k.csv"
    _write_csv(p, [(BASE, "abc", 101, 99, 100, 1)])
    res = load_csv(p, ONE_MIN)
    assert res.candles == [] and res.notes


def test_aggregate_rejects_non_multiple() -> None:
    base = [_c(BASE, tf=TF_5M_MS)]
    with pytest.raises(ValueError):
        aggregate(base, TF_5M_MS + 60_000)