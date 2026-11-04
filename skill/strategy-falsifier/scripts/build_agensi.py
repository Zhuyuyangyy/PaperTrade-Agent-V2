"""Build the Agensi single-file engine.

WHY THIS EXISTS
Agensi runs each script exactly as shipped: the buyer's agent writes the
file unchanged to a TEMPORARY file, runs it there, and removes it. A script
therefore cannot read sibling files in the package, and bundled
dependencies (node_modules, site-packages, virtualenvs) are stripped on
upload. A multi-module engine/ directory is dead on arrival there.

This builder amalgamates the whole engine -- research/verify/*.py plus the
two audit modules it needs -- into ONE self-contained, stdlib-only Python
file that runs from anywhere with no package around it.

AMALGAMATION RULES (all mechanical; equivalence is enforced by
tests/test_agensi_single_file.py, which runs the single file and the
in-repo engine on identical inputs and diffs the JSON):
  - dependency order: pathsim, binance(DataError only), metrics, signals,
    spec, control, excursions, grid, data, report, compare, html_report,
    verify(__init__), __main__
  - relative imports are dropped. `from . import metrics` call sites become
    direct calls (metrics.X -> X) -- verified by a build-time assertion that
    no module-local name shadows the dropped module names
  - stdlib imports are hoisted once to the top, de-duplicated
  - genuine duplicate definitions are RESOLVED BY RENAMING with a module
    prefix (compare._pct -> _cmp_pct: its None case prints "n/a" while
    report._pct prints an em dash -- different behaviour, never merged),
    or dropped when a byte-identical duplicate (excursions._crossed is
    pathsim._crossed without the docstring)
  - binance.py is NOT amalgamated: only its DataError class travels (that
    is all data.py imports from it). The amalgamated engine is offline by
    construction -- no network code ships
  - the demo path is rewired to a deterministic synthetic candle series
    generated in-file (there is no sibling CSV to read in the Agensi
    runtime), and DEMO_SPEC's source line says so
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_VERIFY = ROOT / "research" / "verify"
SRC_AUDIT = ROOT / "research" / "audit"
OUT_DIR = ROOT / "skill" / "strategy-falsifier" / "agensi"
OUT_FILE = OUT_DIR / "scripts" / "verify.py"

# Dependency order. Later modules may use earlier modules' names.
ORDER = [
    ("pathsim", SRC_AUDIT / "pathsim.py"),
    ("metrics", SRC_VERIFY / "metrics.py"),
    ("signals", SRC_VERIFY / "signals.py"),
    ("spec", SRC_VERIFY / "spec.py"),
    ("control", SRC_VERIFY / "control.py"),
    ("excursions", SRC_VERIFY / "excursions.py"),
    ("grid", SRC_VERIFY / "grid.py"),
    ("data", SRC_VERIFY / "data.py"),
    ("report", SRC_VERIFY / "report.py"),
    ("compare", SRC_VERIFY / "compare.py"),
    ("html_report", SRC_VERIFY / "html_report.py"),
    ("diagnosis", SRC_VERIFY / "diagnosis.py"),
    ("verify", SRC_VERIFY / "__init__.py"),
    ("__main__", SRC_VERIFY / "__main__.py"),
]

# Renames: (module, original_name, new_name). Applied to the module's whole
# source text. Each entry must be justified in a comment.
RENAMES = [
    ("compare", "_pct", "_cmp_pct",
     "compare._pct maps None to 'n/a'; report._pct maps None to an em dash. "
     "Different behaviour -> rename, never merge."),
    ("diagnosis", "_pct", "_dx_pct",
     "diagnosis._pct maps None to 'n/a' (same as compare's); report._pct maps "
     "None to an em dash. Three-way collision -> each keeps its own name."),
]

# Definitions dropped because a byte-identical (modulo docstring) earlier
# definition exists: (module, name).
DROP_DEFS = [
    ("excursions", "_crossed",
     "identical logic to pathsim._crossed; the earlier definition wins"),
]

_IMPORT_LINE = re.compile(r"^(?:from|import)\s")


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


def _stdlib_imports(src: str) -> list[str]:
    """Top-level stdlib import statements, with __future__ stripped."""
    out = []
    for line in src.splitlines():
        if _IMPORT_LINE.match(line) and "from __future__" not in line:
            # keep only absolute (stdlib) imports: relative ones start with . 
            if not re.match(r"^from\s+\.", line):
                out.append(line.strip())
    return out


def _drop_internal_imports(src: str, module: str) -> str:
    """Remove intra-package imports (relative imports and the binance one).

    Function-local imports carry indentation, and parenthesised imports span
    multiple lines -- both are handled: a dropped import swallows its
    continuation lines until brackets balance.
    """
    kept = []
    i = 0
    lines = src.splitlines()
    while i < len(lines):
        s = lines[i].strip()
        if s == "from __future__ import annotations":
            # hoisted once to the amalgamation header; a second one anywhere
            # else is a hard SyntaxError
            i += 1
            continue
        if s.startswith("from .") or s.startswith("from ..") \
                or s == "from research.audit.binance import DataError":
            # swallow continuation lines until brackets balance
            depth = s.count("(") - s.count(")")
            i += 1
            while depth > 0 and i < len(lines):
                depth += lines[i].count("(") - lines[i].count(")")
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    return "\n".join(kept)


def _module_aliases(tree: ast.Module) -> set[str]:
    """Names bound by `from . import X [as Y]` (module objects), at any depth.

    `from .spec import Spec` binds a SYMBOL (Spec), not a module -- those are
    excluded: they keep working because their definitions travel in the same
    file. Only true module aliases need their `<alias>.<attr>` qualifier
    stripped.
    """
    return {
        a.asname or a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module is None
        for a in node.names
    }


def _strip_module_qualifiers(src: str, aliases: set[str]) -> str:
    """metrics.X(...) -> X(...) etc. after the module imports are dropped."""
    for name in sorted(aliases, key=len, reverse=True):
        src = re.sub(rf"(?<![\w.]){re.escape(name)}\.(\w+)", r"\1", src)
    return src


def _apply_renames(src: str, module: str) -> str:
    for mod, old, new, _why in RENAMES:
        if mod != module:
            continue
        src = re.sub(rf"(?<![\w.]){re.escape(old)}\(", f"{new}(", src)
        src = re.sub(rf"(?<![\w.]){re.escape(old)}\b(?!\s*\()", new, src)
    return src


def _drop_defs(src: str, module: str) -> str:
    for mod, name, _why in DROP_DEFS:
        if mod != module:
            continue
        # remove a top-level `def name(...):` block (until dedent)
        lines = src.splitlines()
        out, i, removed = [], 0, False
        while i < len(lines):
            if re.match(rf"^def {re.escape(name)}\(", lines[i]):
                i += 1
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                    i += 1
                removed = True
                continue
            out.append(lines[i])
            i += 1
        if not removed:
            raise SystemExit(f"build: DROP_DEFS could not find {module}.{name}")
        src = "\n".join(out)
    return src


SYNTHETIC_DEMO = '''

# ---------------------------------------------------------------------------
# Synthetic demo series (Agensi single-file build)
#
# The Agensi runtime runs this script from a temporary file with no sibling
# files readable, so the demo candles are GENERATED, not shipped. Same seed,
# same bars, every run. Real audits pass --candles with a real CSV; the
# report's repro section always names the actual data source.
# ---------------------------------------------------------------------------
def _synthetic_demo_candles(n: int = 2000, seed: int = 42) -> list[dict]:
    """Deterministic synthetic 1h OHLCV: mild upward drift, fat tails.

    Parameters chosen so the built-in demo rule (RSI(14)<30) fires ~90 times
    over the window -- comfortably past the demo spec's min_fills=30 bar, so
    the demo shows a real verdict instead of an INCONCLUSIVE shrug.
    """
    import random as _random

    rng = _random.Random(seed)
    out, px = [], 100.0
    t0 = 1_700_000_000_000
    for i in range(n):
        px *= 1.0 + rng.gauss(0.0001, 0.012)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.003)))
        low = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.003)))
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": low, "c": c, "v": 1000.0})
    return out

'''


def _rewire_main(src: str) -> str:
    """Point the demo's candle/spec fallbacks at the synthetic series."""
    # `from . import verify as run_verify` (function-local) -> direct call
    src = re.sub(r"(?<![\w.])run_verify\(", "verify(", src)
    # demo candles: use the in-file generator instead of a sibling CSV.
    # NOTE: the qualifier strip has already rewritten data.load_candles_csv
    # to load_candles_csv by the time _rewire_main runs, so the replacement
    # targets the STRIPPED form.
    src = src.replace(
        "if args.demo and BUNDLED_CANDLES.is_file():",
        "if args.demo:",
    )
    src = src.replace(
        "return load_candles_csv(BUNDLED_CANDLES)",
        "return _synthetic_demo_candles()",
    )
    # the bundled-spec branch can never be taken (no sibling files): the
    # demo spec always comes from the in-file DEMO_SPEC dict
    src = src.replace(
        "if (market_path is not None and market_path.is_file()) or not BUNDLED_SPEC.is_file():",
        "if True:",
    )
    # repro lines must not claim a bundle that is not there. NOTE: the
    # `if args.demo and BUNDLED_CANDLES.is_file():` replacement above already
    # rewrote the elif line in _repro_info (it contains that substring), so
    # this pattern targets only the assignment line.
    src = src.replace(
        'candles_src = f"{BUNDLED_CANDLES}（内置演示数据）"',
        'candles_src = "_synthetic_demo_candles()（本文件内确定性合成数据，seed=42）"',
    )
    # the demo spec's provenance must say the data is synthetic
    src = src.replace(
        '"source": "内置演示（规则信号，无需外部信号文件）"',
        '"source": "内置演示（规则信号；Agensi 单文件版使用文件内确定性合成 K 线，'
        '真实审计请用 --candles 提供 CSV）"',
    )
    return src


