"""真实数据源适配测试（离线）：只用注入 fetch，不发网络请求。"""
from __future__ import annotations

import pytest

from research.market.contracts import TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.runtime.live import BinanceKlineSource
from research.runtime.sources import SourceError

BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS
B4 = BASE // TF_4H_MS * TF_4H_MS


def _rows(n: int, step: int, base: int = BASE) -> list[list]:
    rows = []
    price = 100.0
    for i in range(n):
        o = price
        price += 0.2
        rows.append([base + i * step, f"{o}", f"{price + 0.3}", f"{o - 0.3}",
                     f"{price}", "10", base + (i + 1) * step, "0", 1, "5", "0", "0"])
    return rows


def _fake(url: str) -> list[list]:
    if "interval=5m" in url:
        return _rows(400, TF_5M_MS)
    if "interval=15m" in url:
        return _rows(400, TF_15M_MS)
    if "interval=4h" in url:
        return _rows(400, TF_4H_MS, base=B4)
    return _rows(400, TF_1H_MS)


def test_maps_binance_rows_to_candles_and_filters_unclosed() -> None:
    src = BinanceKlineSource(fetch=_fake)
    as_of = BASE + 400 * TF_1H_MS - 1      # 最后一根还差 1ms 才收盘
    views = src.candles(as_of)
    assert "1h" in views
    assert all(c.close_ms <= as_of for c in views["1h"])
    assert views["1h"][-1].close_ms < as_of


def test_missing_1h_raises_source_error() -> None:
    src = BinanceKlineSource(fetch=lambda url: [])
    with pytest.raises(SourceError, match="1h"):
        src.candles(BASE + 10 * TF_1H_MS)


def test_fetch_exception_becomes_source_error_and_counts() -> None:
    def boom(url: str):
        raise TimeoutError("net down")

    src = BinanceKlineSource(fetch=boom)
    with pytest.raises(SourceError):
        src.candles(BASE + 10 * TF_1H_MS)
    assert src.stats()["errors"] >= 1


def test_four_timeframes_present_when_data_allows() -> None:
    src = BinanceKlineSource(fetch=_fake)
    views = src.candles(BASE + 400 * TF_1H_MS)
    assert set(views) == {"5m", "15m", "1h", "4h"}
    assert views["4h"][0].timeframe_ms == TF_4H_MS
    assert views["5m"][0].timeframe_ms == TF_5M_MS