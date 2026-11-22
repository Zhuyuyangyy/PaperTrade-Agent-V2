"""``python -m research.runtime``：demo / run / live / replay / report / shadow / dashboard。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from research.decision import (
    DeepSeekProvider,
    JevProvider,
    LinearProvider,
    LogisticModel,
    RulesProvider,
)
from research.intel.news_llm import LLMConfig

from research.intel.pipeline import IntelPipelineConfig, collect as collect_intel
from research.market.contracts import TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.demo import REGIMES, demo_as_of_ms, synthetic_1m
from research.market.klines import resample
from research.paper.broker import PaperBroker
from research.paper.deepseek_desk import DeepSeekDesk
from research.risk.engine import RiskLimits
from research.strategy.registry import DEFAULT_SPECS

from .dashboard import build_state, render_html
from .heartbeat import read as read_heartbeat
from .live import BinanceKlineSource
from .v2_engine import BOOKS, V2Engine
from research.terminal.feed import BinanceFeed
from .loop import LoopConfig, RunLoop
from .metrics import summarize_books
from .sources import CandlesSource
from .shadow import load_shadow_signals, report as shadow_report
from .round import run_round

D = Decimal
WARMUP_HOURS = 210


def _build_providers(args):
    """按环境变量构造 provider：rules 常开，deepseek/jev/linear 有配置才启用。"""
    if getattr(args, "no_providers", False):
        return []
    provs = [RulesProvider()]
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        provs.append(DeepSeekProvider(LLMConfig(api_key=key)))
    endpoint = os.environ.get("JEV_ENDPOINT", "")
    jev_key = os.environ.get("JEV_API_KEY", "")
    if endpoint and jev_key:
        provs.append(JevProvider(endpoint, jev_key, os.environ.get("JEV_MODEL", "jev")))
    model_path = getattr(args, "model", None)
    if model_path and Path(model_path).is_file():
        provs.append(LinearProvider(LogisticModel.load(model_path)))
    return provs


def _build_deepseek_desk(root: Path, args):
    """DeepSeek 独立盘：自己的 1000 USDT 与账本；无 key 则不启用。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key or getattr(args, "no_deepseek_desk", False):
        return None
    broker = PaperBroker(root / "deepseek_shadow.sqlite", starting_cash=D("1000"),
                         funding_bps_per_8h=D(str(getattr(args, "funding_bps", 1.0))),
                         shadow_mode="allow")
    return DeepSeekDesk(broker, DeepSeekProvider(LLMConfig(api_key=key)), RiskLimits(),
                        root / "data" / "deepseek_decisions.jsonl")


def _views(base) -> dict:
    return {name: resample(base, ms)[0] for name, ms in
            (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))}


def _specs(verified: list[str]):
    return tuple(
        replace(s, verification="VERIFIED", verified_by="DEMO-ONLY")
        if s.strategy_id in verified else s
        for s in DEFAULT_SPECS
    )


def _books(root: Path, funding_bps: Decimal) -> tuple[PaperBroker, PaperBroker]:
    return (
        PaperBroker(root / "verified.sqlite", starting_cash=D("1000"),
                    funding_bps_per_8h=funding_bps),
        PaperBroker(root / "shadow.sqlite", starting_cash=D("1000"),
                    funding_bps_per_8h=funding_bps, shadow_mode="shadow_only"),
    )


def _run_demo(args) -> int:
    specs = _specs(args.verified)
    if args.verified:
        print(f"WARNING: --verified={args.verified} 仅用于演示（verified_by=DEMO-ONLY）；"
              f"正式运行不得通过命令行提升策略权限。", file=sys.stderr)
    as_of = demo_as_of_ms()
    with tempfile.TemporaryDirectory() as td:
        for regime in REGIMES:
            broker = PaperBroker(Path(td) / f"ledger_{regime}.sqlite", starting_cash=D("1000"))
            log_path = Path(td) / f"decisions_{regime}.jsonl"
            res = run_round("BTC/USDT", _views(synthetic_1m(regime)), as_of, broker,
                            RiskLimits(), log_path=log_path, specs=specs)
            sel, risk = res.selection, res.risk
            intent = sel.intent
            print(f"[{regime}] decision={sel.decision}"
                  + (f" strategy={intent.strategy_id} shadow={intent.shadow}" if intent else ""))
            print(f"  state: {res.state.trend}/{res.state.volatility}/{res.state.momentum}/"
                  f"{res.state.risk} conf={res.state.confidence}")
            if risk:
                print(f"  risk: {risk.decision} {risk.codes} — {risk.reason}")
            print(f"  fill: {res.fill.to_dict() if res.fill else 'none'}")
            print(f"  hold: {sel.hold_reason or '-'}")
            broker.close()
    return 0


