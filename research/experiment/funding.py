"""实验 E4：资金费率极值信号（有经济解释，不是均线交叉）。

逻辑假设：永续合约的资金费率是多空拥挤度的直接定价。
  - 费率极端为正 → 多头拥挤、为持多付高溢价 → 反向做空有正carry与回撤补偿；
  - 费率极端为负 → 空头拥挤 → 反向做多。
这不是"价格形态"，而是**持仓成本**给出的信号，经济解释清楚。

关键设计（防前视）：
  - 资金费率在结算时刻才可知；决策只能用 ``fundingTime <= as_of`` 的记录；
  - 信号在结算时刻产生，成交在**下一根 K 线开盘**（复用 ledger.open_at_next_bar）；
  - 阈值用**滚动窗口**的历史分位/z 值，不用全样本统计量。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path
from typing import Callable

from research.market.budget import GLOBAL_BUDGET
from research.market.contracts import Candle

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BPS = D("10000")


def load_funding(*, symbol: str = "BTCUSDT", days: int = 730, cache: Path,
                 fetch: Callable[[str], object] | None = None,
                 end_ms: int | None = None) -> list[dict]:
    """分页拉取资金费率历史（每 8 小时一条），缓存到本地。"""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    end = int(end_ms or time.time() * 1000)
    start = end - days * 86_400_000
    rows: dict[int, dict] = {}
    cursor = start
    for _ in range(50):
        url = (f"{FUNDING_URL}?symbol={symbol}&startTime={cursor}"
               f"&endTime={end}&limit=1000")
        page = []
        if fetch is not None:
            page = list(fetch(url) or [])
        else:
            for attempt in range(4):
                GLOBAL_BUDGET.acquire()
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with op.open(req, timeout=20) as resp:
                        page = list(json.load(resp))
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in (418, 429):
                        GLOBAL_BUDGET.note_rate_limited()
                        continue
                    raise
        if not page:
            break
        for r in page:
            rows[int(r["fundingTime"])] = {
                "funding_ms": int(r["fundingTime"]),
                "rate": str(r["fundingRate"]),
                "mark_price": str(r.get("markPrice") or ""),
            }
        nxt = int(page[-1]["fundingTime"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(page) < 1000:
            break
    out = [rows[k] for k in sorted(rows)]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


@dataclass(frozen=True)
class FundingSignal:
    ts_ms: int              # 结算时刻（信号可用时刻）
    rate: D
    z: D                    # 相对滚动窗口的 z 值
    side: str               # LONG / SHORT（反向于拥挤方向）


def funding_signals(funding: list[dict], bars: list[Candle], *,
                    lookback: int = 90, z_threshold: D = D("2.0"),
                    min_history: int = 30) -> list[FundingSignal]:
    """滚动 z 值越界即产生反向信号；只用当前及之前的费率，无前视。

    费率每 8h 一条，lookback=90 ≈ 30 天。z 用滚动均值/标准差（样本内滚动，非全样本）。
    """
    bars_start = int(bars[0].open_ms) if bars else 0
    bars_end = int(bars[-1].close_ms) if bars else 0
    rates = [D(x["rate"]) for x in funding]
    out: list[FundingSignal] = []
    for i, f in enumerate(funding):
        ts = int(f["funding_ms"])
        if ts < bars_start or ts > bars_end:
            continue
        lo = max(0, i - lookback)
        window = rates[lo:i]                  # 不含当次，避免自我污染
        if len(window) < min_history:
            continue
        n = D(len(window))
        mean = sum(window, D("0")) / n
        var = sum(((r - mean) ** 2 for r in window), D("0")) / n
        sd = var.sqrt()
        if sd <= 0:
            continue
        z = (rates[i] - mean) / sd
        if z >= z_threshold:
            out.append(FundingSignal(ts, rates[i], z, "SHORT"))   # 多头拥挤 → 做空
        elif z <= -z_threshold:
            out.append(FundingSignal(ts, rates[i], z, "LONG"))    # 空头拥挤 → 做多
    return out


__all__ = ["load_funding", "funding_signals", "FundingSignal"]