def build() -> Path:
    stdlib_lines: list[str] = []
    seen_imports: set[str] = set()
    bodies: list[str] = []
    defined: dict[str, str] = {}  # name -> module that defines it

    for module, path in ORDER:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)  # syntax check before any surgery
        top = _top_level_names(tree)

        # Module aliases bound by `from . import X [as Y]` at any depth.
        aliases = _module_aliases(tree)
        # Safety: an alias must not ALSO be bound as a local name (parameter,
        # assignment, loop target) in the same module -- `spec = result["spec"]`
        # followed by spec.get(...) is a dict method call, and stripping the
        # qualifier there would break it. Locals that merely share a name with
        # an engine module (but not with an alias) are fine: they are never
        # stripped.
        stored_names = {
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        for alias in sorted(aliases):
            if alias in stored_names:
                raise SystemExit(
                    f"build: {module} binds {alias!r} both as a module alias "
                    f"and as a local name; rename the local (the amalgamation "
                    f"strips alias.<attr> and would break the local)")

        for line in _stdlib_imports(src):
            if line not in seen_imports:
                seen_imports.add(line)
                stdlib_lines.append(line)

        src = _drop_internal_imports(src, module)
        src = _strip_module_qualifiers(src, aliases)
        src = _apply_renames(src, module)
        src = _drop_defs(src, module)
        if module == "__main__":
            src = _rewire_main(src)

        # record definitions and refuse silent collisions
        for node in ast.parse(src).body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                if node.name in defined and defined[node.name] != module:
                    raise SystemExit(
                        f"build: duplicate definition of {node.name!r} in "
                        f"{module} (already defined in {defined[node.name]}); "
                        f"add a RENAME rule")
                defined[node.name] = module

        bodies.append(f"# ==== from {path.name} " + "=" * (60 - len(path.name)))
        bodies.append(src.strip("\n"))
        bodies.append("")

    header = [
        '"""strategy-falsifier: the whole falsification engine in one file.',
        "",
        " Self-contained, stdlib-only, no sibling files, no network. Runs from",
        " any directory -- including a temporary file, which is exactly where",
        " Agensi puts it. Built by skill/strategy-falsifier/scripts/",
        "build_agensi.py from the in-repo engine; do not edit by hand.",
        "",
        " Usage:",
        "   python verify.py --demo",
        "   python verify.py --spec spec.json --candles klines.csv --out report.md --json",
        "   python verify.py --compare a.json b.json --out compare.md",
        "   python verify.py --spec spec.json --out report.html",
        '"""',
        "from __future__ import annotations",
        "",
    ]

    footer = [
        "",
        'if __name__ == "__main__":',
        "    raise SystemExit(main())",
        "",
    ]

    out = "\n".join(header) + "\n".join(stdlib_lines) + "\n" \
        + SYNTHETIC_DEMO + "\n" + "\n".join(bodies) + "\n".join(footer)

    # the amalgamation must compile
    try:
        ast.parse(out)
    except SyntaxError as exc:
        dbg = ROOT / "amalgam_debug.py"
        dbg.write_text(out, encoding="utf-8")
        raise SystemExit(
            f"build: amalgamation has a syntax error at line {exc.lineno}: "
            f"{exc.msg} -- dumped to {dbg} for inspection"
        ) from None

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(out, encoding="utf-8")
    return OUT_FILE


if __name__ == "__main__":
    p = build()
    n = len(p.read_text(encoding="utf-8").splitlines())
    print(f"built {p} ({n} lines, {p.stat().st_size} bytes)")
    if "--check" in sys.argv:
        sys.exit(0)
