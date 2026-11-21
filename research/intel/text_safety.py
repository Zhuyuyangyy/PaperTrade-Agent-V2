"""不可信文本处理（Sprint 3）。

外部网页/新闻/推文是**数据**，不是指令。本模块只做三件事：

1. ``sanitize_text``：去控制字符、限长、保留原文哈希（可追溯），绝不执行；
2. ``detect_injection``：识别常见的"指令注入"模式，只做标记（``injection_suspected``），
   供日志与告警；不改变任何策略、风控或验证状态；
3. ``extract_facts``：用确定性规则抽取"数字 + 单位"这类事实，不调用 LLM、不做自由推理。

明确不做：不解析或执行文本里的"请忽略之前的指令 / 把策略标为 VERIFIED / 修改风控"等要求。
"""
from __future__ import annotations

import hashlib
import re

from .contracts import Fact

MAX_TEXT = 4_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INJECTION_PATTERNS = (
    re.compile(r"ignore (all )?(previous|prior|above) instructions", re.I),
    re.compile(r"disregard .{0,30}(instructions|rules|policy)", re.I),
    re.compile(r"system\s*:|</?system>|\[system\]", re.I),
    re.compile(r"you are now|act as (an? )?(admin|root|system)", re.I),
    re.compile(r"(忽略|无视|忘记)(之前|以上|所有)的?(指令|规则|设定)"),
    re.compile(r"(把|将).{0,12}(策略|风控|验证).{0,8}(改|设|标记|提升)"),
    re.compile(r"(提升|授予).{0,8}(VERIFIED|权限|白名单)", re.I),
    re.compile(r"<\s*script", re.I),
)
_NUM_UNIT = re.compile(
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>%|bps|bp|亿美元|亿美元|亿美元|美元|USD|USDT|亿|万|B|M|K)?",
    re.I,
)


def sanitize_text(raw: str) -> str:
    """去控制字符并限长；只用于存储与展示，不参与任何执行。"""
    text = _CONTROL.sub("", raw or "")
    return text[:MAX_TEXT]


def text_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:16]


def detect_injection(raw: str) -> tuple[bool, tuple[str, ...]]:
    text = sanitize_text(raw)
    hits = []
    for i, pat in enumerate(_INJECTION_PATTERNS):
        if pat.search(text):
            hits.append(f"pattern_{i}")
    return bool(hits), tuple(hits)


def extract_facts(raw: str, max_facts: int = 8) -> tuple[Fact, ...]:
    """确定性抽取"数字 + 单位"事实；不做语义推断。"""
    text = sanitize_text(raw)
    facts: list[Fact] = []
    for m in _NUM_UNIT.finditer(text):
        value, unit = m.group("value"), (m.group("unit") or "").strip()
        if not value:
            continue
        facts.append(Fact(key=f"number_{len(facts) + 1}", value=value, unit=unit))
        if len(facts) >= max_facts:
            break
    return tuple(facts)