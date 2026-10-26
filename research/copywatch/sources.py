"""Read-only adapters for public on-chain swap data.

Only public, key-less HTTP endpoints are used. Nothing here can move funds: there is
no wallet, no private key, and no signing code in this module.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_TIMEOUT = 12.0
USER_AGENT = "PaperTrade-CopyWatch/0.1 (paper-only research; read-only public data)"
# Public API allows ~30 calls/minute. Space requests out instead of bursting.
MIN_CALL_INTERVAL_SECONDS = 2.0
_last_call_at = 0.0


class SourceError(RuntimeError):
    """Raised when the public data source cannot be read or returns garbage."""


@dataclass(frozen=True)
class SwapEvent:
    """One real on-chain swap, as reported by the public pool API."""

    event_id: str
    network: str
    pool: str
    wallet: str
    kind: str
    usd: float
    price_usd: float
    base_symbol: str
    quote_symbol: str
    block_ts_ms: int
    block_number: int | None
    tx_hash: str
    anomalous: bool
    discovered_at_ms: int
    latency_ms: int


@dataclass(frozen=True)
class PoolInfo:
    network: str
    pool: str
    name: str
    base_symbol: str
    quote_symbol: str
    reserve_usd: float | None
    price_usd: float | None
    fetched_at_ms: int


def _http_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    # A versioned Accept header made this endpoint return an empty list intermittently,
    # so the plain JSON accept plus an explicit no-cache is what actually stays fresh.
    global _last_call_at
    wait = MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise SourceError(f"HTTP {response.status} for {url}")
            payload = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise SourceError("数据源限流（HTTP 429），本轮跳过") from exc
        raise SourceError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"网络不可达：{exc}") from exc
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError("数据源返回内容不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise SourceError("数据源返回结构异常")
    return data


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ms(iso_stamp: object) -> int | None:
    """Convert the source's ISO-8601 UTC timestamp to epoch milliseconds."""
    if not isinstance(iso_stamp, str) or not iso_stamp:
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(iso_stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def fetch_swaps(
    *,
    network: str,
    pool: str,
    base_symbol: str,
    quote_symbol: str,
    min_usd: float,
    base_url: str,
    now_ms: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[SwapEvent]:
    """Return the pool's most recent swaps that are at least ``min_usd`` in size."""
    query = urllib.parse.urlencode(
        {"trade_volume_in_usd_greater_than": max(0.0, float(min_usd))}
    )
    url = f"{base_url.rstrip('/')}/networks/{network}/pools/{pool}/trades?{query}"
    payload = _http_json(url, timeout=timeout)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise SourceError("数据源未返回交易列表")

    events: list[SwapEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attributes = row.get("attributes")
        if not isinstance(attributes, dict):
            continue
        wallet = attributes.get("tx_from_address")
        tx_hash = attributes.get("tx_hash")
        kind = attributes.get("kind")
        usd = _as_float(attributes.get("volume_in_usd"))
        block_ms = _ms(attributes.get("block_timestamp"))
        # The base token is what we price. GeckoTerminal reports two legs: on a
        # buy the base token arrives, on a sell it leaves. Reading a single
        # field silently recorded the quote token's ~1.00 USD price on every
        # sell, which corrupted the whole price series.
        price = _as_float(
            attributes.get("price_to_in_usd")
            if kind == "buy"
            else attributes.get("price_from_in_usd")
        )
        if not (isinstance(wallet, str) and wallet and isinstance(tx_hash, str) and tx_hash):
            continue
        if kind not in ("buy", "sell") or usd is None or usd <= 0 or block_ms is None:
            continue
        if price is None or price <= 0:
            continue
        event_id = str(row.get("id") or f"{network}:{tx_hash}:{wallet}:{block_ms}")
        events.append(
            SwapEvent(
                event_id=event_id,
                network=network,
                pool=pool,
                wallet=wallet.lower(),
                kind=str(kind),
                usd=usd,
                price_usd=price,
                base_symbol=base_symbol,
                quote_symbol=quote_symbol,
                block_ts_ms=block_ms,
                block_number=_as_int(attributes.get("block_number")),
                tx_hash=tx_hash,
                anomalous=attributes.get("is_anomalous") is True,
                discovered_at_ms=now_ms,
                latency_ms=max(0, now_ms - block_ms),
            )
        )
    events.sort(key=lambda item: item.block_ts_ms)
    return events


def fetch_trending_pools(
    *, network: str, base_url: str, limit: int = 10, timeout: float = DEFAULT_TIMEOUT
) -> list[dict]:
    """Trending pools for a network, as pool configs the agent can consume.

    GeckoTerminal's trending endpoint needs no API key and returns the pools
    where attention is concentrated right now -- which for a copy-trading desk
    is where new meme tokens actually trade.
    """
    url = f"{base_url.rstrip('/')}/networks/trending_pools?page=1"
    payload = _http_json(url, timeout=timeout)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise SourceError("数据源未返回趋势池列表")
    out: list[dict] = []
    for row in rows[: max(1, limit)]:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes")
        if not isinstance(attrs, dict):
            continue
        name = str(attrs.get("name") or "")
        base_symbol, _, quote_symbol = (part.strip() for part in name.partition("/"))
        # The pool id is "<network>_<address>"; the network is not in attributes.
        row_id = str(row.get("id") or "")
        net, _, address = row_id.partition("_")
        if not address or not base_symbol or not net:
            continue
        if network and net != network:
            continue
        out.append(
            {
                "network": net,
                "pool": address,
                "base_symbol": base_symbol,
                "quote_symbol": quote_symbol or "?",
                "label": f"{name} · trending",
                "reserve_usd": _as_float(attrs.get("reserve_in_usd")),
                "discovered": True,
            }
        )
    return out


def fetch_pool(
    *, network: str, pool: str, base_url: str, now_ms: int, timeout: float = DEFAULT_TIMEOUT
) -> PoolInfo:
    """Return current pool reserves and the base token's USD price."""
    url = f"{base_url.rstrip('/')}/networks/{network}/pools/{pool}"
    payload = _http_json(url, timeout=timeout)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("attributes"), dict):
        raise SourceError("数据源未返回池子信息")
    attributes = data["attributes"]
    name = str(attributes.get("name") or f"{network}:{pool}")
    base_symbol, _, quote_symbol = (part.strip() for part in name.partition("/"))
    price = _as_float(
        attributes.get("base_token_price_usd")
        or attributes.get("price_usd")
        or attributes.get("quote_token_price_usd")
    )
    reserve = _as_float(attributes.get("reserve_in_usd"))
    return PoolInfo(
        network=network,
        pool=pool,
        name=name,
        base_symbol=base_symbol or "?",
        quote_symbol=quote_symbol or "?",
        reserve_usd=reserve,
        price_usd=price if (price is not None and price > 0) else None,
        fetched_at_ms=now_ms,
    )
