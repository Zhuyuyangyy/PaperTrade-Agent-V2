"""新闻 feeds（Alpha Discovery V1）——RSS/Atom 拉取与解析。

只做三件事：拉、解析、保留来源与时间。不做语义判断（那是 news_llm 的事），
也不执行任何正文中的指令。``fetch`` 可注入，测试完全不联网。
"""
from __future__ import annotations

import email.utils
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone

UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (research; read-only)"}


@dataclass(frozen=True)
class FeedItem:
    source: str
    title: str
    link: str
    published_ms: int | None
    summary: str = ""


def default_fetch(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers=UA)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _ms(date_str: str) -> int | None:
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def parse_feed(xml_text: str, source: str) -> list[FeedItem]:
    """同时支持 RSS 2.0 与 Atom。解析失败抛 ValueError（调用方记 warning）。"""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise ValueError(f"feed 解析失败: {exc}") from exc

    items: list[FeedItem] = []
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {c.tag.split("}")[-1]: c for c in list(item)}
        title = _text(fields.get("title"))
        if not title:
            continue
        link = _text(fields.get("link"))
        if not link:
            link_node = fields.get("link")
            if link_node is not None:
                link = link_node.attrib.get("href", "")
        published = _text(fields.get("pubDate")) or _text(fields.get("updated")) \
            or _text(fields.get("published"))
        summary = _text(fields.get("description")) or _text(fields.get("summary"))
        items.append(FeedItem(source=source, title=title, link=link,
                              published_ms=_ms(published), summary=summary[:2000]))
    return items


def fetch_feed(url: str, source: str, fetch=None, timeout: float = 15.0) -> list[FeedItem]:
    fn = fetch or (lambda u: default_fetch(u, timeout))
    raw = fn(url)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return parse_feed(raw, source)


def fetch_feeds(feeds: tuple[tuple[str, str], ...], fetch=None,
                timeout: float = 15.0) -> tuple[list[FeedItem], list[str]]:
    """返回 (items, warnings)；单个源失败不影响其它源。"""
    items: list[FeedItem] = []
    warnings: list[str] = []
    for url, source in feeds:
        try:
            items.extend(fetch_feed(url, source, fetch=fetch, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"FEED_FAILED:{source}:{type(exc).__name__}")
    return items, warnings