"""数据源适配（Sprint 4）：把"某一时刻能看到的 K 线"抽象出来。

循环只依赖这个接口，因此 72h 加速回放与真实采集可以无差别替换：

    source.candles(as_of_ms) -> {"5m": [...], "15m": [...], "1h": [...], "4h": [...]}

约束：返回的 K 线必须是 ``close_ms <= as_of_ms`` 的（已收盘），
未收盘的数据不允许出现在这里——过滤在 source 内完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from research.market.contracts import Candle, TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.demo import synthetic_1m
from research.market.klines import resample

_FRAMES = (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))


class Source(Protocol):
    def candles(self, as_of_ms: int) -> Mapping[str, list[Candle]]: ...


class SourceError(RuntimeError):
    """数据源不可用（断流/返回空/字段异常）——循环据此累计失败并熔断。"""


@dataclass
class CandlesSource:
    """预聚合的本地数据源：用于 72h 加速回放与离线测试。

    基础周期可以是 1m 或 1h（测试常用 1h 减少数据量）；只聚合 >= 基础周期的
    目标周期，低周期缺失是事实，不伪造。
    """

    base_1m: list[Candle]
    _views: dict[str, list[Candle]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not self.base_1m:
            raise SourceError("base candles 为空")
        base_ms = self.base_1m[0].timeframe_ms
        self._views = {}
        for name, ms in _FRAMES:
            if ms < base_ms:
                continue
            if ms == base_ms:
                self._views[name] = list(self.base_1m)
            else:
                candles, _ = resample(self.base_1m, ms)
                if candles:
                    self._views[name] = candles

    def candles(self, as_of_ms: int) -> Mapping[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        for name, candles in self._views.items():
            closed = [c for c in candles if c.close_ms <= as_of_ms]
            if closed:
                out[name] = closed
        if not out:
            raise SourceError(f"as_of={as_of_ms} 没有任何已收盘 K 线")
        return out


@dataclass
class DemoSource:
    """Sprint 4 demo：用 market.demo 的合成 regime，按时间推进。"""

    regime: str = "trend_up"
    seed: int = 7

    def __post_init__(self) -> None:
        self._inner = CandlesSource(synthetic_1m(self.regime, seed=self.seed))

    def candles(self, as_of_ms: int) -> Mapping[str, list[Candle]]:
        return self._inner.candles(as_of_ms)


@dataclass
class FlakySource:
    """测试用：指定轮次抛错，其余转发。"""

    inner: Source
    fail_at: set[int] = field(default_factory=set)
    calls: int = 0

    def candles(self, as_of_ms: int) -> Mapping[str, list[Candle]]:
        self.calls += 1
        if self.calls in self.fail_at:
            raise SourceError(f"injected outage at call {self.calls}")
        return self.inner.candles(as_of_ms)