def _run_loop(args) -> int:
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="openalpha_run_"))
    root.mkdir(parents=True, exist_ok=True)
    base = synthetic_1m(args.regime)
    source = CandlesSource(base)
    start = base[0].open_ms + WARMUP_HOURS * TF_1H_MS
    until = start + args.hours * TF_1H_MS
    verified, shadow = _books(root, D("0"))
    loop = RunLoop(root, verified, RiskLimits(), source, specs=_specs(args.verified),
                   shadow_broker=shadow,
                   config=LoopConfig(interval_ms=args.interval_ms, max_rounds=args.max_rounds))
    summary = loop.run(start, until)
    for res in loop.results:
        print(f"[{res.state.as_of_ms}] {res.selection.decision}"
              f" risk={res.risk.decision if res.risk else 'NONE'}"
              f" fill={'yes' if res.fill else 'no'}"
              f" shadow_fill={'yes' if res.log.get('shadow_fill') else 'no'}")
    print(f"\nsummary: {json.dumps(summary, ensure_ascii=False)}")
    print(json.dumps(summarize_books(root), ensure_ascii=False, indent=2))
    print(f"root: {root}")
    verified.close()
    shadow.close()
    return 0


def _run_live(args) -> int:
    """真实数据 → RunLoop：验证盘 + 实验盘双账本，心跳/熔断/幂等/日志一条路径。"""
    root = Path(args.root) if args.root else Path("data/openalpha_live")
    root.mkdir(parents=True, exist_ok=True)
    source = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                                timeout_s=args.timeout_s, retries=1)
    verified, shadow = _books(root, D(str(args.funding_bps)))
    intel_fn = None
    if not args.no_intel:
        intel_cfg = IntelPipelineConfig(with_books=args.intel_books)
        intel_fn = lambda as_of: collect_intel(as_of, intel_cfg)  # noqa: E731 - 每轮采集，失败由 RunLoop 兜底
    loop = RunLoop(
        root, verified, RiskLimits(), source, shadow_broker=shadow,
        log_path=root / "data" / "decisions.jsonl",
        intel_fn=intel_fn,
        providers=_build_providers(args),
        providers_log_path=root / "data" / "providers.jsonl",
        deepseek_desk=_build_deepseek_desk(root, args),
        config=LoopConfig(interval_ms=args.interval_ms,
                          decision_period_ms=args.interval_ms,
                          max_rounds=args.rounds),
    )
    sleep_s = args.sleep_s if args.sleep_s is not None else max(1.0, args.interval_ms / 1000)
    for i in range(args.rounds):
        as_of = int(time.time() * 1000)
        res = loop.step(as_of)
        if loop.last_status == "SKIPPED":
            print(f"[{as_of}] skipped: {loop.last_reason}（同一根 K 线已成功决策过）")
        elif loop.last_status == "FAILED":
            print(f"[{as_of}] FAILED: {loop.last_reason}（下一轮重试；连续失败会熔断）")
        elif res is not None:
            intent = res.selection.intent
            print(f"[{as_of}] state={res.state.trend}/{res.state.volatility}/"
                  f"{res.state.momentum}/{res.state.risk} conf={res.state.confidence}")
            print(f"  decision={res.selection.decision}"
                  + (f" {intent.strategy_id} shadow={intent.shadow}" if intent else ""))
            if res.risk:
                print(f"  risk={res.risk.decision} {res.risk.codes} — {res.risk.reason}")
            print(f"  fill={res.fill.to_dict() if res.fill else 'none'}")
            print(f"  shadow_fill={res.log.get('shadow_fill') or 'none'}")
            print(f"  equity={res.equity} shadow_equity={res.log.get('shadow_equity')}")
        print(f"  source.stats={source.stats()} circuit={loop.circuit.to_dict()}")
        if i + 1 < args.rounds:
            time.sleep(sleep_s)
    print("\nbook summary:")
    print(json.dumps(summarize_books(root), ensure_ascii=False, indent=2))
    hb = read_heartbeat(loop.heartbeat_path)
    print(f"heartbeat: {hb.to_dict() if hb else None}")
    print(f"root={root}")
    verified.close()
    shadow.close()
    return 0


