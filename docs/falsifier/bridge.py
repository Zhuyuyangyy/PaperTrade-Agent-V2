"""浏览器内运行引擎的桥接层（Pyodide）。

设计约束（都是为了"不静默出错"）：

1. 引擎的 ``main(argv)`` 把报告 print 到 stdout。浏览器没有真 stdout，
   所以先把 ``sys.stdout`` 换成内存缓冲，跑完取回文本。
2. 引擎用 ``SystemExit(码)`` 传递结果（契约被篡改时是 99）。必须捕获，
   否则一次正常退出会打断整个 Pyodide 调用。
3. Pyodide 把 ``sqlite3`` 从标准库剥离（unvendored），而引擎顶层就
   ``import sqlite3``。所以宿主必须先 ``loadPackage("sqlite3")``，
   否则连 import 都过不去。装好之后浏览器里跑的是**真 sqlite3**，
   不是占位——``probe_sqlite()`` 会给出诚实结论供 UI 标注。
4. 引擎按"自己位于仓库子目录若干层下"来推断 ROOT（``_HERE.parents[1]``）。
   ``/tmp/engine.py`` 只有一层父目录，会直接 IndexError。所以这里把引擎
   放进一个足够深的虚拟目录 ``/work/web/falsifier/``，让引擎的路径推断
   逻辑原样成立——**引擎一行都不改**，保证与仓库版本逐字一致。

只做转发与布局，不含业务逻辑——业务逻辑全在 engine.py 里。
"""

import io
import json
import sys
import traceback

# 引擎推断 ROOT 需要 _HERE.parents[1] 存在，所以至少两层。
# 目录名刻意与真实仓库一致，让引擎的自述路径读起来不迷惑。
ENGINE_DIR = "/work/web/falsifier"
ENGINE_PATH = ENGINE_DIR + "/engine.py"

_installed = False


def install(engine_src, demo_candles_csv=None, demo_spec_json=None):
    """把引擎落到虚拟目录并接进 sys.path。页面与验证脚本共用这一个入口。

    engine_src        : engine.py 的源码文本
    demo_candles_csv  : 可选，写进引擎预期的 samples/ 目录，供 --demo 使用
    demo_spec_json    : 可选，同上
    """
    global _installed
    import os

    os.makedirs(ENGINE_DIR, exist_ok=True)
    with open(ENGINE_PATH, "w", encoding="utf-8") as f:
        f.write(engine_src)

    # --demo 会去 samples/ 找文件；给了就放到位，没给就让它照常报缺。
    if demo_candles_csv is not None or demo_spec_json is not None:
        samples = ENGINE_DIR + "/samples"
        os.makedirs(samples, exist_ok=True)
        if demo_candles_csv is not None:
            with open(samples + "/demo_candles.csv", "w", encoding="utf-8") as f:
                f.write(demo_candles_csv)
        if demo_spec_json is not None:
            with open(samples + "/demo_spec_csv.json", "w", encoding="utf-8") as f:
                f.write(demo_spec_json)

    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)

    sys.modules.pop("engine", None)     # 允许热替换：重复 install 生效
    _installed = True
    return engine_info()


def _capture(func, *args, **kwargs):
    """在内存里接住引擎的所有 stdout 输出。"""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = func(*args, **kwargs)
        err = None
    except SystemExit as e:                       # 引擎用 SystemExit 传退出码
        code = e.code if isinstance(e.code, int) else 0
        err = None
    except Exception:                             # 内部异常：栈留给前端显示
        code = 1
        err = traceback.format_exc()
    finally:
        sys.stdout = old
    return code, buf.getvalue(), err


def run_verify(spec_json, candles_csv, mode="report"):
    """跑一次证伪，返回 (退出码, 报告文本, JSON 对象, 错误栈)。

    spec_json    : spec 的 JSON 字符串
    candles_csv  : K 线 CSV 文本（t,o,h,l,c[,v]）
    mode         : "report" → markdown + JSON；"html" → HTML
    """
    if not _installed:
        raise RuntimeError("先调用 install(engine_src)")
    import engine

    spec_path = "/tmp/_spec.json"
    csv_path = "/tmp/_candles.csv"
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(spec_json)
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(candles_csv)

    # 引擎的 main(argv) 直接交给 argparse.parse_args，所以 argv 里不能带
    # 脚本名（那是 sys.argv[0] 的位置），否则会被当成无法识别的参数。
    argv = ["--spec", spec_path, "--candles", csv_path]
    md_path, json_path, html_path = "/tmp/_r.md", "/tmp/_r.json", "/tmp/_r.html"

    if mode == "html":
        argv += ["--out", html_path]
    else:
        argv += ["--out", md_path, "--json"]

    code, stdout_text, err = _capture(engine.main, argv)

    report_text = stdout_text
    path = html_path if mode == "html" else md_path
    try:
        with open(path, encoding="utf-8") as f:
            report_text = f.read()
    except OSError:
        pass                                     # 没落盘就用 stdout

    json_obj = None
    if mode != "html":
        try:
            with open(json_path, encoding="utf-8") as f:
                json_obj = json.load(f)
        except (OSError, ValueError):
            json_obj = None

    return code, report_text, json_obj, err


def run_demo():
    """跑内置演示（走引擎自带的合成/样本数据，不传 spec 与 CSV）。"""
    if not _installed:
        raise RuntimeError("先调用 install(engine_src)")
    import engine
    code, stdout_text, err = _capture(engine.main, ["--demo"])
    return code, stdout_text, err


def probe_sqlite():
    """sqlite3 是否真的可用——供 UI 如实标注能力边界。"""
    try:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        got = conn.execute("select 1").fetchone()
        conn.close()
        return got == (1,)
    except Exception:
        return False


def engine_info():
    """引擎自述信息，页面顶栏与页脚据此显示。"""
    info = {
        "sqlite3": probe_sqlite(),
        "installed": _installed,
        "engine_path": ENGINE_PATH,
        "python": sys.version.split()[0],
    }
    try:
        import engine
        info["has_main"] = hasattr(engine, "main")
        info["engine_file"] = getattr(engine, "__file__", ENGINE_PATH)
        info["lines"] = sum(1 for _ in open(ENGINE_PATH, encoding="utf-8"))
    except Exception as e:
        info["has_main"] = False
        info["engine_error"] = "%s: %s" % (type(e).__name__, e)
    return info