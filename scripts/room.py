"""Build the multi-agent "desk room" page from the same read-only ledgers.

The room is a presentation layer only: every number on it comes from
dashboard.build_state(), so the visual page can never show a value the plain
dashboard would refuse to show. Missing ledgers render as "--", never as a
plausible-looking placeholder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from .dashboard import build_state
except ImportError:  # pragma: no cover - supports direct script execution
    from dashboard import build_state


def build_room_html(now: datetime | None = None) -> str:
    state = build_state(now)
    safe_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    # Prevent a local data field from closing the script element.
    safe_json = safe_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (ROOT / "templates" / "deskroom.html").read_text(encoding="utf-8")
    return template.replace("__STATE_DATA__", safe_json).replace(
        "__GENERATED_AT__", escape(state["generated_at"])
    )


def main() -> None:
    out = ROOT / "data" / "desk_room.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    out.write_text(build_room_html(now), encoding="utf-8")
    print(f"Multi-agent room generated: {out}")


if __name__ == "__main__":
    main()
