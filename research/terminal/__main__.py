"""``python -m research.terminal``：本机只读终端。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .server import Terminal, TerminalConfig


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="research.terminal")
    ap.add_argument("--root", default="data/openalpha_forward_v1",
                    help="运行目录（含三本账与 data/*.jsonl）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default="",
                    help="绑定非本机地址时必填；浏览器用 ?token= 或 Bearer")
    args = ap.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print("拒绝：绑定非本机地址必须提供 --token", file=sys.stderr)
        return 2
    cfg = TerminalConfig(Path(args.root), args.host, args.port, args.token)
    term = Terminal(cfg)
    server = term.serve()
    print(f"OpenAlpha Terminal: http://{args.host}:{args.port}")
    print(f"root: {cfg.root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())