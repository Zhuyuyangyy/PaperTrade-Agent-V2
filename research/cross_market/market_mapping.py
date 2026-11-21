"""Market Mapping（规格 §6）——每个 Polymarket 市场映射到可研究的标的与视界。

禁防："ETH Above $2000 by Friday" 被拿去预测"未来 30 秒涨不涨"。
mapping 未登记的市场一律不可研究（fail-closed）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    gamma_id: str = ""
    clob_token_ids: tuple[str, ...] = ()
    expired: bool = False  # 加载时按 event_expiry 计算；过期市场禁入研究
    expiry_unknown: bool = False  # 到期时间解析失败：规则不明，同样禁入（fail-closed）


VALID_COMPARATORS = ("ABOVE", "BELOW")


def load_mapping(path: Path) -> dict[str, MarketMapping]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, MarketMapping] = {}
    now = datetime.now(timezone.utc)
    for mid, m in (data or {}).items():
        comparator = str(m["event_comparator"]).upper()
        if comparator not in VALID_COMPARATORS:
            raise ValueError(f"{mid}: event_comparator 必须是 {VALID_COMPARATORS}")
        horizons = tuple(str(h) for h in m["target_horizons"])
        primary = str(m.get("primary_horizon") or horizons[0])
        if primary not in horizons:
            raise ValueError(f"{mid}: primary_horizon {primary} 不在 target_horizons")
        # 过期判定（禁把过期市场当研究标的）。解析失败 = 规则不明 → fail-closed。
        expiry_str = str(m["event_expiry"])
        expiry_unknown = False
        try:
            exp = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            expired = exp <= now
        except ValueError:
            expired = False
            expiry_unknown = True
        out[mid] = MarketMapping(
            market_id=mid,
            underlying_asset=str(m["underlying_asset"]),
            event_threshold=float(m["event_threshold"]),
            event_comparator=comparator,
            event_expiry=expiry_str,
            resolution_source=str(m.get("resolution_source", "")),
            target_horizons=horizons,
            primary_horizon=primary,
            gamma_id=str(m.get("gamma_id", "")),
            clob_token_ids=tuple(str(t) for t in (m.get("clob_token_ids") or [])),
            expired=expired,
            expiry_unknown=expiry_unknown,
        )
    return out


def is_market_tradeable(m: MarketMapping) -> bool:
    """市场是否可以进入研究/实验：未过期、到期规则明确。"""
    return not m.expired and not m.expiry_unknown


def researchable_markets(mapping: dict[str, MarketMapping]) -> dict[str, MarketMapping]:
    """只返回未过期、规则明确的已登记市场。"""
    return {k: v for k, v in mapping.items() if is_market_tradeable(v)}


def is_researchable(market_id: str, mapping: dict[str, MarketMapping]) -> bool:
    """未登记 / 已过期 / 到期规则不明 = 不可研究（fail-closed，规格 §6）。"""
    m = mapping.get(market_id)
    return m is not None and is_market_tradeable(m)
