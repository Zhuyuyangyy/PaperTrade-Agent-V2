"""SEC EDGAR 8-K 一手源适配器（只读公开数据，advisory_only）。

为什么接这个源：
  2026-11-23 黄仁勋复职事件实测——董事会 11/22 决议（"effective immediately"），
  但市场真正能拿到消息是 **8-K 被 SEC 接受的 2026-11-23T13:57:54Z**
  （美东 08:57:54 EST / 北京 21:57:54）。X 上的快讯号与媒体都在之后。
  8-K 免费、机器可读、带秒级时间戳，比追 X 账号更快更硬。

**最重要的防前视设计**：
  ``reportDate``（董事会决议日）和 ``acceptanceDateTime``（SEC 接受时刻）必须分开。

  - ``occurred_at_ms``  = reportDate 的 UTC 00:00，**只有天精度，不参与任何时点决策**；
  - ``first_seen_ms``   = acceptanceDateTime，**这才是市场可知时刻**；
  - ``published_at_ms`` = 同 acceptanceDateTime。

  如果把 reportDate 当成 first_seen，事件会提前最多一整天进入特征，
  这是教科书级的前视泄漏。这里从数据结构上堵死。

SEC 要求带 User-Agent；免费、无需 API key。只读，不碰交易账户。
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .contracts import Fact, NewsEvent, SourceRef

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_digits}/{doc}"
USER_AGENT = "OpenAlpha research research@openalpha.local"

#: 常用 CIK。新增品种必须显式登记，不允许运行时猜测。
CIK_MAP: dict[str, str] = {
    "NVDA": "0001045810",
}

#: 8-K 的 Item 编号 → 人读标签（只做标注，不做交易方向判断）
ITEM_LABELS: dict[str, str] = {
    "2.02": "业绩发布",
    "5.02": "高管变动",
    "8.01": "其他重大事件",
    "1.01": "重大协议",
    "2.01": "资产收购/处置",
    "4.01": "审计师变更",
    "5.03": "章程修订",
}

_DAY_MS = 86_400_000


def _parse_iso_utc(s: str) -> int:
    """把 SEC 的 ISO8601（含 Z）转成 epoch ms。"""
    return int(datetime.fromisoformat(s.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp() * 1000)


def _parse_day_utc(s: str) -> int:
    """只带日期的 reportDate → 当日 UTC 00:00（天精度，仅作事实字段）。"""
    return int(datetime.strptime(s, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_submissions(cik: str, *, fetch: Callable[[str], object] | None = None,
                      cache: Path | None = None, timeout_s: float = 30.0) -> dict:
    """拉 EDGAR submissions JSON。命中本地缓存则零请求。"""
    if cache is not None and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    url = SUBMISSIONS_URL.format(cik=int(cik))
    if fetch is not None:
        payload = fetch(url)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        # 显式禁用代理，与项目内其它只读抓取保持一致
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload  # type: ignore[return-value]


def extract_8k(payload: dict, *, since: str | None = None,
               forms: tuple[str, ...] = ("8-K",)) -> list[dict]:
    """从 submissions 中抽出目标 form 的 filing，保留两个时间字段。

    返回项里的 ``accepted_ms`` 是秒级精确的市场可知时刻，
    ``report_day_ms`` 只是天精度的事实日期——**不得用于择时**。
    """
    recent = payload.get("filings", {}).get("recent", {})
    n = len(recent.get("form", []))
    out: list[dict] = []
    for i in range(n):
        form = recent["form"][i]
        # 精确匹配：startswith("8-K") 会把 8-K/A 修订版也算进来，
        # 修订版不是新事件，重复入库会造成同一事件双计。
        if form not in forms:
            continue
        filing_date = recent["filingDate"][i]
        if since and filing_date < since:
            continue
        out.append({
            "form": form,
            "filing_date": filing_date,
            "report_date": recent["reportDate"][i],
            "accepted_utc": recent["acceptanceDateTime"][i],
            "accepted_ms": _parse_iso_utc(recent["acceptanceDateTime"][i]),
            "report_day_ms": _parse_day_utc(recent["reportDate"][i]),
            "accession": recent["accessionNumber"][i],
            "doc": recent["primaryDocument"][i],
            "items": recent.get("items", [""] * n)[i],
        })
    out.sort(key=lambda x: x["accepted_ms"])
    return out


def to_news_events(filings: list[dict], *, symbol: str,
                   cik: str | None = None) -> list[NewsEvent]:
    """filing → NewsEvent，严格区分"事件发生"与"市场可知"两个时间。"""
    cik = cik or CIK_MAP.get(symbol, "")
    now = int(time.time() * 1000)
    out: list[NewsEvent] = []
    for f in filings:
        items = [x.strip() for x in f["items"].split(",") if x.strip()]
        labels = [ITEM_LABELS.get(x, x) for x in items]
        headline = f"{symbol} SEC {f['form']} · {'/'.join(labels) if labels else '无 item 标注'}"
        doc_url = ARCHIVE_URL.format(
            cik=int(cik), acc_no_digits=f["accession"].replace("-", ""),
            doc=f["doc"]) if cik else ""
        src = SourceRef(
            url=doc_url, publisher="SEC EDGAR",
            published_at_ms=f["accepted_ms"],   # 市场可知时刻
            first_seen_ms=f["accepted_ms"],     # 同上
            retrieved_ms=now,
        )
        facts = tuple([
            Fact("form", f["form"]),
            Fact("items", ",".join(items)),
            Fact("report_date", f["report_date"]),
            Fact("accepted_utc", f["accepted_utc"]),
            Fact("accession", f["accession"]),
        ])
        out.append(NewsEvent(
            event_id=f"sec-{symbol}-{f['accession']}",
            headline=headline,
            facts=facts,
            occurred_at_ms=f["report_day_ms"],   # 天精度，仅事实
            first_seen_ms=f["accepted_ms"],      # 秒精度，用于择时
            sources=(src,),
            credibility=Decimal("1.0"),          # 法定披露，可信度最高
            horizon_ms=_DAY_MS,
            tags=("sec", "8k", "official", "primary_source", symbol, *items),
            direction="neutral",                 # 不对公告内容做方向判断
            confidence=Decimal("1.0"),
            event_type="regulatory_filing",
            extraction_method="rules",
        ))
    return out


def latency_ms(event: NewsEvent, reference_ms: int) -> int:
    """参照源相对 8-K 的滞后（正数 = 8-K 更早）。

    用途：量化和交叉验证"某个新闻源到底比法定披露快还是慢"。
    """
    return reference_ms - event.first_seen_ms


def format_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


__all__ = ["CIK_MAP", "ITEM_LABELS", "fetch_submissions", "extract_8k",
           "to_news_events", "latency_ms", "format_utc", "SUBMISSIONS_URL"]
