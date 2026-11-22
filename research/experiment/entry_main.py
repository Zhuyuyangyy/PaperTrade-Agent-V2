"""实验 E 命令行（与实验 M 的 paired 子命令完全隔离）。

    python -m research.experiment.entry_main run --root data/v3_entryE \
        --run-id e20261122_01 --days 60 --max-bars 2000 --ai both

    python -m research.experiment.entry_main report --run data/v3_entryE/e20261122_01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from decimal import Decimal as D
from pathlib import Path

from .data import load_bars
from .entry_exp import ExpConfig, run_experiment


def _providers(ai: str, cfg: ExpConfig):
    from research.decision.providers.deepseek import DeepSeekProvider
    from research.decision.providers.entry_multitf import DeepSeekMultiTFEntry
    from research.intel.news_llm import LLMConfig
    from research.execution.binance_demo import load_demo_env

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    llm = LLMConfig(api_key=key)
    out = {}
    if ai in ("both", "legacy"):
        out["A"] = DeepSeekProvider(llm)
    if ai in ("both", "multitf"):
        out["B"] = DeepSeekMultiTFEntry(llm)
    return out, bool(key)


def _run(args) -> int:
    run_dir = Path(args.root) / args.run_id
    if run_dir.exists():
        raise SystemExit(f"run_id 已存在，拒绝覆盖: {run_dir}")
    run_dir.mkdir(parents=True)
    if args.env_from:
        from research.execution.binance_demo import load_demo_env
        shutil.copy(Path(args.env_from), run_dir / ".env")
        load_demo_env(str(run_dir))
    cache = run_dir / "bars_5m_cache.json"
    if args.cache_from:
        shutil.copy(Path(args.cache_from), cache)
    barset = load_bars(days=args.days, cache=cache)
    bars = barset.bars
    if len(bars) < args.max_bars:
        print(f"警告: 请求 {args.max_bars} 根，实际只有 {len(bars)} 根，按实际跑")

    cfg = ExpConfig(notional=D(str(args.notional)))
    providers, has_key = _providers(args.ai, cfg)
    # 规则组 C 无需 API；用闭包注入（entry_exp 里 C 走函数分支）
    from research.experiment.entry_exp import _rules_multitf
    providers["C"] = _rules_multitf

    # 严格时间切分：前 70% 研究集，参数在研究集确定；样本外只评估
    usable = bars[:args.max_bars]
    research_end_ms = int(usable[int(len(usable) * 0.7)].close_ms)
    started = time.time()
    filt = None
    if args.filter and has_key:
        from research.decision.providers.entry_multitf import DeepSeekEntryFilter
        from research.intel.news_llm import LLMConfig
        filt = DeepSeekEntryFilter(LLMConfig(api_key=os.environ.get("DEEPSEEK_API_KEY", "")))
    result = run_experiment(usable, cfg=cfg, providers=providers,
                            research_end_ms=research_end_ms, full_bars=bars,
                            filter_provider=filt, decision_every=args.decision_every)
    result["meta"] = {
        "run_id": args.run_id, "ai": args.ai, "api_key_present": has_key,
        "bar_quality": barset.quality.to_dict(),
        "bars_total": len(bars), "bars_used": min(len(bars), args.max_bars),
        "elapsed_s": round(time.time() - started, 1),
        "bars_sha256": hashlib.sha256(
            json.dumps([c.open_ms for c in usable]).encode()).hexdigest()[:16],
        "note": "收盘信号按下一根开盘价成交（无前视）；三组同费用/风险/撮合/退出。",
    }
    (run_dir / "exp_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (run_dir / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for d in result["decisions"]:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    with (run_dir / "trades.jsonl").open("w", encoding="utf-8") as f:
        for t in result["trades"]:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(json.dumps({"run": str(run_dir), "decisions": len(result["decisions"]),
                      "trades": len(result["trades"]),
                      "api_key": has_key, "elapsed_s": result["meta"]["elapsed_s"]},
                     ensure_ascii=False))
    return 0


def _report(args) -> int:
    from .entry_report import report
    run = Path(args.run)
    out = report(run)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written -> {args.out}")
    else:
        print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="research.experiment.entry_main")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="运行实验 E")
    p.add_argument("--root", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--max-bars", type=int, default=1000)
    p.add_argument("--notional", type=float, default=100)
    p.add_argument("--ai", choices=("both", "legacy", "multitf", "none"), default="both")
    p.add_argument("--env-from", default=None)
    p.add_argument("--cache-from", default=None)
    p.add_argument("--filter", action="store_true", help="启用 D 组：规则信号 + AI 过滤")
    p.add_argument("--decision-every", type=int, default=6,
                   help="每 N 根 5m 评估一次入场（默认 6=30分钟）；三组同频")
    q = sub.add_parser("report", help="只读汇总实验 E")
    q.add_argument("--run", required=True)
    q.add_argument("--out", default=None)
    a = ap.parse_args()
    return _run(a) if a.cmd == "run" else _report(a)


if __name__ == "__main__":
    raise SystemExit(main())