def _run_replay(args) -> int:
    """真实历史 1h K 线 → 双账本跑 N 轮（默认 72），用于 72h 实验盘回放。"""
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="openalpha_replay_"))
    root.mkdir(parents=True, exist_ok=True)
    live = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                              timeout_s=args.timeout_s, retries=1)
    try:
        views = live.candles(int(time.time() * 1000))
    except Exception as exc:  # noqa: BLE001
        print(f"拉取历史 K 线失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    base = views.get("1h") or []
    if len(base) < WARMUP_HOURS + args.rounds + 2:
        print(f"1h K 线不足：{len(base)} 根，需要 >= {WARMUP_HOURS + args.rounds + 2}", file=sys.stderr)
        return 1
    source = CandlesSource(base)
    start = base[WARMUP_HOURS].close_ms + 31_000
    until = start + (args.rounds - 1) * TF_1H_MS
    verified, shadow = _books(root, D(str(args.funding_bps)))
    loop = RunLoop(root, verified, RiskLimits(), source, shadow_broker=shadow,
                   log_path=root / "data" / "decisions.jsonl",
                   providers=_build_providers(args),
                   providers_log_path=root / "data" / "providers.jsonl",
                   deepseek_desk=_build_deepseek_desk(root, args),
                   config=LoopConfig(interval_ms=TF_1H_MS,
                                     decision_period_ms=TF_1H_MS,
                                     max_rounds=args.rounds))
    summary = loop.run(start, until)
    print(f"replay: {summary}")
    print(json.dumps(summarize_books(root), ensure_ascii=False, indent=2))
    print(f"root={root}")
    verified.close()
    shadow.close()
    return 0


def _run_report(args) -> int:
    print(json.dumps(summarize_books(Path(args.root)), ensure_ascii=False, indent=2))
    return 0


def _run_shadow(args) -> int:
    root = Path(args.root)
    signals = load_shadow_signals(root / "data" / "decisions.jsonl")
    if not signals:
        print("没有影子信号（先跑 live）")
        return 0
    source = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                                timeout_s=args.timeout_s, retries=1)
    try:
        candles = source.candles(int(time.time() * 1000)).get("1h", [])
    except Exception as exc:  # noqa: BLE001 - 影子评估失败只需如实报告
        print(f"拉取 K 线失败，无法评估影子信号: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(shadow_report(signals, candles, tuple(args.horizons)),
                     ensure_ascii=False, indent=2))
    return 0


def _run_v2(args) -> int:
    feed = BinanceFeed(symbol="btcusdt")
    feed.start()
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    ds = DeepSeekProvider(LLMConfig(api_key=key)) if key else None
    second = None
    if not args.no_second:
        endpoint = os.environ.get("JEV_ENDPOINT", "")
        jk = os.environ.get("JEV_API_KEY", "")
        if endpoint and jk:
            second = JevProvider(endpoint, jk, os.environ.get("JEV_MODEL", "jev"))
    intel_cfg = IntelPipelineConfig(max_news=10, with_books=False)
    executor, demo_book = _build_executor(args)
    engine = V2Engine(Path(args.root), feed, RiskLimits(), deepseek=ds, second=second,
                      intel_fn=lambda ts: collect_intel(ts, intel_cfg),
                      executor=executor, demo_book=demo_book)
    engine.load_history()
    print(f"v2 engine: root={args.root} minutes={args.minutes} "
          f"deepseek={'on' if ds else 'off'} second={'on' if second else 'off'} "
          f"demo={'on:' + demo_book if demo_book else 'off(虚拟盘)'}")
    engine.run(args.minutes)
    print("done; errors:", engine.errors[-5:])
    if engine.last_inputs:
        print("last inputs:", json.dumps(engine.last_inputs, ensure_ascii=False)[:800])
    return 0


