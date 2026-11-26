"""浏览器内证伪台：网页资产必须与仓库引擎一致，且页面字段契约不能脱节。

这个页面卖的是"你看到的每个数字都来自仓库里那个引擎的同一次执行"。
两件事一旦失守，这句话就变成谎话，而且**不会报错**——页面只会静默显示
「—」，看起来像"这工具跑不出数"，不是像 bug。所以要靠测试守：

  1. 漂移守卫：docs/falsifier/engine.py 必须与 agensi 单文件引擎逐字节相同。
     网页里的引擎是手工复制的旧版本 = 用户跑的不是我们声称的那个引擎。
  2. 字段契约：index.html 里每个取值都必须对应引擎真实输出的字段名。
     引擎 JSON 有 net_sum_pct，页面写 net_return，页面就永远显示「—」。

第 2 项用静态检查实现（不需要浏览器）：把引擎真实 JSON 的字段名收集起来，
再扫页面脚本里用到的字段访问，凡是不存在于引擎输出的就报错。这比跑一次
浏览器快得多，也能在 CI 里天天跑。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "docs" / "falsifier"
ENGINE_SRC = ROOT / "skill" / "strategy-falsifier" / "agensi" / "scripts" / "verify.py"
ENGINE_WEB = WEB / "engine.py"
INDEX = WEB / "index.html"
BRIDGE = WEB / "bridge.py"
CANDLES_WEB = WEB / "demo_candles.csv"
CANDLES_SRC = ROOT / "research" / "verify" / "samples" / "demo_candles.csv"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1. 漂移守卫
# ---------------------------------------------------------------------------

def test_web_engine_is_byte_identical_to_repo_engine():
    """网页里的引擎必须就是仓库那个引擎，一个字节都不能差。

    不一致时的修法只有一条：python scripts/sync_falsifier_web.py
    """
    assert ENGINE_WEB.is_file(), (
        "缺 docs/falsifier/engine.py。跑 python scripts/sync_falsifier_web.py"
    )
    assert _sha(ENGINE_WEB) == _sha(ENGINE_SRC), (
        "网页引擎与仓库引擎不一致——用户跑的不是我们声称的那个引擎。\n"
        f"  docs: {_sha(ENGINE_WEB)[:16]}\n"
        f"  repo: {_sha(ENGINE_SRC)[:16]}\n"
        "修法：python scripts/sync_falsifier_web.py"
    )


def test_web_demo_candles_identical_to_repo_samples():
    """演示 K 线必须与仓库 samples 同源，否则页面与命令行跑出不同数字。"""
    assert CANDLES_WEB.is_file(), "缺 docs/falsifier/demo_candles.csv"
    assert _sha(CANDLES_WEB) == _sha(CANDLES_SRC), (
        "网页演示 K 线与仓库 samples 不一致。修法：python scripts/sync_falsifier_web.py"
    )


def test_sync_script_check_mode_passes():
    """同步脚本的 --check 必须通过——这是 CI 里守漂移的那个开关。"""
    import subprocess
    import sys
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_falsifier_web.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert r.returncode == 0, f"--check 失败：\n{r.stdout}\n{r.stderr}"


# ---------------------------------------------------------------------------
# 2. 页面资产完整性
# ---------------------------------------------------------------------------

def test_all_deploy_assets_present():
    """部署目录必须自带全部运行所需文件——少一个页面就永远加载不出引擎。"""
    for name in ("index.html", "engine.py", "bridge.py", "demo_candles.csv"):
        p = WEB / name
        assert p.is_file(), f"缺部署文件 docs/falsifier/{name}"
        assert p.stat().st_size > 0, f"docs/falsifier/{name} 是空文件"


def test_index_html_is_self_contained():
    """页面不许引用外部 JS/CSS 文件（除 Pyodide CDN）——离线打开也要能用。"""
    html = INDEX.read_text(encoding="utf-8")
    # 不该有 <script src="..."> 指向本地文件
    for m in re.finditer(r'<script[^>]+src="([^"]+)"', html):
        src = m.group(1)
        assert src.startswith("http"), f"页面引用了本地脚本 {src}，会让离线打开失败"
    # 不该有 <link rel=stylesheet href=本地文件>
    for m in re.finditer(r'<link[^>]+href="([^"]+)"', html):
        href = m.group(1)
        assert href.startswith("http"), f"页面引用了本地样式 {href}"


def test_bridge_hands_argv_without_prog_name():
    """引擎 main(argv) 直接交给 argparse，argv 里不能有脚本名。

    这条曾经真的坏过：传 ["verify.py", "--spec", ...] 会被 argparse 当成
    无法识别的参数，退出码 2，页面永远拿不到 JSON。
    """
    src = BRIDGE.read_text(encoding="utf-8")
    assert '"verify.py"' not in src, (
        "bridge.py 里又把脚本名塞进 argv 了。引擎的 main(argv) 直接传给 "
        "argparse.parse_args，argv 里不能含 sys.argv[0] 那一位。"
    )
    assert "argv = [" in src, "bridge.py 里找不到 argv 构造，结构可能已变"


def test_bridge_places_engine_in_deep_enough_dir():
    """引擎用 _HERE.parents[1] 推断 ROOT，路径层级不够会 IndexError。"""
    src = BRIDGE.read_text(encoding="utf-8")
    m = re.search(r'ENGINE_DIR\s*=\s*"([^"]+)"', src)
    assert m, "bridge.py 里找不到 ENGINE_DIR 定义"
    depth = len([p for p in m.group(1).split("/") if p])
    assert depth >= 3, (
        f"ENGINE_DIR={m.group(1)} 只有 {depth} 层，引擎的 _HERE.parents[1] 会越界"
    )


# ---------------------------------------------------------------------------
# 3. 字段契约：页面取值必须对应引擎真实字段
# ---------------------------------------------------------------------------

def _engine_json_fields() -> set[str]:
    """跑一次真实引擎，收集它输出 JSON 里出现过的全部字段名。"""
    import subprocess
    import sys
    import tempfile

    spec = {
        "name": "contract-probe", "hypothesis": "字段契约探测", "source": "test",
        "symbol": "BTCUSDT", "interval": "1h", "venue": "spot",
        "fee_rate": 0.0004, "fill_model": "taker", "funding": False,
        "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
        "signals": {"kind": "rule", "rule": "rsi_below", "period": 14,
                    "threshold": 30, "side": "long"},
        "control": {"enabled": True, "n_random": 30, "seed": 42},
        "walk_forward": {"enabled": True, "folds": 2},
        "param_grid": {"axes": {"take_profit": [0.02, 0.03],
                                "stop_loss": [-0.06, -0.08]}},
        "fee_multiples": [1, 2],
        "falsify_if": {"control_lift_ci_includes_zero": True,
                       "worst_case_net_negative": True, "min_fills": 10},
    }
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        out = td / "r.md"
        r = subprocess.run(
            [sys.executable, str(ENGINE_SRC),
             "--spec", str(td / "spec.json"),
             "--candles", str(CANDLES_SRC),
             "--out", str(out), "--json"],
            capture_output=True, text=True, cwd=str(td), timeout=120,
        )
        jpath = out.with_suffix(".json")
        assert jpath.is_file(), f"引擎没产出 JSON（rc={r.returncode}）：{r.stderr[-800:]}"
        payload = json.loads(jpath.read_text(encoding="utf-8"))

    fields: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                fields.add(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return fields


# 页面脚本里允许出现、但不是引擎字段的标识符（DOM id、局部变量、格式化函数等）
_NOT_ENGINE_FIELDS = {
    "id", "className", "textContent", "innerHTML", "value", "srcdoc", "style",
    "disabled", "length", "map", "filter", "join", "slice", "split", "replace",
    "includes", "toFixed", "toString", "push", "entries", "keys", "concat",
    "result", "kind", "tag", "cls", "t", "m", "d", "k", "v", "n", "x", "s", "c",
    "json", "mdText", "spec", "csv", "html", "out", "info", "rc", "err",
}


# JS / Python 内置方法与 DOM 属性：与引擎字段无关，扫描时跳过
_JS_BUILTINS = {
    "trim", "replace", "split", "join", "map", "filter", "slice", "includes",
    "toString", "toFixed", "concat", "push", "entries", "keys", "length",
    "textContent", "innerHTML", "className", "srcdoc", "style", "value",
    "disabled", "id", "src", "onload", "onerror", "dumps", "loads", "globals",
    "addEventListener", "querySelector", "remove", "add", "contains", "message",
    "stack", "target", "files", "then", "catch", "all", "stringify", "parse",
    "now", "match", "test", "display", "head", "set", "parents",
}


def test_page_field_access_matches_engine_schema():
    """页面从引擎 JSON 上取的每个字段必须真实存在。

    这条守的是最阴的一类 bug：字段名写错不报错，页面静默显示「—」。
    实锤过一次：页面读 runs.worst_case.net_return，引擎给的是
    runs.worst_case.stats.sum_pct，KPI 卡永远空着。

    做法：只扫 render() 函数体（所有引擎 JSON 都只在这里被消费），
    且只扫"直接指向引擎 JSON 某一块的变量"上的属性访问。
    """
    fields = _engine_json_fields()
    js = re.search(r"<script>([\s\S]*)</script>", INDEX.read_text(encoding="utf-8")).group(1)

    # 只取 render 函数体——这是唯一消费引擎 JSON 的地方
    start = js.index("function render(")
    depth = 0
    end = start
    for i in range(js.index("{", start), len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = js[start:end]
    assert len(body) > 2000, f"render 函数体只提取到 {len(body)} 字符，提取逻辑可能坏了"

    # 这些变量在 render 里都直接指向引擎返回的某一块 JSON
    engine_vars = ["json", "v", "worst", "ws", "best", "bs", "ctl", "bh",
                   "wf", "oos", "br", "isg", "osg", "s", "c"]

    checked = 0
    bad: list[str] = []
    for var in engine_vars:
        for fm in re.finditer(rf"\b{var}\.([a-z_][a-z0-9_]*)\b", body):
            field = fm.group(1)
            if field in _JS_BUILTINS:
                continue
            checked += 1
            if field not in fields:
                bad.append(f"{var}.{field}")

    assert checked >= 25, f"只扫到 {checked} 处字段访问，正则可能失效了"
    assert not bad, (
        "页面从引擎 JSON 上读了不存在的字段（会静默显示「—」）：\n  "
        + "\n  ".join(sorted(set(bad)))
    )


def test_page_kpi_values_are_all_populated():
    """四个 KPI 的取值路径必须在引擎输出里有对应字段，否则卡片永远空着。

    逐个点名，因为这些是最显眼的位置——空一格就毁掉整个页面的可信度。
    """
    fields = _engine_json_fields()
    required = {
        "n_signals": "成交笔数",
        "sum_pct": "最坏口径净收益",
        "diff_pct": "随机对照差值 / 买入持有差值",
        "diff_ci": "对照差值的 95% 区间",
        "selectivity": "选择性是否已证明",
        "matched_bh_pct": "同期买入持有收益",
        "exposure": "在场时间占比",
        "stats": "runs.worst_case 的统计块",
        "worst_case": "最坏口径回放",
        "best_case": "最好口径回放",
    }
    missing = [f"{k}（{why}）" for k, why in required.items() if k not in fields]
    assert not missing, "引擎输出里缺这些 KPI 依赖字段：\n  " + "\n  ".join(missing)


def test_page_no_legacy_wrong_field_names():
    """曾经写错的那几个字段名，不许再回来。

    这些是真实踩过的坑，各自都让一整块 UI 静默变空。
    """
    js = re.search(r"<script>([\s\S]*)</script>", INDEX.read_text(encoding="utf-8")).group(1)
    legacy = {
        "net_return": "引擎用 sum_pct（百分数），不是 net_return（小数率）",
        "round_trip_bps": "引擎用 fee_bps，不是 round_trip_bps",
        "ctl.lift": "引擎用 control.diff_pct，不是 lift",
        "bh.net_return": "引擎用 buy_and_hold.diff_pct / matched_bh_pct",
    }
    hits = [f"{k} —— {why}" for k, why in legacy.items() if k in js]
    assert not hits, "页面里还留着旧的错字段名：\n  " + "\n  ".join(hits)


# ---------------------------------------------------------------------------
# 4. 页面不越权：它只是展示层
# ---------------------------------------------------------------------------

def test_page_contains_no_strategy_logic():
    """页面不许自己算收益、胜率、显著性——所有数字必须来自引擎。

    一旦页面开始自己算，就出现了"两套数字"，而页面上的那套没有测试守着。
    """
    js = re.search(r"<script>([\s\S]*)</script>", INDEX.read_text(encoding="utf-8")).group(1)
    forbidden = [
        r"Math\.(?:pow|sqrt|log|exp)\s*\(",   # 自己算复利/统计量
        r"reduce\s*\(\s*\(?[a-z]",            # 自己聚合
    ]
    for pat in forbidden:
        assert not re.search(pat, js), (
            f"页面脚本里出现了疑似自算逻辑 {pat}——所有数字必须来自引擎输出"
        )


def test_page_does_not_send_data_anywhere():
    """页面不许把用户数据发出去。除了 Pyodide CDN，不该有任何外部地址。"""
    html = INDEX.read_text(encoding="utf-8")
    urls = set(re.findall(r"https?://([^/\s\"'<>]+)", html))
    allowed_hosts = {
        "cdn.jsdelivr.net",              # Pyodide 运行时
        "github.com",                    # 页脚源码链接
        "zhuyuyangyy.github.io",         # 自身域名
        "img.shields.io",                # 可能有的徽章
        "pyodide.org",
    }
    unexpected = {u for u in urls if u not in allowed_hosts}
    assert not unexpected, f"页面里出现了意料外的外部地址：{sorted(unexpected)}"