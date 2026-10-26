"""CopyWatch — event-driven on-chain follow simulation (paper only).

The package never signs transactions, never holds keys, and never sends orders.
It reads public DEX swap data, filters it, and records simulated fills in its own
SQLite ledger that is fully separate from the A/B/C Freqtrade accounts.
"""

__all__ = ["sources", "store", "risk", "agent"]
