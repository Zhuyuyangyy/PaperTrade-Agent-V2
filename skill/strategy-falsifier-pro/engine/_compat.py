"""Compat shims for the flat engine package.

DataError is lifted verbatim from research/audit/binance.py (that module's
network code does not ship -- the engine is offline by construction).
"""
from __future__ import annotations


class DataError(RuntimeError):
    """Raised when Binance could not be read after retrying."""
