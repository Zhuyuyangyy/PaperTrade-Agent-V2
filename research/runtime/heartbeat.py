"""心跳（Sprint 4）：运行状态写到文件，看板与运维只读文件，不读内存。

字段故意少而硬：能否证明"上一轮什么时候成功过"、"连续失败几次"、
"是否被熔断"、"熔断原因是什么"。任何一项缺失，看板应显示 UNKNOWN 而不是猜。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Heartbeat:
    updated_at_ms: int = 0
    last_ok_ms: int = 0
    last_round_ts_ms: int = 0
    last_error: str = ""
    consecutive_failures: int = 0
    halted: bool = False
    halt_reason: str = ""
    rounds: int = 0
    pid: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def write(path: Path | str, hb: Heartbeat) -> None:
    """原子写：先写临时文件再替换，避免看板读到半截 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(hb.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read(path: Path | str) -> Heartbeat | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return Heartbeat(**{k: v for k, v in data.items() if k in Heartbeat.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def staleness_ms(hb: Heartbeat, now_ms: int) -> int | None:
    """距离上一次成功过去多久；从未成功 → None（不是 0，不能假装新鲜）。"""
    if hb.last_ok_ms <= 0:
        return None
    return max(0, now_ms - hb.last_ok_ms)