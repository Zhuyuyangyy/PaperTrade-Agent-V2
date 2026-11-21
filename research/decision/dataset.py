"""Point-in-time 数据集（Alpha Discovery V1）：给线性模型准备特征与未来标签。

规则：
  - 特征只取决策日志里已记录的快照（决策时刻已知）；
  - 标签只在未来 h 小时 **兑现之后** 才写；未兑现 → None（不是 0）；
  - 训练集/验证集按时间切分并留 embargo，避免重叠标签泄漏；
  - 30 条只是代码能调用 train() 的下限，不是"可验证有效"的样本量。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from research.market.contracts import Candle

HOUR = 3_600_000
DEFAULT_HORIZONS = (1, 4, 24)


@dataclass(frozen=True)
class DatasetRow:
    as_of_ms: int
    features: dict[str, float]
    labels: dict[str, float | None]

    def to_dict(self) -> dict:
        return {"as_of_ms": self.as_of_ms, "features": self.features,
                "labels": {k: (None if v is None else round(v, 6))
                           for k, v in self.labels.items()}}


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _features_from_entry(entry: dict) -> dict[str, float]:
    state = entry.get("state") or {}
    feats = state.get("features") or {}
    trend = state.get("trend", "")
    momentum = state.get("momentum", "")
    intel = entry.get("intel") or {}
    news = (intel.get("news") or [])
    signed = 0.0
    for n in news:
        d = n.get("direction")
        c = _num(n.get("confidence"))
        signed += c if d == "bullish" else -c if d == "bearish" else 0.0
    return {
        "trend_dir": 1.0 if trend == "TREND_UP" else -1.0 if trend == "TREND_DOWN" else 0.0,
        "momentum_dir": 1.0 if momentum == "MOMENTUM_UP" else -1.0 if momentum == "MOMENTUM_DOWN" else 0.0,
        "adx_norm": min(1.0, _num(feats.get("adx")) / 50.0),
        "rsi_norm": max(-1.0, min(1.0, (_num(feats.get("rsi")) - 50.0) / 50.0)),
        "atr_norm": min(1.0, _num(feats.get("atr_pct")) * 10_000 / 200.0),
        "news_bias": max(-1.0, min(1.0, signed / max(1, len(news)))),
        "pred_bias": 0.0,
        "wallet_bias": 0.0,
        "micro_bias": 0.0,
    }


def _price_at(candles: list[Candle], target_ms: int) -> float | None:
    for c in candles:
        if c.close_ms >= target_ms:
            return float(c.close)
    return None


def build_rows(log_path: Path | str, candles: list[Candle],
               horizons_h: tuple[int, ...] = DEFAULT_HORIZONS) -> list[DatasetRow]:
    path = Path(log_path)
    if not path.is_file():
        return []
    candles = sorted(candles, key=lambda c: c.open_ms)
    rows: list[DatasetRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            as_of = int(entry["round_ts_ms"])
        except (ValueError, KeyError):
            continue
        base = _price_at(candles, as_of)
        labels: dict[str, float | None] = {}
        for h in horizons_h:
            future = _price_at(candles, as_of + h * HOUR) if base else None
            labels[f"{h}h"] = (future / base - 1.0) if (future is not None and base) else None
        rows.append(DatasetRow(as_of, _features_from_entry(entry), labels))
    return rows


def purged_split(rows: list[DatasetRow], train_ratio: float = 0.7,
                 embargo_hours: int = 24) -> tuple[list[DatasetRow], list[DatasetRow]]:
    """按时间切分，训练集末尾留 embargo（丢弃重叠标签的样本）。"""
    if not rows:
        return [], []
    ordered = sorted(rows, key=lambda r: r.as_of_ms)
    cut = int(len(ordered) * train_ratio)
    train = ordered[:cut]
    test = ordered[cut:]
    if train and test:
        cutoff = train[-1].as_of_ms - embargo_hours * HOUR
        train = [r for r in train if r.as_of_ms <= cutoff]
    return train, test


def to_jsonl(rows: list[DatasetRow], path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def labelled(rows: list[DatasetRow], horizon: str = "1h") -> tuple[list[dict], list[int]]:
    """返回 (features, label) —— 标签为正收益记 1，负收益记 0；未兑现丢弃。"""
    xs, ys = [], []
    for r in rows:
        v = r.labels.get(horizon)
        if v is None:
            continue
        xs.append(r.features)
        ys.append(1 if v > 0 else 0)
    return xs, ys