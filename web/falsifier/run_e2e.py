"""一条命令内起本地服务、跑浏览器 E2E、然后关掉服务。

为什么要自己起服务：后台进程会随 shell 任务结束被杀，跨命令依赖
"服务还活着"是不可靠的。

为什么要本地复制 Pyodide：页面默认从 CDN 取运行时，但测试环境未必能
访问外网。这里把 node_modules/pyodide 复制进临时服务目录，并用
window.PYODIDE_BASE 指过去——测的还是同一份页面代码，只是换个运行时
来源。生产路径不受影响。
"""
import http.server
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVE_DIR = ROOT / "docs" / "falsifier"
WORK_DIR = Path(__file__).resolve().parent
PYODIDE_LOCAL = WORK_DIR / "node_modules" / "pyodide"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    script = os.environ.get("E2E_SCRIPT", "e2e_browser.js")
    if not (PYODIDE_LOCAL / "pyodide.js").is_file():
        print("缺本地 pyodide：在 web/falsifier 下跑 npm install pyodide")
        return 2

    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        # 把页面资产 + 本地 pyodide 一起铺进临时目录，页面代码一个字不改
        for f in SERVE_DIR.iterdir():
            if f.is_file():
                shutil.copy2(f, stage / f.name)
        shutil.copytree(PYODIDE_LOCAL, stage / "pyodide")

        # 注入运行时基址：不改源页面，只在测试副本的 head 里插一行
        idx = stage / "index.html"
        html = idx.read_text(encoding="utf-8")
        html = html.replace(
            "<head>",
            '<head>\n<script>window.PYODIDE_BASE = "pyodide";</script>',
            1,
        )
        idx.write_text(html, encoding="utf-8")

        port = free_port()
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(stage))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        httpd.log_message = lambda *a, **k: None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)

        url = f"http://127.0.0.1:{port}/"
        print(f"服务已起: {url}  (页面 assets + 本地 pyodide)")

        env = dict(os.environ, FALSIFIER_URL=url)
        try:
            r = subprocess.run(
                ["node", str(WORK_DIR / script)],
                cwd=str(WORK_DIR), env=env,
            )
            return r.returncode
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())