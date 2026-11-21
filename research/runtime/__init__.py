"""运行时（Sprint 2）：一轮决策 + 决策日志。"""
from .log import append_jsonl, read_jsonl
from .round import RoundResult, run_round

__all__ = ["append_jsonl", "read_jsonl", "RoundResult", "run_round"]