def _demo_credentials(root) -> "tuple":
    """先读环境变量，缺失项再从 .env 候选路径补；返回 (cfg, env_info)。"""
    from research.execution.binance_demo import DemoConfig, load_demo_env

    env_info = load_demo_env(root)
    return DemoConfig.from_env(), env_info


def _build_executor(args):
    """只有显式 --demo-exec 且凭据齐全时才构造适配器；否则纯虚拟盘。"""
    if not getattr(args, "demo_exec", False):
        return None, None
    from research.execution.adapter import DemoExecutionAdapter
    from research.execution.binance_demo import DemoClient

    cfg, env_info = _demo_credentials(getattr(args, "root", None))
    if not cfg.available:
        print("WARNING: --demo-exec 需要 BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET；"
              "当前缺失，退回纯虚拟盘，一个订单都不会发。\n"
              f"  已尝试的 .env：{env_info['checked']}", file=sys.stderr)
        return None, None
    if env_info["sources"]:
        print(f"credentials: 从 .env 补入 {env_info['filled']}（来源 {env_info['sources']}）",
              file=sys.stderr)
    adapter = DemoExecutionAdapter(DemoClient(cfg), symbol="BTCUSDT",
                                   root=Path(args.root))
    return adapter, args.demo_book


def _run_demo_exec(args) -> int:
    """Demo 联调：preflight（只读）→ 可选受控小额测试单 → 对账。

    默认只做 preflight。下单必须显式加 --place-test-order，且名义金额受 --test-notional 限制。
    """
    from research.execution.adapter import DemoExecutionAdapter
    from research.execution.binance_demo import DemoClient
    from research.strategy.contracts import TradeIntent

    cfg, env_info = _demo_credentials(args.root)
    client = DemoClient(cfg)
    if not cfg.available:
        print(json.dumps({"ok": False, "reason": "DEMO_UNAVAILABLE: 未设置 "
                           "BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET",
                          "env_files_checked": env_info["checked"],
                          "note": "把两个变量写进上面任一路径的 .env（或设成系统环境变量）后重跑；"
                                  "没有凭据时只允许离线验证，本地虚拟盘不用凭据也能继续跑。"},
                         ensure_ascii=False, indent=2))
        return 2
    if env_info["sources"]:
        print(f"credentials: 从 .env 补入 {env_info['filled']}（来源 {env_info['sources']}）",
              file=sys.stderr)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    adapter = DemoExecutionAdapter(client, symbol=args.symbol, root=root,
                                   place_tp=not args.no_tp, poll_s=0.5, poll_tries=8)

    pre = adapter.preflight()
    print("preflight:", json.dumps(pre, ensure_ascii=False, indent=2)[:2000])
    if not pre.get("ok"):
        print("preflight 未通过，停止（不发任何订单）。", file=sys.stderr)
        return 3
    rec = adapter.reconcile()
    print("reconcile:", json.dumps(rec, ensure_ascii=False, indent=2)[:2000])

    if not args.place_test_order:
        print("\n只读体检完成。要下单请加 --place-test-order --test-notional 120")
        return 0

    # 受控小额测试单：用当前标记价构造 1 笔立刻可平的市价单
    pos = client.positions(args.symbol)
    if pos:
        print(f"已有持仓 {pos}，拒绝叠加测试单（先人工处理，不盲目下单）。", file=sys.stderr)
        return 4
    mark = Decimal(str(client._request(
        "GET", "/fapi/v1/premiumIndex", {"symbol": args.symbol}).get("markPrice", "0")))
    if mark <= 0:
        print("拿不到标记价，停止。", file=sys.stderr)
        return 5
    notional = Decimal(str(args.test_notional))
    # 用交易所真实过滤器前置校验：低于 minNotional 就明确告知需要多少，绝不硬试
    f = adapter._ensure_filters()
    qty = f.qty(notional / mark)
    effective = qty * mark
    if qty < f.min_qty or effective < f.min_notional:
        need = (f.min_notional / mark / f.step_size).to_integral_value(rounding="ROUND_UP") \
            * f.step_size
        print(json.dumps({
            "ok": False, "reason": "NOTIONAL_TOO_SMALL",
            "requested_notional": str(notional), "mark": str(mark),
            "rounded_qty": str(qty), "effective_notional": str(effective),
            "min_qty": str(f.min_qty), "min_notional": str(f.min_notional),
            "step_size": str(f.step_size),
            "minimum_qty_to_pass": str(need),
            "minimum_notional_to_pass": str(need * mark),
            "hint": f"请加 --test-notional {int((need * mark).to_integral_value(rounding='ROUND_UP'))} "
                    f"或更大；本轮未发任何订单。",
        }, ensure_ascii=False, indent=2))
        return 7
    atr_proxy = mark * Decimal("0.004")          # 0.4% 止损，约等于 5m ATR 的量级
    intent = TradeIntent(
        f"demo-test-{int(time.time())}", int(time.time() * 1000), args.symbol,
        args.test_side, "demo-connectivity", "manual",
        mark, mark - atr_proxy if args.test_side == "LONG" else mark + atr_proxy,
        mark + atr_proxy if args.test_side == "LONG" else mark - atr_proxy,
        600_000, Decimal("1"), "controlled demo connectivity test", shadow=False)
    print(f"test order: {args.test_side} notional={notional} mark={mark} "
          f"qty={qty} effective={effective}")
    opened = adapter.open(intent, notional)
    print("open:", json.dumps(opened.to_dict(), ensure_ascii=False))
    if not opened.ok:
        # 失败后立刻查残留：绝不盲目重试
        print("residual:", json.dumps(adapter.reconcile(), ensure_ascii=False)[:1500])
        return 6
    if not args.hold_test_order:
        closed = adapter.close(intent, opened.qty, "connectivity_test")
        print("close:", json.dumps(closed.to_dict(), ensure_ascii=False))
        if not closed.ok:
            print("residual:", json.dumps(adapter.reconcile(), ensure_ascii=False)[:1500])
            return 8
    print("reconcile after:", json.dumps(adapter.reconcile(), ensure_ascii=False)[:1500])
    return 0


