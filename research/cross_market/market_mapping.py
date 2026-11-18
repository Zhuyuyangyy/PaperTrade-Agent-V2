"""Market Mapping（规格 §6）——每个 Polymarket 市场映射到可研究的标的与视界。

禁防："ETH Above $2000 by Friday" 被拿去预测"未来 30 秒涨不涨"。
mapping 未登记的市场一律不可研究（fail-closed）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MarketMapping:
    market_id: str
    underlying_asset: str
    event_threshold: float
    event_comparator: str  # ABOVE | BELOW
    event_expiry: str      # ISO8601
    resolution_source: str
    target_horizons: tuple[str, ...]
    primary_horizon: str   # 预注册主视界（其余 secondary，禁多重检验钓鱼）


VALID_COMPARATORS = ("ABOVE", "BELOW")


def load_mapping(path: Path) -> dict[str, MarketMapping]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, MarketMapping] = {}
    for mid, m in (data or {}).items():
        comparator = str(m["event_comparator"]).upper()
        if comparator not in VALID_COMPARATORS:
            raise ValueError(f"{mid}: event_comparator 必须是 {VALID_COMPARATORS}")
        horizons = tuple(str(h) for h in m["target_horizons"])
        primary = str(m.get("primary_horizon") or horizons[0])
        if primary not in horizons:
            raise ValueError(f"{mid}: primary_horizon {primary} 不在 target_horizons")
        out[mid] = MarketMapping(
            market_id=mid,
            underlying_asset=str(m["underlying_asset"]),
            event_threshold=float(m["event_threshold"]),
            event_comparator=comparator,
            event_expiry=str(m["event_expiry"]),
            resolution_source=str(m.get("resolution_source", "")),
            target_horizons=horizons,
            primary_horizon=primary,
        )
    return out


def is_researchable(market_id: str, mapping: dict[str, MarketMapping]) -> bool:
    """未登记 = 不可研究（fail-closed，规格 §6）。"""
    return market_id in mapping
