"""Serve a local, read-only dashboard that refreshes from the paper ledgers.

The server binds only to loopback, makes no outbound requests, and exposes no order
or credential endpoints. Each page refresh rebuilds the snapshot from local SQLite
ledgers and validated signal files.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dashboard import build_html
from scripts.room import build_room_html

REFRESH_META = '<meta http-equiv="refresh" content="15">'


def render_live_page() -> str:
    page = build_html()
    if "</head>" not in page:
        raise RuntimeError("Dashboard template is missing </head>")
    return page.replace("</head>", f"{REFRESH_META}\n</head>", 1)


def render_room_page() -> str:
    page = build_room_html()
    if "</head>" not in page:
        raise RuntimeError("Room template is missing </head>")
    return page.replace("</head>", f"{REFRESH_META}\n</head>", 1)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PaperTradeDashboard/1.0"

    def _send_html(self, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler interface
        if self.path in ("/", "/index.html"):
            self._send_html(render_live_page())
            return
        if self.path in ("/room", "/room/", "/room.html"):
            self._send_html(render_room_page())
            return
        if self.path == "/healthz":
            body = json.dumps({"status": "ok", "paper_only": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"Not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print("[dashboard] " + (fmt % args), flush=True)


def main() -> None:
    try:
        port = int(os.environ.get("PAPER_DASHBOARD_PORT", "8765"))
    except ValueError as exc:
        raise SystemExit("PAPER_DASHBOARD_PORT must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise SystemExit("PAPER_DASHBOARD_PORT must be between 1024 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"PaperTrade read-only dashboard: http://127.0.0.1:{port}", flush=True)
    print("Bound to localhost only; refreshes every 15 seconds; no order endpoints.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Dashboard server stopping", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
