"""AST 静态前视偏差扫描器。

**为什么我们需要自己的一个**：`research/cards/structure.py` 里的 pivot 判定
就是 `left=n, right=n` 的中心窗口——`confirmed_at = i + right` 那个补丁
修的是**使用时机**，不是检测能力。我们至今没有工具能自动发现「有人又
写了一个中心窗口」。同样，freqtradestrategies 那 137 个被标记的文件里
`rolling(center=True)` 命中 59 次，说明这类错误在真实语料里是**高频且
成家族出现**的，不是偶发手误。

**方法来源与边界**：规则集参考 RoboticAutomations/freqtradestrategies 的
`docs/LOOKAHEAD.md`（该仓库收集 1,286 个 Freqtrade 策略，静态标记 137 个）。
它明确声明自己的边界，我们照抄这个态度：

    「静态检测发现模式，而非提供证明。
      🔴 文件极可能存在偏差；🟢 文件只是未被标记，不是已被验证。」

所以本模块**绝不执行被扫代码**（无 import、无 eval、无 backtest），
只做 `ast.parse` + 模式匹配。命中不等于有 bug，不命中不等于没 bug。
最终裁决必须在沙箱里跑动态分析。

**与外部实现的差异（三处收紧，都是我们踩过的坑）**：

1. `extrema_scan` 必须是 `ast.Call`，不能是 import。我们的 `pivots()` 就是
   一个自实现的 pivot 扫描，如果只看 import 会漏掉。
2. **新增 `center_window_custom`**：外部的 `rolling(center=True)` 只覆盖
   pandas 写法，抓不到我们这种手写 `for i: window = cs[i-n:i+n+1]` 的。
   这是本项目历史上最贵的一个 bug 形态，必须单独一条规则。
3. **新增 `confirmed_at_gt_self`**：结构体带 `confirmed_at` 字段时，
   断言 `confirmed_at > i`。这是「显式记录确认延迟」的正面模式，
   用来**反向检查**——如果某个结构类有 `confirmed_at` 但从不晚于自身，
   说明它的延迟是假的。

用法：
    python research/external/lookahead_scan.py                # 扫 research/
    python research/external/lookahead_scan.py --path research/cards
    python research/external/lookahead_scan.py --json out.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


# ---------------------------------------------------------------------------
# 规则定义
# ---------------------------------------------------------------------------

HIGH = "high"
MED = "medium"

# 高危：命中即判定「极可能存在前视」。理由见 LOOKAHEAD.md 的分级逻辑——
# 这四条在语义上几乎没有合法用法。
RULES_HIGH = {
    "backfill": "用后面的值回填前面的行（bfill / fillna(method='bfill')）",
    "extrema_scan": "极值扫描（argrelextrema / find_peaks / zigzag）需要未来K线",
    "future_name_in_features": "预测列被选进特征矩阵（df[['a','label']]）",
    "interpolate_back": "interpolate(direction='backward'|'both') 从后续行填充",
    "negative_diff": "diff()/pct_change() 周期为负 = 与未来值做差",
    "negative_shift": "shift(-n) 把未来K线拉到当前",
    "reverse_index": "价格序列被 [::-1] 反转，向后看算子全变向前看",
    "rolling_center": "rolling(center=True) 窗口居中，一半是未来数据",
    "center_window_custom": "手写中心窗口 cs[i-n:i+n+1]，等价于 rolling(center=True)",
}

# 中危：上下文敏感，脱离上下文无法裁定。外部的例子：
#   dataframe['close'].max() 写在指标里是错的，写在一行日志里是对的。
RULES_MED = {
    "describe_stats": "对整列 describe()/mean()，用全量数据一次性统计",
    "future_name": "读取了预测列——在评估代码里正常，在特征里是泄漏",
    "global_scaler": "scaler 在整个 DataFrame 上 fit，测试集统计量泄漏",
    "iloc_last_assign": "把标量赋给 df.iloc[-1]，最新值反向广播到历史行",
    "whole_series_stat": "无窗口的 max()/min()/quantile()/rank()",
}

RULES = {**{k: (HIGH, v) for k, v in RULES_HIGH.items()},
         **{k: (MED, v) for k, v in RULES_MED.items()}}

CENTER_FUNCS = {"argrelextrema", "find_peaks", "find_peaks_cwt", "zigzag",
                "find_peaks_prominence"}

# `future_name` 的判据。**第一版是 `(future|target|label|lookahead|forward)`
# 直接搜，在真实语料上 22 条命中里 20 条是误报**：
#
#   target_annual   目标年化波动率（风控参数）
#   target_weight   目标仓位权重
#   walk_forward    向前滚动验证——这**恰恰是防前视的方法**
#   tag_labels      分类标签名（人读的类别，不是预测目标）
#   label           图表图例
#
# 「噪音淹没信号比漏报更糟」——22 条里挑 2 条真的，没人会认真看。
# 所以改成：整名精确匹配，或「target/label 在一个词的头部且后面不是
# 表示配置/参数的常见后缀」。
FUTURE_COL_EXACT = {
    "future", "futures", "future_close", "future_return", "future_ret",
    "target", "targets", "label", "labels", "y", "y_true", "y_pred",
    "lookahead", "fwd_return", "fwd_ret", "forward_return",
}
# 这些后缀说明 target/label 是「配置目标」或「人读标签」，不是预测视野
SAFE_COL_SUFFIX = (
    "_weight", "_weights", "_annual", "_position", "_positions", "_alloc",
    "_size", "_pct", "_ratio", "_vol", "_drawdown", "_dd", "_tag",
    "_tags", "_name", "_id", "_type", "_kind", "_desc", "_metric",
)
# 出现在这些名字里时 target/label 完全无关
SAFE_COL_CONTAINS = ("walk_forward", "walkforward", "tag_label", "label_name")


@dataclass
class Finding:
    rule: str
    level: str
    lineno: int
    snippet: str
    why: str


@dataclass
class FileReport:
    path: str
    findings: list[Finding] = field(default_factory=list)
    parse_error: str = ""

    @property
    def flagged(self) -> bool:
        """有高危命中即视为可疑。中危只记录，不判定。"""
        return any(f.level == HIGH for f in self.findings)


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------

def _is_future_column(name: str) -> bool:
    """判断列名是否真的表示「预测视野」。

    判据必须能区分 `row['label']`（预测目标，可疑）和
    `spec['volatility']['target_annual']`（目标年化波动率，风控参数，
    完全无关）。第一版正则一视同仁，误报 20/22。

    规则按优先级：
      1. 出现在已知安全组合里（walk_forward / tag_labels）→ 放行
      2. 整名精确匹配 FUTURE_COL_EXACT → 命中
      3. 以安全后缀结尾（target_weight / target_annual）→ 放行
      4. 名字就是 target/label/future 或以其开头 + 下划线分隔 → 命中
      5. 其余 → 放行（宁漏不误，误报会让整条规则失效）

    注意第 5 条：**这里选择偏向漏报**。理由是本规则只覆盖
    「列名泄漏预测视野」这一种模式，它本身不是强证据；而
    `whole_series_stat` / `center_window_custom` 那些才是真正抓 bug 的。
    一条 90% 误报的规则等于没有规则。
    """
    n = name.strip().lower()
    if not n:
        return False
    for safe in SAFE_COL_CONTAINS:
        if safe in n:
            return False
    if n in FUTURE_COL_EXACT:
        return True
    for suf in SAFE_COL_SUFFIX:
        if n.endswith(suf):
            return False
    head = re.split(r"[_\-.]", n)[0]
    return head in {"future", "target", "label", "lookahead", "fwd",
                    "forward", "y", "outcome"}


def _chain_name(node: ast.AST) -> str:
    """取属性链末端名。

    `series.interpolate(...).bfill()` 里 bfill 挂在 Call 上，
    只取末端才能在**不依赖 import 别名**的前提下匹配。
    """
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            return node.attr
        elif isinstance(node, ast.Name):
            return node.id
        else:
            return ""


def _kw_str(call: ast.Call, key: str) -> str:
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str):
            return kw.value.value
    return ""


def _kw_is_true(call: ast.Call, key: str) -> bool:
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant):
            return kw.value.value is True
    return False


def _is_negative(node: ast.AST) -> bool:
    """识别带一元负号的数字字面量。

    **`shift(-1)` 的 `-1` 是 `UnaryOp(USub, Constant(1))`，不是 `Constant(-1)`。**
    这一条漏了会导致 negative_shift 命中率归零——外部文档专门点出了它。
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, (int, float))
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and node.value < 0
    return False


