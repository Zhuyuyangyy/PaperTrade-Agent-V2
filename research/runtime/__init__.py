"""运行时（Sprint 4）：一轮决策、调度循环、心跳熔断、看板。"""
from .circuit import CircuitBreaker, CircuitConfig
from .dashboard import build_state as build_dashboard_state, render_html as render_dashboard_html
from .heartbeat import Heartbeat, read as read_heartbeat, staleness_ms, write as write_heartbeat
from .log import append_jsonl, read_jsonl
from .loop import LoopConfig, LoopStopped, RunLoop
from .round import RoundResult, run_round
from .sources import CandlesSource, DemoSource, FlakySource, Source, SourceError

__all__ = [
    "CircuitBreaker", "CircuitConfig",
    "build_dashboard_state", "render_dashboard_html",
    "Heartbeat", "read_heartbeat", "staleness_ms", "write_heartbeat",
    "append_jsonl", "read_jsonl",
    "LoopConfig", "LoopStopped", "RunLoop",
    "RoundResult", "run_round",
    "CandlesSource", "DemoSource", "FlakySource", "Source", "SourceError",
]