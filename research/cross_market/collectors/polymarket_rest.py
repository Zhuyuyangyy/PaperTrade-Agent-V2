"""Polymarket REST 采集器（P1）——轮询已映射市场的盘口快照入 EventStore。

时间戳铁律（用户 2026-11-19 口径）：
  - event_ts    = 数据源服务端时间戳（CLOB book 的 timestamp 字段，ms）；
  - exchange_ts = 同上（REST 快照即服务端视图）；
  - ingest_ts   = 本地接收时间（recv_ts）。
  绝不用本地到达时间冒充事件发生时间——可用性门禁靠这三者卡迟到数据。

轮询：可配置间隔 + jitter；失败重试；全部记录（loud absence，不静默丢）。

网络实况（2026-11-19 坐实）：部署节点 DNS 为 fake-ip（198.18.0.0/15），
本地代理对 TLS 做 MITM（自签名证书）。Node 走系统证书库可通；Python 默认
certifi 不信任该 CA → CERTIFICATE_VERIFY_FAILED。修复：truststore 注入
（用 Windows 系统证书库）。此链路由此**不是** 已证明的直连——
network_probe.json 的 SUSPECTED_FAKE_IP_OR_PROXY 判定保持有效。

WS 状态：WS_STREAM_UNVERIFIED（握手通、0 数据帧）——P1 用 REST，WS 后续补。
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..schemas import PolyBookEvent
from ..storage import EventStore

# 本地 MITM 代理环境：Python 默认 certifi 不信任本地代理 CA（CERTIFICATE_VERIFY_FAILED）。
# 修复：从 Windows ROOT 证书库枚举 CA 显式加载，并关闭吊销检查（MITM 代理的
# OCSP/CRL 端点不可达；链验证保留）。全部 stdlib，无新依赖。
try:
    import atexit as _atexit
    import ssl as _ssl
    import tempfile as _tempfile

    def _build_windows_ca_context() -> "_ssl.SSLContext | None":
        try:
            entries = _ssl.enum_certificates("ROOT")
        except Exception:  # noqa: BLE001 — 非 Windows 或无权访问
            return None
        if not entries:
            return None
        pem_parts = []
        for _der, cert_type, _trust in entries:
            try:
                pem = _ssl.DER_cert_to_PEM_cert(_der)
            except Exception:  # noqa: BLE001
                continue
            # 只收 CA（basicConstraints CA:TRUE 的近似：cert_type 标记或直接全收）
            pem_parts.append(pem)
        if not pem_parts:
            return None
        tmp = _tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="ascii")
        tmp.write("".join(pem_parts))
        tmp.close()
        _atexit.register(lambda: __import__("os").unlink(tmp.name))
        ctx = _ssl.create_default_context(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(tmp.name)  # 叠加 Windows ROOT CA
        ctx.verify_flags &= ~(
            _ssl.VERIFY_CRL_CHECK_LEAF
            | _ssl.VERIFY_CRL_CHECK_CHAIN
            | _ssl.VERIFY_X509_STRICT
        )
        return ctx

    _SSL_CTX = _build_windows_ca_context()
    _SSL_HANDLER = urllib.request.HTTPSHandler(context=_SSL_CTX) if _SSL_CTX is not None else None
except Exception:  # noqa: BLE001 — 任何失败都退回默认行为
    _SSL_HANDLER = None

CLOB_BOOK = "https://clob.polymarket.com/book"
UA = {"User-Agent": "PaperTrade-CrossMarketCollector/0.1 (research; read-only)"}


class CollectorError(RuntimeError):
    """采集失败——重试后仍失败则抛出，禁静默跳过。"""


@dataclass
class CollectorConfig:
    interval_s: float = 2.0
    jitter_s: float = 0.5
    max_retries: int = 2
    timeout_s: float = 10.0


class PolyRestCollector:
    """轮询 CLOB 盘口，写 PolyBookEvent 到 EventStore。

    fetch_fn 可注入（测试用）；默认走 urllib 直连（不挂代理）。
    """

    def __init__(self, store: EventStore, markets: dict, cfg: CollectorConfig | None = None,
                 fetch_fn=None):
        # markets: {market_key: {"gamma_id":..., "clob_token_ids":[...], ...}}
        self.store = store
        self.markets = markets
        self.cfg = cfg or CollectorConfig()
        self._fetch = fetch_fn or self._default_fetch
        self.stats = {"polls": 0, "events": 0, "errors": 0}

    def _default_fetch(self, url: str) -> dict:
        for attempt in range(self.cfg.max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=UA)
                if _SSL_HANDLER is not None:
                    opener = urllib.request.build_opener(_SSL_HANDLER, urllib.request.ProxyHandler({}))
                else:
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

    @staticmethod
    def _best(levels: list) -> Decimal | None:
        if not levels:
            return None
        return Decimal(str(levels[0]["price"]))

    def poll_once(self, now_ms: int | None = None) -> list[PolyBookEvent]:
        """对所有已映射 token 拉一次盘口；返回写入的事件列表。"""
        recv_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        out: list[PolyBookEvent] = []
        for key, m in self.markets.items():
            token_ids = m.get("clob_token_ids") or []
            if not token_ids:
                continue
            for token_id in token_ids:
                try:
                    book = self._fetch(f"{CLOB_BOOK}?token_id={token_id}")
                except CollectorError:
                    self.stats["errors"] += 1
                    raise  # loud failure：调用方决定重试或停机
                ev = self._to_event(key, token_id, book, recv_ms)
                self.store.append(
                    "polymarket", ev.event_type, ev.token_id,
                    event_ts_ms=ev.event_ts_ms, ingest_ts_ms=ev.ingest_ts_ms,
                    payload={
                        "market_id": ev.market_id,
                        "bid": str(ev.bid) if ev.bid is not None else None,
                        "ask": str(ev.ask) if ev.ask is not None else None,
                        "spread_abs": str(ev.spread_abs) if ev.spread_abs is not None else None,
                        "spread_bps": str(ev.spread_bps) if ev.spread_bps is not None else None,
                        "book_hash": book.get("hash"),
                    },
                    exchange_ts_ms=ev.exchange_ts_ms,
                )
                out.append(ev)
                self.stats["events"] += 1
        self.stats["polls"] += 1
        return out

    def _to_event(self, market_key: str, token_id: str, book: dict, recv_ms: int) -> PolyBookEvent:
        # event_ts = 服务端时间戳（禁拿 recv_ms 冒充）
        server_ts = book.get("timestamp")
        event_ts = int(server_ts) if server_ts is not None else recv_ms
        bid = self._best(book.get("bids") or [])
        ask = self._best(book.get("asks") or [])
        spread_abs = (ask - bid) if (bid is not None and ask is not None) else None
        spread_bps = None
        if spread_abs is not None and bid is not None and bid > 0:
            spread_bps = (spread_abs / bid * Decimal("10000")).quantize(Decimal("0.01"))
        return PolyBookEvent(
            market_id=str(book.get("market") or market_key),
            token_id=token_id,
            event_type="book",
            bid=bid,
            ask=ask,
            spread_abs=spread_abs,
            spread_bps=spread_bps,
            event_ts_ms=event_ts,
            exchange_ts_ms=event_ts,
            ingest_ts_ms=recv_ms,
            raw=json.dumps(book, ensure_ascii=False)[:2000],
        )

    def sleep_with_jitter(self) -> None:
        time.sleep(self.cfg.interval_s + random.uniform(0, self.cfg.jitter_s))