def _run_dashboard(args) -> int:
    state = build_state(Path(args.root), args.now_ms)
    if args.html:
        Path(args.html).write_text(render_html(state), encoding="utf-8")
        print(f"html -> {args.html}")
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="research.runtime", description="OpenAlpha 自主模拟运行时")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="三种 regime 的完整决策链")
    p_demo.add_argument("--verified", nargs="*", default=[], help="演示用：标记 VERIFIED（DEMO-ONLY）")

    p_run = sub.add_parser("run", help="按小时推进的运行循环（合成数据）")
    p_run.add_argument("--root", default=None)
    p_run.add_argument("--regime", choices=REGIMES, default="trend_up")
    p_run.add_argument("--hours", type=int, default=24)
    p_run.add_argument("--interval-ms", type=int, default=TF_1H_MS)
    p_run.add_argument("--max-rounds", type=int, default=None)
    p_run.add_argument("--verified", nargs="*", default=[])

    p_live = sub.add_parser("live", help="真实 Binance K 线 → RunLoop（双账本、心跳、熔断、幂等）")
    p_live.add_argument("--root", default=None)
    p_live.add_argument("--symbol", default="BTCUSDT")
    p_live.add_argument("--rounds", type=int, default=1)
    p_live.add_argument("--interval-ms", type=int, default=TF_1H_MS)
    p_live.add_argument("--sleep-s", type=float, default=None)
    p_live.add_argument("--limit", type=int, default=300)
    p_live.add_argument("--timeout-s", type=float, default=15.0)
    p_live.add_argument("--funding-bps", type=float, default=1.0,
                        help="资金费（bps / 8h，默认 1bp = 0.01%%）；正数多头支付")
    p_live.add_argument("--no-intel", action="store_true",
                        help="关闭每轮新闻/Polymarket 采集（默认开启，只写日志不改决策）")
    p_live.add_argument("--intel-books", action="store_true",
                        help="是否额外拉 Polymarket 盘口（更慢，默认关）")
    p_live.add_argument("--no-providers", action="store_true",
                        help="关闭 rules/DeepSeek/Jev/linear 对照记录")
    p_live.add_argument("--model", default=None,
                        help="线性模型 JSON 路径（训练后导出，跨机器可迁移）")
    p_live.add_argument("--no-deepseek-desk", action="store_true",
                        help="关闭 DeepSeek 独立实验盘（默认有 key 即启用）")

    p_replay = sub.add_parser("replay", help="真实历史 1h K 线 → 双账本跑 N 轮（72h 回放）")
    p_replay.add_argument("--root", default=None)
    p_replay.add_argument("--symbol", default="BTCUSDT")
    p_replay.add_argument("--rounds", type=int, default=72)
    p_replay.add_argument("--limit", type=int, default=500)
    p_replay.add_argument("--timeout-s", type=float, default=20.0)
    p_replay.add_argument("--funding-bps", type=float, default=1.0)
    p_replay.add_argument("--no-providers", action="store_true")
    p_replay.add_argument("--model", default=None)
    p_replay.add_argument("--no-deepseek-desk", action="store_true")

    p_report = sub.add_parser("report", help="双账本统计（现金/成交/胜率/回撤）")
    p_report.add_argument("--root", required=True)

    p_shadow = sub.add_parser("shadow", help="影子信号之后的 +1h/4h/24h 表现（保守止损优先）")
    p_shadow.add_argument("--root", required=True)
    p_shadow.add_argument("--symbol", default="BTCUSDT")
    p_shadow.add_argument("--limit", type=int, default=500)
    p_shadow.add_argument("--timeout-s", type=float, default=15.0)
    p_shadow.add_argument("--horizons", nargs="*", type=int, default=[1, 4, 24])

    p_v2 = sub.add_parser("v2", help="5m 三账本引擎（规则/DeepSeek/双模型）")
    p_v2.add_argument("--root", default="data/openalpha_v2")
    p_v2.add_argument("--minutes", type=float, default=30.0)
    p_v2.add_argument("--no-second", action="store_true")
    p_v2.add_argument("--demo-exec", action="store_true",
                      help="把指定账本镜像到 Binance Demo（需 Demo 凭据；默认关）")
    p_v2.add_argument("--demo-book", default="deepseek_5m", choices=list(BOOKS),
                      help="镜像哪本账到交易所（默认 deepseek_5m）")

    p_de = sub.add_parser("demo-exec", help="Binance Demo 联调：只读体检 / 受控小额测试单 / 对账")
    p_de.add_argument("--root", default="data/openalpha_demo")
    p_de.add_argument("--symbol", default="BTCUSDT")
    p_de.add_argument("--place-test-order", action="store_true",
                      help="真的下一笔受控小额测试单（需显式开启）")
    p_de.add_argument("--test-notional", type=float, default=120.0,
                      help="测试单名义金额（USDT，默认 120；Demo BTCUSDT minNotional=50，"
                           "取整后低于门槛会被前置拒绝）")
    p_de.add_argument("--test-side", default="LONG", choices=["LONG", "SHORT"])
    p_de.add_argument("--hold-test-order", action="store_true",
                      help="不立刻平掉测试单（默认立刻平，避免留仓）")
    p_de.add_argument("--no-tp", action="store_true", help="只挂止损，不挂止盈")

    p_dash = sub.add_parser("dashboard", help="只读看板（JSON 或 HTML）")
    p_dash.add_argument("--root", required=True)
    p_dash.add_argument("--now-ms", type=int, default=0)
    p_dash.add_argument("--html", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "demo":
        return _run_demo(args)
    if args.cmd == "run":
        return _run_loop(args)
    if args.cmd == "live":
        return _run_live(args)
    if args.cmd == "replay":
        return _run_replay(args)
    if args.cmd == "report":
        return _run_report(args)
    if args.cmd == "v2":
        return _run_v2(args)
    if args.cmd == "demo-exec":
        return _run_demo_exec(args)
    if args.cmd == "shadow":
        return _run_shadow(args)
    return _run_dashboard(args)


if __name__ == "__main__":
    raise SystemExit(main())