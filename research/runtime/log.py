"""决策日志（Sprint 2）：每轮一条 JSONL，append-only。

一条日志必须能回答：当时市场是什么状态、有哪些证据与冲突、候选策略为什么被选/被拒、
风控逐项结果、有没有成交、成交价与费用、空仓的具体原因。
"""
from __future__ import annotations

import json
from pathlib import Path


def append_jsonl(path: Path | str, entry: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_jsonl(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out