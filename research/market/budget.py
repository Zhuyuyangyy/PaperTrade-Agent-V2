"""共享 REST 请求预算：所有行情抓取都必须经过这里。

24 小时实验出现 418 的根因是"单进程不等于低请求量"：
每次历史刷新打 4 个周期，新闻采集、REST 兜底又各打自己的请求，
总量没人管。这里给所有走 BinanceKlineSource 的请求一个全局闸门：

- 滑动窗口限流（默认 30 次/分钟）；
- 遇到 418/429 后全局冷却（默认 10 分钟），遵守交易所退避；
- 所有请求/拒绝/限流/等待都计数，可从引擎 data_stats 读出来。

新闻采集目前仍走独立 fetch；见阶段报告里列出的残余风险。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RequestBudget:
    max_per_minute: int = 30
    cooldown_s: int = 600
    _hits: deque = field(default_factory=deque)
    _cooldown_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    stats: dict = field(default_factory=lambda: {
        "requests": 0, "blocked_by_budget": 0, "rate_limited": 0,
        "waited_ms": 0, "cooldown_triggered": 0,
    })

    def acquire(self) -> None:
        """阻塞直到允许请求；把等待计入统计，绝不静默重试。"""
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait_ms = int((self._cooldown_until - now) * 1000)
                    self.stats["waited_ms"] += wait_ms
                else:
                    while self._hits and now - self._hits[0] >= 60:
                        self._hits.popleft()
                    if len(self._hits) < self.max_per_minute:
                        self._hits.append(now)
                        self.stats["requests"] += 1
                        return
                    wait_ms = int((60 - (now - self._hits[0])) * 1000) + 10
                    self.stats["blocked_by_budget"] += 1
                    self.stats["waited_ms"] += wait_ms
            time.sleep(max(0.01, wait_ms / 1000))

    def note_rate_limited(self) -> None:
        with self._lock:
            self.stats["rate_limited"] += 1
            self._cooldown_until = time.monotonic() + self.cooldown_s
            self.stats["cooldown_triggered"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self.stats)
            out["cooldown_left_s"] = max(0.0, self._cooldown_until - time.monotonic())
            return out


GLOBAL_BUDGET = RequestBudget()
