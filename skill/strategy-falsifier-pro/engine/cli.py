"""Command-line entry for the Pro package.

The engine's real CLI lives in __main__.py; this shim exists so both
`python -m engine` and `python -m engine.cli` work from an installed skill.
"""
from __future__ import annotations

from .__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
