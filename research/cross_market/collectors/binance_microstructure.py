"""Binance 微结构采集器（P1/P1.1）——快照入 EventStore。

字段口径（规格 §3/§4/§5）：
  - price/volume：fapi ticker；
  - oi_snapshot：fapi openInterest（当前持仓量，单位=标的数量）；
  - funding：fapi premiumIndex.lastFundingRate；
  - cvd_delta：由 klines 的 taker_buy_base_asset_volume 本地计算
    （cvd_delta = 2×taker_buy − volume），不在服务端 pretend；
    **只用已收盘 K 线**（S0-12：旧实现取 klines[-1]，那是正在形成的 K 线，
    closeTime 在未来，等于用了决策时刻之后的数据）；
  - liquidation_observed_usdt：REST 无公开强平端点 → 恒记 UNKNOWN 覆盖的观测
    （observed_notional=0, coverage=UNKNOWN），绝伪造"全网清算"。

时间戳：event_ts = 数据时间（优先 openInterest.time，其次 premiumIndex.time）；
**不用** 24hr ticker 的 closeTime——它只是滚动窗口结束，不是该报价的观测时间。
ingest_ts = 本地接收。两者都缺 → 抛错（loud），不拿本地时间冒充。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

from ..schemas import BinanceSnapshot
from ..storage import EventStore

FAPI = "https://fapi.binance.com/fapi/v1"
UA = {"User-Agent": "PaperTrade-CrossMarketCollector/0.1 (research; read-only)"}


class CollectorError(RuntimeError):
    """采集失败——重试后仍失败则抛出，禁静默跳过。"""


@dataclass
class MicroConfig:
    timeout_s: float = 10.0
    max_retries: int = 2
    cvd_interval: str = "1m"   # CVD 本地计算所用的 klines 周期
    cvd_lookback: int = 3      # 取最近 N 根（至少 2，保证有一根已收盘）


class BinanceMicroCollector:
    """拉取微结构快照，写 BinanceSnapshot 到 EventStore。fetch_fn 可注入。"""

    def __init__(self, store: EventStore, symbols: list[str], cfg: MicroConfig | None = None,
                 fetch_fn=None):
        self.store = store
        self.symbols = symbols
        self.cfg = cfg or MicroConfig()
        self._fetch = fetch_fn or self._default_fetch
        self.stats = {"snapshots": 0, "errors": 0, "missing_cvd": 0}

    def _default_fetch(self, url: str) -> object:
        for attempt in range(self.cfg.max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=self.cfg.timeout_s) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < self.cfg.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CollectorError(f"HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt < self.cfg.max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise CollectorError(f"数据源不可达: {e}") from e
        raise CollectorError("重试后仍失败")

    # ---------- 单符号快照 ----------

    def snapshot(self, symbol: str, now_ms: int | None = None) -> BinanceSnapshot:
        recv_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        s = symbol.upper()
        try:
            ticker = self._fetch(f"{FAPI}/ticker/24hr?symbol={s}")
            oi = self._fetch(f"{FAPI}/openInterest?symbol={s}")
            premium = self._fetch(f"{FAPI}/premiumIndex?symbol={s}")
            klines = self._fetch(
                f"{FAPI}/klines?symbol={s}&interval={self.cfg.cvd_interval}&limit={self.cfg.cvd_lookback}"
            )
        except CollectorError:
            self.stats["errors"] += 1
            raise
        if not isinstance(ticker, dict) or not isinstance(oi, dict) or not isinstance(premium, dict):
            raise CollectorError(f"{s}: 微结构返回异常")
        return self._assemble(s, ticker, oi, premium, klines if isinstance(klines, list) else [], recv_ms)

    def _assemble(self, symbol: str, ticker: dict, oi: dict, premium: dict,
                  klines: list, recv_ms: int) -> BinanceSnapshot:
        # event_ts：服务端数据时间；两个来源都缺 → loud failure（禁本地时间冒充）
        event_ts, ts_source = self._event_ts(oi, premium)
        price = Decimal(str(ticker.get("lastPrice", "0")))
        volume = Decimal(str(ticker.get("volume", "0")))
        oi_snap = Decimal(str(oi.get("openInterest", "0")))
        cvd = self._cvd_delta(klines, event_ts)
        if cvd is None:
            self.stats["missing_cvd"] += 1
        # 强平：REST 无端点 → UNKNOWN 覆盖的零观测（禁伪造）
        snap = BinanceSnapshot(
            symbol=symbol,
            price=price,
            volume=volume,
            oi_snapshot=oi_snap,
            cvd_delta=cvd,
            liquidation_observed_usdt=Decimal("0"),
            event_ts_ms=event_ts,
            exchange_ts_ms=event_ts,
            ingest_ts_ms=recv_ms,
            raw=json.dumps({"ticker": ticker, "oi": oi, "premium": premium}, ensure_ascii=False)[:2000],
        )
        self.store.append(
            "binance", "snapshot", symbol,
            event_ts_ms=snap.event_ts_ms, ingest_ts_ms=snap.ingest_ts_ms,
            payload={
                "price": str(price),
                "volume": str(volume),
                "oi_snapshot": str(oi_snap),
                "cvd_delta": str(cvd) if cvd is not None else None,
                "funding_rate": str(premium.get("lastFundingRate", "")),
                "liquidation_observed_usdt": "0",
                "liquidation_coverage": "UNKNOWN",  # REST 无强平端点，如实标记
                "event_time_source": ts_source,
            },
            exchange_ts_ms=snap.exchange_ts_ms,
        )
        self.stats["snapshots"] += 1
        return snap

    @staticmethod
    def _event_ts(oi: dict, premium: dict) -> tuple[int, str]:
        for src, payload in (("openInterest.time", oi), ("premiumIndex.time", premium)):
            v = payload.get("time")
            if v is not None:
                try:
                    ts = int(v)
                except (TypeError, ValueError):
                    continue
                if ts > 0:
                    return ts, src
        raise CollectorError("微结构响应缺少服务端时间戳（openInterest.time/premiumIndex.time）")

    @staticmethod
    def _cvd_delta(klines: list, event_ts_ms: int) -> Decimal | None:
        """CVD delta = 2×taker_buy_base − volume，只取**已收盘** K 线。

        已收盘判定：closeTime（index 6）<= event_ts。未收盘 K 线包含决策时刻之后
        的成交，用它算 CVD 就是前视（S0-12）。
        """
        if not klines:
            return None
        closed = []
        for k in klines:
            try:
                if int(k[6]) <= event_ts_ms:
                    closed.append(k)
            except (IndexError, TypeError, ValueError):
                continue
        if not closed:
            return None
        k = closed[-1]
        try:
            volume = Decimal(str(k[5]))
            taker_buy = Decimal(str(k[9]))
        except (IndexError, ArithmeticError):
            return None
        return taker_buy * 2 - volume

    def snapshot_all(self, now_ms: int | None = None) -> list[BinanceSnapshot]:
        return [self.snapshot(s, now_ms) for s in self.symbols]


def oi_deltas_from_store(store: EventStore, symbol: str, now_ms: int,
                         windows: dict[str, int] | None = None) -> dict[str, Decimal | None]:
    """从 EventStore 的 OI 快照本地计算 OI 变化（规格 §3：不 pretend 服务端有历史）。

    windows: {"1m": 60000, "5m": 300000, "15m": 900000}
    只使用 now 时刻已可用的快照（可用性门禁），迟到数据不得进入 now 时刻的计算。
    """
    windows = windows or {"1m": 60_000, "5m": 300_000, "15m": 900_000}
    span = max(windows.values())
    rows = store.query_available("binance", now_ms - span, now_ms,
                                 event_type="snapshot", key=symbol.upper(), as_of_ms=now_ms)
    if not rows:
        return {f"oi_delta_{k}_pct": None for k in windows}
    cur = Decimal(str(rows[-1]["payload"].get("oi_snapshot", "0")))
    out: dict[str, Decimal | None] = {}
    for k, window in windows.items():
        ref = None
        for r in rows:
            if r["event_ts_ms"] <= now_ms - window:
                ref = Decimal(str(r["payload"].get("oi_snapshot", "0")))
        if ref is None or ref == 0:
            out[f"oi_delta_{k}_pct"] = None
        else:
            out[f"oi_delta_{k}_pct"] = (cur - ref) / ref * Decimal("100")
    return out