"""OpenAlpha Trading Terminal：只读可视化终端。"""
from .server import Terminal, TerminalConfig, read_marks, read_state

__all__ = ["Terminal", "TerminalConfig", "read_marks", "read_state"]