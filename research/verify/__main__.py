"""CLI: run the falsification battery from the command line.

    python -m research.verify --list-market
    python -m research.verify --spec spec.json [--candles klines.csv] [--out report.md]
    python -m research.verify --demo [--out report.md]

Data resolution order for candles: --candles CSV first, then the local market
cache (runtime/audit/market.sqlite) filtered by the spec's symbol/interval/
venue. Signals come from the spec: kind=csv reads --signals (or signals.path),
kind=rule computes from the candles, kind=random is the control by itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import data, report
from .spec import Spec, SpecError

# Dual-layout path resolution. The SAME file runs in two places:
#   in-repo:      research/verify/__main__.py      (repo root has runtime/)
#   vendored:     skill/strategy-falsifier/engine/__main__.py (no repo around)
# In both layouts the demo bundle sits next to this file's package root, so a
# standalone ZIP runs with zero repository context.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
ROOT = _REPO_ROOT if (_REPO_ROOT / "runtime" / "audit").is_dir() else _HERE.parent
DEFAULT_MARKET = (ROOT / "runtime" / "audit" / "market.sqlite"
                  if (ROOT / "runtime" / "audit").is_dir() else None)
# Self-contained demo bundle: a fresh clone has no market cache (sqlite is
# gitignored), so the demo must work from committed CSVs alone.
_SAMPLES = next((p for p in (_HERE / "samples", ROOT / "samples") if (p / "demo_candles.csv").is_file()),
                _HERE / "samples")
BUNDLED_CANDLES = _SAMPLES / "demo_candles.csv"
BUNDLED_SPEC = _SAMPLES / "demo_spec_csv.json"
# The funnel tail printed after every run: where to see the 30-second demo
# and the case matrix, and what the first 10 orders cost (nothing).
LANDING_PAGE = "https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/"

# The spec file currently being processed, so relative paths inside it resolve
# against the spec's own directory. Set by main() before any loading happens.
_spec_path: list = [None]

DEMO_SPEC = {
    "name": "rsi-dip-btc-1h-demo",
    "hypothesis": "BTC 1 小时 RSI(14) 低于 30 后做多，捕捉均值回复",
    "source": "内置演示（规则信号，无需外部信号文件）",
    "symbol": "BTCUSDT",
    "interval": "1h",
    "venue": "futures",
    "fee_rate": 0.0004,
    "fill_model": "taker",
    "funding": True,
    "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
    "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30, "side": "long"},
    "control": {"enabled": True, "n_random": 200, "seed": 42},
    "walk_forward": {"enabled": True, "folds": 3},
    "param_grid": {"axes": {"take_profit": [0.02, 0.03, 0.04],
                            "stop_loss": [-0.06, -0.08, -0.10]}},
    "fee_multiples": [1, 2, 5],
    "falsify_if": {
        "control_lift_ci_includes_zero": True,
        "net_negative_at_fee_multiple": 2,
        "worst_case_net_negative": True,
        "grid_positive_ratio_min": 0.5,
        "min_fills": 30,
    },
}


def _load_candles(args, spec: Spec) -> list[dict]:
    if args.candles:
        return data.load_candles_csv(args.candles)
    # The spec's own candle file (relative to the spec's directory) travels
    # with the spec folder -- this is how --compare mixes strategies over
    # different data sources, each disclosed in the comparison.
    if spec.candles_path:
        p = _resolve_against_spec(_spec_path[0], spec.candles_path)
        if p.is_file():
            return data.load_candles_csv(p)
        raise data.CandleError(f"spec 声明的 candles_path 不存在: {p}")
    market = Path(args.market) if args.market else DEFAULT_MARKET
    if market is not None and market.is_file():
        return data.load_candles_market(market, spec.symbol, spec.interval, spec.venue)
    # No cache (fresh clone or standalone skill): fall back to the committed
    # demo bundle so the demo always runs. Numbers differ slightly from the
    # cached run (no funding series ships in the bundle) and the report
    # discloses that.
    if args.demo and BUNDLED_CANDLES.is_file():
        print(f"[提示] 无行情缓存，使用内置演示数据 {BUNDLED_CANDLES.name}", file=sys.stderr)
        return data.load_candles_csv(BUNDLED_CANDLES)
    where = market if market is not None else "（独立运行，无仓库行情缓存）"
    raise data.CandleError(
        f"找不到行情缓存 {where}。请用 --candles 提供 CSV（列 t,o,h,l,c），"
        "或用 --list-market 查看可用缓存。"
    )


def _build_entries(args, spec: Spec, candles: list[dict]) -> dict[int, str]:
    from . import signals as sig

    # A signal file passed to a rule/random spec used to be SILENTLY ignored:
    # the customer's own signals never got tested while the report quietly
    # audited the built-in rule instead. That is buying the wrong answer, so
    # it is now a hard error.
    if args.signals and spec.signals.kind in ("rule", "random"):
        raise SpecError(
            f"spec 声明 signals.kind='{spec.signals.kind}'，但又传了 --signals {args.signals}。"
            f"引擎不会猜测你意图：要么把 spec 改成 kind='csv' 测你自己的信号，"
            f"要么去掉 --signals 测内置规则。"
        )

    if spec.signals.kind == "rule":
        entries = sig.rule_signals(
            candles, rule=spec.signals.rule, period=spec.signals.period,
            threshold=spec.signals.threshold, side=spec.signals.side,
        )
        if not entries:
            raise SpecError(f"规则 {spec.signals.rule} 在窗口内没有产生任何信号（阈值 {spec.signals.threshold}）")
        return entries
    if spec.signals.kind == "random":
        return sig.random_signals(candles, spec.signals.n_random, spec.signals.seed, side=spec.signals.side)
    path = args.signals or spec.signals.path
    if not path:
        raise SpecError("signals.kind='csv' 需要 --signals 或 spec.signals.path")
    # A relative path inside the spec resolves against the SPEC's directory
    # first (a spec folder travels together), not the engine's cwd -- the
    # exit-signal loader already did this; entries silently did not.
    path = str(_resolve_against_spec(_spec_path[0], path))
    entries, stats = data.load_signals_csv(path, candles)
    if stats["total"] and not entries:
        raise data.CandleError(
            f"信号文件 {path} 有 {stats['total']} 行但无一映射到 K 线窗口内"
            f"（窗口 {candles[0]['t']} → {candles[-1]['t']}）"
        )
    if stats["bad_rows"] or stats["skipped_after_end"] or stats["skipped_before_start"]:
        print(
            f"[信号提示] 共 {stats['total']} 行：映射 {stats['mapped']}，"
            f"窗口外跳过 {stats['skipped_after_end'] + stats['skipped_before_start']}，"
            f"坏行 {stats['bad_rows']}",
            file=sys.stderr,
        )
    return entries


def _resolve_against_spec(spec_path: str | None, rel: str) -> Path:
    """A relative path in a spec resolves against the spec's own directory
    first (so a spec folder travels together), then against the CWD."""
    p = Path(rel)
    if p.is_absolute() or p.is_file():
        return p
    if spec_path:
        cand = Path(spec_path).resolve().parent / rel
        if cand.is_file():
            return cand
    return p


def _load_exit_signals(spec: Spec, candles: list[dict]) -> dict[int, str]:
    path = _resolve_against_spec(_spec_path[0], spec.exit.exit_signals_path)
    sigs = data.load_exit_signals_csv(path, candles)
    if not sigs:
        raise data.CandleError(f"退出信号文件 {path} 没有映射到 K 线窗口内的信号")
    return sigs


def _q(s: str) -> str:
    """Quote a path only when it needs it, so the repro command stays
    copy-pasteable on Windows shells."""
    return f'"{s}"' if " " in s else s


def _repro_info(args, spec: Spec, raw_argv: list[str]) -> dict:
    """The exact command and input paths behind this run, so the report ends
    with its own recipe. Built from what was actually passed -- never guessed."""
    # Layout-aware: the same file runs in-repo (python -m research.verify) and
    # vendored inside the standalone skill (python -m engine).
    cmd = "python -m research.verify" if (ROOT / "research").is_dir() else "python -m engine"
    if raw_argv:
        cmd += " " + " ".join(_q(a) for a in raw_argv)
    market_path = Path(args.market) if args.market else DEFAULT_MARKET
    has_cache = market_path is not None and market_path.is_file()
    if args.demo:
        if has_cache or not BUNDLED_SPEC.is_file():
            spec_src = "内置演示 spec（--demo；DEMO_SPEC 定义于 research/verify/__main__.py）"
        else:
            spec_src = f"{BUNDLED_SPEC}（内置离线演示 spec）"
    else:
        spec_src = str(args.spec)
    if args.candles:
        candles_src = str(args.candles)
    elif spec.candles_path:
        candles_src = f"{spec.candles_path}（spec 自带，相对 spec 目录解析）"
    elif has_cache:
        candles_src = f"{market_path}（本地行情缓存 sqlite）"
    elif args.demo and BUNDLED_CANDLES.is_file():
        candles_src = f"{BUNDLED_CANDLES}（内置演示数据）"
    else:
        candles_src = f"{market_path}（未找到）"
    k = spec.signals.kind
    if k == "rule":
        sig_src = (f"规则信号（{spec.signals.rule}，period={spec.signals.period:g}，"
                   f"threshold={spec.signals.threshold:g}，由 K 线实时计算，无需外部文件）")
    elif k == "random":
        sig_src = f"随机信号（n={spec.signals.n_random}，seed={spec.signals.seed}）"
    else:
        path = args.signals or spec.signals.path
        sig_src = str(path) if path else "未提供（signals.kind=csv 但无路径）"
    if spec.funding_path:
        funding_src = f"{spec.funding_path}（spec 自带资金费序列）"
    elif spec.funding and not (args.candles or spec.candles_path) and market_path is not None:
        funding_src = f"{market_path}（本地行情缓存 funding 表）"
    elif spec.funding:
        funding_src = "无序列，按 0 计"
    else:
        funding_src = "不适用（spec.funding=false）"
    return {"command": cmd, "spec": spec_src, "candles": candles_src, "signals": sig_src,
            "funding": funding_src}


def _print_footer_promo() -> None:
    """The funnel tail: every run -- report to stdout or to a file -- ends
    with the landing page and the free-first-10-orders offer."""
    print()
    print("-" * 60)
    print(f"落地页（30 秒演示 · 真实案例矩阵）：{LANDING_PAGE}")
    print("服务：带策略来，我出证伪报告 —— 首 10 单免费（换匿名公开案例授权），付费档 $99 / $129 诊断增强 / $299。")
    print("-" * 60)


def _run_one(args, spec_path: str | None, raw_argv: list[str]) -> dict:
    """Run the full battery for ONE spec and return the result dict.

    Shared by the single-strategy path and --compare (which calls it once per
    spec). spec_path=None means the built-in demo.
    """
    market_path = Path(args.market) if args.market else DEFAULT_MARKET
    if args.demo and spec_path is None:
        # With a cache the demo runs the full 3-year series; without one it
        # falls back to the committed bundle (no funding series ships with
        # it, so the bundled spec declares funding: false and says so).
        if (market_path is not None and market_path.is_file()) or not BUNDLED_SPEC.is_file():
            spec = Spec.from_dict(DEMO_SPEC)
        else:
            spec = Spec.load(BUNDLED_SPEC)
    else:
        spec = Spec.load(spec_path)
        _spec_path[0] = spec_path
    candles = _load_candles(args, spec)
    entries = _build_entries(args, spec, candles)
    exit_signals = None
    if spec.exit.exit_signals_path:
        exit_signals = _load_exit_signals(spec, candles)
    funding = None
    # Funding resolution: the spec's own funding file first (it travels with
    # the spec), then the cache -- but only when the candles ALSO come from
    # that cache, so rates never get applied across mismatched windows.
    if spec.funding_path:
        fp = _resolve_against_spec(_spec_path[0], spec.funding_path)
        if fp.is_file():
            funding = data.load_funding_csv(fp)
        else:
            raise data.CandleError(f"spec 声明的 funding_path 不存在: {fp}")
    elif spec.funding and not (args.candles or spec.candles_path) and market_path is not None:
        funding = data.load_funding(market_path, spec.symbol)
        if not funding:
            print("[提示] 缓存中没有该 symbol 的资金费数据，按 0 处理", file=sys.stderr)
    from . import verify as run_verify

    result = run_verify(spec, candles, entries, funding=funding, exit_signals=exit_signals)
    result["repro"] = _repro_info(args, spec, raw_argv)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research.verify", description="策略证伪引擎：path-aware 回放 + 成本敏感性 + 障碍可达性 + 随机对照组")
    ap.add_argument("--spec", help="策略 spec JSON 路径")
    ap.add_argument("--demo", action="store_true", help="运行内置演示（BTC 1h RSI 规则，数据取自本地行情缓存）")
    ap.add_argument("--compare", nargs="+", metavar="SPEC",
                    help="对比模式：2 个以上 spec 一次跑，输出多策略对比矩阵（排名规则事前声明）")
    ap.add_argument("--candles", help="K 线 CSV（列 t,o,h,l,c），优先于行情缓存；对比模式下所有策略共用")
    ap.add_argument("--signals", help="信号 CSV（列 t[,side]），kind=csv 时使用；对比模式下对所有 spec 生效")
    ap.add_argument("--market", help=f"行情缓存 sqlite（默认 {DEFAULT_MARKET}）")
    ap.add_argument("--out", help="报告输出路径（markdown；.html 输出 HTML 版）；缺省打印到 stdout")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读 JSON（与 --out 同名 .json）")
    ap.add_argument("--list-market", action="store_true", help="列出本地行情缓存内容后退出")
    args = ap.parse_args(argv)
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    if args.compare:
        if args.demo:
            ap.error("--compare 与 --demo 不能同用（对比需要显式 spec）")
        if args.spec:
            ap.error("--compare 与 --spec 不能同用（把全部 spec 都给 --compare）")
        if len(args.compare) < 2:
            ap.error("--compare 至少需要 2 个 spec；单策略请用 --spec")
    elif not args.spec and not args.demo:
        ap.error("必须提供 --spec 或 --demo（或用 --list-market 查看缓存）")

    market = Path(args.market) if args.market else DEFAULT_MARKET
    if args.list_market:
        if market is None:
            print("独立运行模式：无仓库行情缓存可用（--list-market 需要缓存）", file=sys.stderr)
            return 2
        try:
            rows = data.describe_market_cache(market)
        except Exception as exc:  # noqa: BLE001 - CLI boundary, message is the product
            print(f"无法读取行情缓存 {market}: {exc}", file=sys.stderr)
            return 2
        print(f"{market} · {len(rows)} 组序列")
        print(f"{'symbol':<12}{'interval':<10}{'venue':<10}{'bars':>8}  from → to")
        import datetime as _dt

        for sym, itv, ven, n, t0, t1 in rows:
            f = lambda ms: _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
            print(f"{sym:<12}{itv:<10}{ven:<10}{n:>8}  {f(t0)} → {f(t1)}")
        return 0

    try:
        if args.compare:
            from . import compare as compare_mod

            results = []
            for sp in args.compare:
                _spec_path[0] = None  # each spec resolves its own relative paths
                results.append(_run_one(args, sp, raw_argv))
            md = compare_mod.render_comparison(results)
            payload = results
        else:
            result = _run_one(args, args.spec, raw_argv)
            md = report.render(result)
            payload = result
    except (SpecError, data.CandleError, ValueError) as exc:
        print(f"[输入错误] {exc}", file=sys.stderr)
        return 2

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".html":
            from . import html_report

            if args.compare:
                html_report.render_comparison_html_to(results, out)
            else:
                html_report.render_to(result, out)
            print(f"HTML 报告已写入 {out}")
        else:
            out.write_text(md, encoding="utf-8")
            print(f"报告已写入 {out}")
        if args.json:
            jpath = out.with_suffix(".json")
            jpath.write_text(report.render_json(payload), encoding="utf-8")
            print(f"JSON 已写入 {jpath}")
    else:
        print(md)
        if args.json:
            print(report.render_json(payload))
    _print_footer_promo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
