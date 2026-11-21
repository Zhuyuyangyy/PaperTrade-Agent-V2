"""真实数据源适配（Sprint 4 接线）：把 Binance K 线接成 ``Source``。

只是适配层，不是策略：只读公开端点、无 key、超时/重试/断流都抛 ``SourceError``，
由 ``RunLoop`` 计入失败并熔断。``fetch`` 可注入，因此可以完全离线测试。

用法（部署节点）：

    source = BinanceKlineSource(symbol="BTCUSDT")
    loop = RunLoop(root, broker, limits, source, ...)

注意：本模块不判断行情、不产生信号；它只负责"某一时刻能看到哪些已收盘 K 线"。
"""
from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

from research.market.contracts import Candle, TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.klines import from_rows

from .sources import SourceError

FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
_FRAMES = (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))
UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (research; read-only)"}


@dataclass
class BinanceKlineSource:
    symbol: str = "BTCUSDT"
    limit: int = 500
    timeout_s: float = 10.0
    retries: int = 2
    fetch: Callable[[str], object] | None = None
    _stats: dict = field(default_factory=lambda: {"requests": 0, "errors": 0})

    def candles(self, as_of_ms: int) -> Mapping[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        for name, tf in _FRAMES:
            url = f"{FAPI_KLINES}?symbol={self.symbol}&interval={_INTERVAL[name]}&limit={self.limit}"
            rows = self._call(url)
            candles = from_rows(rows if isinstance(rows, list) else [], tf)
            closed = [c for c in candles if c.close_ms <= as_of_ms]
            if closed:
                out[name] = closed
        if "1h" not in out:
            raise SourceError(f"{self.symbol}: 没有可用的 1h 已收盘 K 线")
        return out

    def _call(self, url: str) -> object:
        fn = self.fetch or self._default_fetch
        self._stats["requests"] += 1
        try:
            return fn(url)
        except SourceError:
            self._stats["errors"] += 1
            raise
        except Exception as exc:  # noqa: BLE001
            self._stats["errors"] += 1
            raise SourceError(f"{self.symbol}: 拉取失败 {type(exc).__name__}: {exc}") from exc

    def _default_fetch(self, url: str) -> object:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=self.timeout_s) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 429 and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                    OSError, http.client.HTTPException) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                break
        raise SourceError(f"{self.symbol}: 数据源不可达: {last}")

    def stats(self) -> dict:
        return dict(self._stats)