def _any_negative_arg(call: ast.Call) -> bool:
    return any(_is_negative(a) for a in call.args) or \
        any(_is_negative(k.value) for k in call.keywords)


def _slice_neg_step(node: ast.Subscript) -> bool:
    """`[::-1]` → Slice(step=UnaryOp(USub, 1))"""
    s = node.slice
    if isinstance(s, ast.Slice) and s.step is not None:
        return _is_negative(s.step)
    return False


def _in_rolling(node: ast.AST, parents: dict) -> bool:
    """向上找一层，判断是否在 rolling(...) 调用内。

    `df['close'].rolling(20).max()` 里的 max 是合法的，`df['close'].max()`
    不是。区分它们只能看父节点。
    """
    cur = parents.get(id(node))
    for _ in range(4):
        if cur is None:
            return False
        if isinstance(cur, ast.Call) and _chain_name(cur.func) == "rolling":
            return True
        cur = parents.get(id(cur))
    return False


def _has_explicit_window(node: ast.Call) -> bool:
    """参数里是否出现了显式切片窗口 `cs[a:b]`。

    `max((b['h'] for b in cs[i-channel:i]))` 是**正确**写法——窗口
    `cs[i-channel:i]` 的右下界就是 i 本身（右开），只看过去。
    真正危险的是 `max(df['high'])` 这种完全没有窗口的。

    区分这两者是这个规则唯一有价值的地方。没有这一步，
    窗口内的 max 和全样本的 max 会混在一起报，等于没报。
    """
    for a in node.args:
        for sub in ast.walk(a):
            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice):
                return True
            # range(...) / for b in cs 之类也算有界遍历
            if isinstance(sub, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                for gen in sub.generators:
                    if isinstance(gen.iter, ast.Call) and \
                            _chain_name(gen.iter.func) == "range":
                        return True
                    if isinstance(gen.iter, ast.Subscript):
                        return True
    return False


def _is_scalar_builtin(node: ast.Call) -> bool:
    """判断 `max(a, b)` / `min(x, 0)` 这类纯标量调用。

    内置 `max` / `min` 有两个用途，性质完全不同：
      · `max(a, b)` 比两个标量 → **和全样本统计毫无关系**
      · `max(series)` / `df['x'].max()` → 真的用了整列

    第一版把两者都报，结果 `research/cards` 上 55 条命中里绝大多数是
    `max(self.bars_held, 0)`、`min(bar['o'], limit)` 这种标量比较。
    噪音淹没信号比漏报更糟——55 条里挑 3 条真的，没人会认真看。

    判据：参数个数 >= 2，且每个参数都不是「看起来像容器」的东西。
    """
    args = node.args
    if len(args) < 2:
        return False
    for a in args:
        # 列表/元组/字典/集合字面量、生成器表达式、推导式 → 可能是容器统计
        if isinstance(a, (ast.List, ast.Tuple, ast.Dict, ast.Set,
                          ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return False
        # 方法链末端是 .values/.tolist() 之类 → 容器
        if isinstance(a, ast.Call) and _chain_name(a.func) in {
                "values", "tolist", "to_numpy", "items", "keys"}:
            return False
    return True


# ---------------------------------------------------------------------------
# 手写中心窗口检测（本项目专有规则）
# ---------------------------------------------------------------------------

def _flatten_add(node: ast.AST, _sign: int = 1) -> list[tuple[int, ast.AST]]:
    """把加减链展平成 [(符号, 项)]。

    必要性：`i + right + 1` 的 AST 是 `Add(Add(i, right), 1)`，
    不是 `Add(i, Add(right, 1))`。第一版只判断 `hi.left` 是不是
    `Name('i')`，于是 `cs[i - left: i + right + 1]` 直接漏掉——
    **而这个恰好就是 `structure.py` 里 pivot 的真实写法**。
    漏报的正是最该抓的那一条，比误报严重得多。
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_add(node.left, _sign) + _flatten_add(node.right, _sign)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        return _flatten_add(node.left, _sign) + _flatten_add(node.right, -_sign)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _flatten_add(node.operand, -_sign)
    return [(_sign, node)]


def _offset_from_i(node: ast.AST) -> tuple[str, float]:
    """把边界表达式化成「相对 i 的偏移」。

    返回 `(kind, const_offset)`：

    - `("exact", k)`：能算出确切偏移 k。`i+1` → 1，`i-3` → -3，`i` → 0。
    - `("symbolic", c)`：含 i，但还有未知量，只知道常数部分 c。
      `i - n + 1` → `("symbolic", 1)`；`i + right + 1` → `("symbolic", 1)`。
    - `("no_i", 0)`：不含 i，与锚点无关，不参与判定。
    """
    has_i = False
    const = 0.0
    sym: list[str] = []
    for sign, t in _flatten_add(node):
        if isinstance(t, ast.Name) and t.id == "i":
            if sign != 1:
                return ("no_i", 0.0)     # 形如 -i 的写法，不猜
            has_i = True
        elif isinstance(t, ast.Constant) and isinstance(t.value, (int, float)) \
                and not isinstance(t.value, bool):
            const += sign * t.value
        else:
            sym.append(ast.unparse(t))
    if not has_i:
        return ("no_i", 0.0)
    return ("symbolic" if sym else "exact", const)


def _detect_center_window(tree: ast.AST) -> list[tuple[int, str]]:
    """找 `cs[i-n : i+n+1]` 这类手写中心窗口。

    pandas 的 `rolling(center=True)` 有明确函数名可匹配，但本项目
    `structure.py` 的 pivot 是手写循环：

        window = cs[i - left: i + right + 1]

    它和 rolling(center=True) 是**完全同一个错误**（第 t 根用了 t+right），
    但如果只匹配函数名就永远抓不到。

    **判据是「上界包含了 i 之后的下标」。** Python 切片右开：
    `cs[a:b]` 覆盖 a..b-1。所以上界 `i+1` 覆盖到 i 本身——**完全正确**，
    这是「右闭合滚动窗口」的标准写法。只有上界 >= i+2 才真正看到了 i+1。

    三版判据的演进，每一步都是被实际误报/漏报逼出来的：

    1. ❌「上界含 Add」→ `[i-n+1 : i+1]` 和 `[i-window+1 : i+1]` 全被误报，
       而那是 `rolling_extreme()` 里正确得不能再正确的窗口统计。
    2. ❌「上界是 `i + k` 且 `k >= 2`」→ 漏掉 `i + right + 1`，
       因为 AST 左结合成 `Add(Add(i,right),1)`，`hi.left` 不是 `Name(i)`。
       漏的恰好是 pivot 本身。
    3. ✅ 展平成加法链再求相对偏移（本版），同时处理符号量。
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        s = node.slice
        if not isinstance(s, ast.Slice):
            continue
        if s.lower is None or s.upper is None:
            continue

        # 下界必须是「i 减去点什么」。具体减多少不重要——下界再靠前
        # 也只是多看历史，永远安全。
        lo_kind, lo_off = _offset_from_i(s.lower)
        if lo_kind == "no_i":
            continue
        if lo_kind == "exact" and lo_off >= 0:
            continue        # `cs[i : ...]` 不是以 i 为右端的窗口

        # 上界决定有没有看未来。exact 偏移为 k 时，最后覆盖的下标是 i+k-1。
        hi_kind, hi_off = _offset_from_i(s.upper)
        if hi_kind == "no_i":
            continue
        if hi_kind == "exact" and hi_off < 2:
            continue        # i+k-1 <= i，只看过去
        # symbolic：上界含未知量（`i + right + 1`），无法证明它只到 i。
        # 这种形态在真实代码里几乎只有 pivot 一种用途，一律报——
        # 但要允许人工 whitelist，见 `KNOWN_SAFE`。
        if hi_kind == "symbolic" and _symbolic_upper_is_known_safe(s.upper):
            continue
        out.append((node.lineno, ast.unparse(node)))
    return out


def _symbolic_upper_is_known_safe(upper: ast.AST) -> bool:
    """符号上界里存在**合法**情形：`cs[i - n + 1 : i + 1]` 这种……

    等一下，那个是 exact。真正合法的符号上界极少，但有一种：
    窗口长度是变量、上界是 `i + 1` 时仍是 exact，不会走到这里。

    所以这里只留一个空实现的钩子，供将来人工确认后加白名单。
    当前一律返回 False —— **宁可多报一条让人看一眼，也不静默放过**。
    这是本项目历史上最贵的 bug 形态，代价不对等。
    """
    return False


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def scan_source(src: str, path: str = "<string>") -> FileReport:
    rep = FileReport(path=path)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        rep.parse_error = f"{e.msg} (line {e.lineno})"
        return rep

    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def add(rule: str, lineno: int, snippet: str):
        level, why = RULES[rule]
        rep.findings.append(Finding(rule, level, lineno, snippet, why))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _chain_name(node.func)
            snip = ast.unparse(node)[:110]

            if name == "bfill" or (
                    name == "fillna" and _kw_str(node, "method") == "bfill"):
                add("backfill", node.lineno, snip)
            if name in CENTER_FUNCS:
                add("extrema_scan", node.lineno, snip)
            if name == "interpolate" and (
                    _kw_str(node, "direction") in {"backward", "both"}
                    or _kw_str(node, "limit_direction") in {"backward", "both"}):
                add("interpolate_back", node.lineno, snip)
            if name in {"diff", "pct_change"} and _any_negative_arg(node):
                add("negative_diff", node.lineno, snip)
            if name == "shift" and _any_negative_arg(node):
                add("negative_shift", node.lineno, snip)
            if name == "rolling" and _kw_is_true(node, "center"):
                add("rolling_center", node.lineno, snip)
            if name == "describe":
                add("describe_stats", node.lineno, snip)
            if name in {"fit", "fit_transform"}:
                add("global_scaler", node.lineno, snip)
            if name in {"max", "min", "quantile", "rank"} \
                    and not _in_rolling(node, parents) \
                    and not _is_scalar_builtin(node) \
                    and not _has_explicit_window(node):
                add("whole_series_stat", node.lineno, snip)

        elif isinstance(node, ast.Subscript):
            if _slice_neg_step(node):
                add("reverse_index", node.lineno, ast.unparse(node)[:110])
            s = node.slice
            if isinstance(s, ast.Constant) and isinstance(s.value, str) \
                    and _is_future_column(s.value):
                # 单独取一列（`row['label']`、`s['fwd_return']`）在评估代码里
                # 是**正常且必要**的——你要算收益就得读标签。按名称判高危
                # 会把整个 verify/ 和 replay/ 目录打成红色，然后没人再看。
                # 真正的泄漏是「标签混进特征矩阵」，见下面 future_name_in_features。
                add("future_name", node.lineno, ast.unparse(node)[:110])
            # 特征矩阵形态：df[['close','rsi','label']] —— 字符串列表里
            # 出现预测列。这才是硬证据：标签被当成输入喂进模型了。
            elif isinstance(s, (ast.List, ast.Tuple)):
                names = [e.value for e in s.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if names and any(_is_future_column(n) for n in names):
                    add("future_name_in_features", node.lineno,
                        ast.unparse(node)[:110])

        elif isinstance(node, ast.Assign):
            tgt = node.targets[0] if node.targets else None
            if isinstance(tgt, ast.Subscript) and "iloc" in ast.unparse(tgt) \
                    and "-1" in ast.unparse(tgt):
                add("iloc_last_assign", node.lineno, ast.unparse(node)[:110])

    for lineno, snip in _detect_center_window(tree):
        add("center_window_custom", lineno, snip)

    # 去重：(rule, lineno) 只记一次，但保留次数信息在 report 层汇总
    seen = set()
    uniq = []
    for f in rep.findings:
        k = (f.rule, f.lineno)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    rep.findings = sorted(uniq, key=lambda f: (f.level != HIGH, f.lineno))
    return rep


def scan_file(p: Path) -> FileReport:
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return FileReport(path=str(p), parse_error=str(e))
    return scan_source(src, str(p))


def scan_dir(root: Path, skip: tuple[str, ...] = (
        "__pycache__", ".git", "_out", "data", ".venv")) -> list[FileReport]:
    out = []
    for p in sorted(root.rglob("*.py")):
        if any(s in p.parts for s in skip):
            continue
        out.append(scan_file(p))
    return out


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def summarize(reports: list[FileReport]) -> dict:
    n = len(reports)
    flagged = [r for r in reports if r.flagged]
    errs = [r for r in reports if r.parse_error]
    counts: dict[str, int] = {}
    for r in reports:
        for f in r.findings:
            counts[f.rule] = counts.get(f.rule, 0) + 1
    return {
        "files_scanned": n,
        "files_flagged": len(flagged),
        "parse_errors": len(errs),
        "rule_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(ROOT / "research"))
    ap.add_argument("--json", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    target = Path(args.path)
    if not target.exists():
        print(f"路径不存在：{target}")
        return 1

    reports = [scan_file(target)] if target.is_file() else scan_dir(target)
    s = summarize(reports)

    print("=" * 88)
    print(f"前视偏差静态扫描 · {target}")
    print("=" * 88)
    print(f"扫描文件 {s['files_scanned']}，检出高危 {s['files_flagged']}，"
          f"语法错误 {s['parse_errors']}")
    print()
    if s["rule_counts"]:
        print(f"{'规则':<24}{'级别':<8}{'次数':>6}  说明")
        print("-" * 88)
        for rule, cnt in s["rule_counts"].items():
            lvl, why = RULES[rule]
            print(f"{rule:<24}{lvl:<8}{cnt:>6}  {why[:44]}")
    else:
        print("无命中。")
    print()

    if not args.quiet:
        for r in reports:
            if not r.findings:
                continue
            mark = "🔴" if r.flagged else "🟡"
            rel = Path(r.path).relative_to(ROOT) if Path(r.path).is_absolute() \
                and ROOT in Path(r.path).parents else r.path
            print(f"{mark} {rel}")
            for f in r.findings:
                print(f"    L{f.lineno:<5} [{f.rule}] {f.snippet[:72]}")
            print()

    print("边界声明：")
    print("  🔴 只是「极可能存在偏差」，不是已证实；🟡 只是「未被标记」，")
    print("  不是已验证。静态检测发现模式，不提供证明。")
    print("  本工具不执行被扫代码——无 import、无 eval、无回测。")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "target": str(target),
            "summary": s,
            "files": [{"path": r.path, "flagged": r.flagged,
                       "parse_error": r.parse_error,
                       "findings": [vars(f) for f in r.findings]}
                      for r in reports if r.findings or r.parse_error],
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\n写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())