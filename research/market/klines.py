"""多周期 K 线（Sprint 1）——只吃已收盘、显式报缺口、可离线确定性重放。

规则：
  1. **已收盘**：只有 ``open_ms + timeframe <= as_of - settle`` 的 K 线才允许进入分析；
     未收盘 K 线的 high/low/close 还会变，用它算指标就是前视。
  2. **对齐**：``open_ms`` 必须落在 timeframe 边界上；不对齐的行被丢弃并记录，
     绝不"就近取整"（取整会让两根 K 线看起来连续，其实是两个时段）。
  3. **缺口不补**：聚合时一个 bucket 的分钟数不足或时间戳不连续，就丢弃该 bucket
     并记进 ``incomplete``；缺口检测把相邻 K 线的间距 != timeframe 全部列出。
     缺 100 根而当成连续序列，会让 ATR/ADX/持有期全部失真。
  4. **确定性**：同一输入 → 同一输出；不排序外部可变结构。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .contracts import Candle


@dataclass(frozen=True)
class LoadResult:
    candles: list[Candle]
    notes: tuple[str, ...] = ()
    dropped_unaligned: int = 0
    dropped_duplicate: int = 0


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def load_csv(path: Path, timeframe_ms: int, *, as_of_ms: int | None = None,
             settle_ms: int = 0, keep_unclosed: bool = False) -> LoadResult:
    """CSV（列 t,o,h,l,c[,v]，t 为 UTC 毫秒）→ 已收盘 Candle 列表。

    ``keep_unclosed=True`` 只给诊断用（会显式标注），分析路径禁止使用。
    """
    rows: dict[int, Candle] = {}
    notes: list[str] = []
    unaligned = dup = 0
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                t = int(float(raw["t"]))
                o, h, l, c = (_dec(raw[k]) for k in ("o", "h", "l", "c"))
                v = _dec(raw.get("v") or "0")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                notes.append("skipped_malformed_row")
                continue
            if timeframe_ms > 0 and t % timeframe_ms != 0:
                unaligned += 1
                continue
            if t in rows:
                dup += 1
                notes.append(f"duplicate_open_ms:{t}")
            try:
                rows[t] = Candle(t, o, h, l, c, v, timeframe_ms)
            except ValueError as exc:
                notes.append(f"invalid_candle:{t}:{exc}")
    candles = [rows[t] for t in sorted(rows)]
    if as_of_ms is not None and not keep_unclosed:
        candles = closed_only(candles, as_of_ms, settle_ms)
    return LoadResult(candles, tuple(notes), unaligned, dup)


def closed_only(candles: list[Candle], as_of_ms: int, settle_ms: int = 0) -> list[Candle]:
    """只保留在 as_of（减去 settle 宽限）之前已经收盘的 K 线。"""
    cutoff = as_of_ms - settle_ms
    return [c for c in candles if c.close_ms <= cutoff]


def gaps(candles: list[Candle]) -> list[dict[str, int]]:
    """相邻 K 线间距 != timeframe 的缺口；``missing_bars`` 为缺失根数。"""
    out: list[dict[str, int]] = []
    if len(candles) < 2:
        return out
    tf = candles[0].timeframe_ms
    for prev, cur in zip(candles, candles[1:]):
        delta = cur.open_ms - prev.open_ms
        if delta != tf:
            out.append({
                "from_ms": prev.open_ms,
                "to_ms": cur.open_ms,
                "missing_bars": max(0, delta // tf - 1) if delta > tf else 0,
            })
    return out


def aggregate(base: list[Candle], target_ms: int) -> tuple[list[Candle], list[dict[str, int]]]:
    """把基础周期聚合成目标周期；只输出**完整** bucket，并返回不完整 bucket。"""
    if not base:
        return [], []
    base_ms = base[0].timeframe_ms
    if target_ms % base_ms != 0:
        raise ValueError(f"目标周期 {target_ms} 不是基础周期 {base_ms} 的整数倍")
    ratio = target_ms // base_ms
    buckets: dict[int, list[Candle]] = {}
    for c in base:
        buckets.setdefault(c.open_ms // target_ms * target_ms, []).append(c)
    out: list[Candle] = []
    incomplete: list[dict[str, int]] = []
    for b in sorted(buckets):
        group = sorted(buckets[b], key=lambda c: c.open_ms)
        expected = [b + i * base_ms for i in range(ratio)]
        if [c.open_ms for c in group] != expected:
            incomplete.append({"open_ms": b, "bars": len(group), "expected": ratio})
            continue
        out.append(Candle(
            b,
            group[0].open,
            max(c.high for c in group),
            min(c.low for c in group),
            group[-1].close,
            sum((c.volume for c in group), Decimal("0")),
            target_ms,
        ))
    return out, incomplete


def resample(base: list[Candle], target_ms: int) -> tuple[list[Candle], list[dict[str, int]]]:
    """基础周期 == target 时原样返回；否则聚合。"""
    if not base:
        return [], []
    if base[0].timeframe_ms == target_ms:
        return list(base), []
    return aggregate(base, target_ms)


def from_rows(rows: list[list], timeframe_ms: int) -> list[Candle]:
    """Binance klines 原始行 → Candle（只接受 6 列以上、已对齐的行）。"""
    out: list[Candle] = []
    for row in rows:
        if len(row) < 6:
            continue
        t, o, h, l, c, v = row[:6]
        try:
            candle = Candle(int(t), _dec(o), _dec(h), _dec(l), _dec(c), _dec(v), timeframe_ms)
        except (TypeError, ValueError):
            continue
        out.append(candle)
    return out


def to_payload(candle: Candle) -> dict:
    return {
        "open_ms": candle.open_ms,
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }


def ensure_contiguous(candles: list[Candle]) -> None:
    """分析前的硬校验：出现缺口直接抛错，调用方必须显式处理（不能假装连续）。"""
    g = gaps(candles)
    if g:
        raise ValueError(f"K 线序列存在 {len(g)} 个缺口，禁止直接分析: {g[:3]}")