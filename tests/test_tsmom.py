"""TSMOM 研究的回归测试。

守两件事，都是这个仓库最容易悄悄坏掉的地方：

1. **信号生成器不能前视**。这是唯一无法靠「看输出对不对」发现的错误——
   一个用了未来数据的信号，回测会很好看，而且不会报错。所以要用构造性测
   试证明：把第 i 根之后的数据全部删掉，position[i] 必须一模一样。

2. **报告里的数字必须能从产物复现**。研究报告最不能犯的错是叙述与数据脱
   节——尤其在这份报告里，结论是「没找到 alpha」，如果数字漂了，可能漂成
   「找到了」。

这些测试刻意不联网、不依赖引擎，只校验生成器与已落盘的产物，
所以 CI 里跑得很快。
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
TSMOM = REPO / "research" / "tsmom"
sys.path.insert(0, str(TSMOM))

# 直接按文件路径加载 research/tsmom/signals.py。不放进 sys.path 靠模块名找，
# 是因为「signals」是个太通用的名字，容易和别处的同名模块撞车，撞了之后
# 测试会静默地测到另一个文件——比报错更糟。
_spec = importlib.util.spec_from_file_location("tsmom_signals", TSMOM / "signals.py")
sg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sg)


# --------------------------------------------------------------------------
# 1. 前视偏差：结构性证明
# --------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["tsmom", "riskadj", "regime"])
@pytest.mark.parametrize("lookback", [10, 30, 60])
def test_signal_does_not_look_ahead(variant, lookback):
    """截断未来数据后，同一时点的持仓必须不变。

    做法：取前 k 根的信号，与全长序列的前 k 项逐项比较。只要生成器里出现
    任何 closes[i+1] 之类的下标，截断后结果就会变。
    """
    candles = sg.read_candles(TSMOM / "data" / "BTCUSDT.csv")
    closes_full = [c["c"] for c in candles]

    fn = {
        "tsmom": lambda c: sg.positions_tsmom(c, lookback),
        "riskadj": lambda c: sg.positions_riskadj(c, lookback, 1.0),
        "regime": lambda c: sg.positions_regime(c, lookback, 200),
    }[variant]

    full = fn(closes_full)
    for k in (300, 800, 1500):
        if k <= lookback + 5:
            continue
        part = fn(closes_full[:k])
        assert part == full[:k], (
            f"{variant}/{lookback} 在 k={k} 处出现前视：截断后信号变了。"
            "生成器里用了未来数据。"
        )


def test_positions_are_valid_directions():
    """持仓只能取 -1 / 0 / +1，引擎的 side 映射只认这三态。"""
    candles = sg.read_candles(TSMOM / "data" / "BTCUSDT.csv")
    closes = [c["c"] for c in candles]
    for pos in (sg.positions_tsmom(closes, 30),
                sg.positions_riskadj(closes, 30, 1.0),
                sg.positions_regime(closes, 30, 200)):
        assert set(pos) <= {-1, 0, 1}


def test_entries_exits_pair_up():
    """每次翻仓必须成对：旧仓平、新仓建，段数应与方向变化数一致。"""
    candles = sg.read_candles(TSMOM / "data" / "BTCUSDT.csv")
    closes = [c["c"] for c in candles]
    pos = sg.positions_tsmom(closes, 30)
    entries, exits = sg.to_entries_exits(candles, pos)

    flips = sum(1 for i in range(1, len(pos)) if pos[i] != pos[i - 1])
    # exits 含翻仓 + 尾部补平
    assert len(exits) in (flips, flips + 1), f"exits={len(exits)} flips={flips}"
    # 入场数不超过翻仓数（翻到空仓不产生入场）
    assert len(entries) <= flips
    # 每个入场方向都必须合法
    assert all(e["side"] in ("long", "short") for e in entries)


def test_final_position_gets_closed():
    """末尾未平的仓必须补一条退出，否则最后一笔的盈亏会被丢掉。"""
    candles = sg.read_candles(TSMOM / "data" / "BTCUSDT.csv")
    closes = [c["c"] for c in candles]
    pos = sg.positions_tsmom(closes, 30)
    assert pos[-1] != 0, "测试前提：BTC 末尾应当有持仓"
    _, exits = sg.to_entries_exits(candles, pos)
    assert exits[-1]["t"] == candles[-1]["t"], "末尾持仓没有被平掉"


# --------------------------------------------------------------------------
# 2. 报告数字可复现
# --------------------------------------------------------------------------

def _scan():
    p = TSMOM / "_out" / "scan_results.json"
    if not p.is_file():
        pytest.skip("scan_results.json 不存在（先跑 scan.py）")
    return [r for r in json.loads(p.read_text(encoding="utf-8")) if "error" not in r]


def _fair():
    p = TSMOM / "_out" / "fair_control.json"
    if not p.is_file():
        pytest.skip("fair_control.json 不存在（先跑 fair_control.py）")
    return json.loads(p.read_text(encoding="utf-8"))


def test_scan_covers_declared_matrix():
    rows = _scan()
    assert len(rows) == 300, f"扫描矩阵应为 300 条，实际 {len(rows)}"
    assert {r["variant"] for r in rows} == {"tsmom", "tsmom_long", "tsmom_short",
                                            "riskadj", "regime"}


def test_naive_selectivity_rate_is_the_bug_not_the_finding():
    """82.3% 这个数字是缺陷的度量，不是成绩。它必须被固定在报告里，
    否则将来有人重新跑出 90% 会以为自己发现了更好的策略。"""
    rows = _scan()
    passed = sum(1 for r in rows
                 if r.get("control_ci") and not (r["control_ci"][0] <= 0 <= r["control_ci"][1]))
    rate = passed / len(rows) * 100
    assert abs(rate - 82.3) < 0.5, (
        f"修正前通过率应为 82.3%（这是对照组无限期持有造成的假象），实际 {rate:.1f}%"
    )


def test_fair_control_kills_the_false_positives():
    """核心结论：把对照组改公平后，通过的只剩个位数。

    这个测试是整个研究的守门人。如果它失败（通过率变高），说明
    fair_control.py 里的超时对齐逻辑坏了，报告结论随即失效。
    """
    rows = _fair()
    passed = sum(1 for r in rows
                 if r.get("control_ci") and not (r["control_ci"][0] <= 0 <= r["control_ci"][1]))
    assert passed <= 5, (
        f"公平对照组下通过 {passed} 个，超出噪声上限。"
        "要么发现了真 alpha（需要独立复现），要么超时对齐逻辑失效。"
    )
    # 与纯随机期望（n*0.05）对比：通过的个数不应显著超过期望
    assert passed <= max(3, len(rows) * 0.05 * 2), "通过数已超出多重比较噪声区间"


def test_fair_control_timeout_matches_hold_length():
    """超时必须等于策略持仓中位数，否则「公平」二字不成立。"""
    for r in _fair():
        assert r["fair_timeout"] == r["hold_median"], (
            f"{r['name']}: 超时 {r['fair_timeout']} != 中位持仓 {r['hold_median']}"
        )


def test_long_leg_earns_short_leg_loses():
    """方向不对称是「钱来自 beta 而非择时」的直接证据。"""
    rows = _scan()
    lg = [r["compound_x"] for r in rows if r["variant"] == "tsmom_long" and r.get("compound_x")]
    sh = [r["compound_x"] for r in rows if r["variant"] == "tsmom_short" and r.get("compound_x")]
    assert statistics.median(lg) > 1, "多头腿中位应为正"
    assert statistics.median(sh) < 1, "空头腿中位应为负"
    assert sum(1 for x in lg if x > 1) > sum(1 for x in sh if x > 1), \
        "多头腿赚钱的配置数应远多于空头腿"


def test_funding_meta_is_complete():
    p = TSMOM / "funding" / "_meta.json"
    if not p.is_file():
        pytest.skip("资金费元数据不存在")
    meta = json.loads(p.read_text(encoding="utf-8"))
    ok = [k for k, v in meta.items() if "error" not in v]
    assert len(ok) >= 10, f"应有 10 个币的资金费，实际 {len(ok)}"
    btc = meta["BTCUSDT"]
    assert btc["n"] > 7000, "BTC 资金费条数过少，可能只拉了近半年"
    # 均值必须为正——BTC 长期正资金费是这份研究的关键成本事实
    assert btc["mean_bps"] > 0.5, f"BTC 资金费均值 {btc['mean_bps']}bps 异常"


def test_data_has_no_gaps():
    """零空洞是数据质量前提，破了下游全部结论不可信。"""
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        p = TSMOM / "data" / f"{sym}.csv"
        if not p.is_file():
            pytest.skip(f"{sym}.csv 不存在")
        candles = sg.read_candles(p)
        gaps = sum(1 for i in range(1, len(candles))
                   if candles[i]["t"] - candles[i - 1]["t"] != 86_400_000)
        assert gaps == 0, f"{sym} 有 {gaps} 处空洞"


def test_data_files_use_lf():
    """数据文件必须是 LF。

    踩过这个坑：csv.writer 配 newline="" 会跟随平台默认行尾，Windows 写
    CRLF、Linux 写 LF，同一份数据在两台机器上字节不同。虽然 `.gitattributes`
    里 `*.csv -text` 已经阻止 git 改写，但生成器本身不一致仍会让「同一份数
    据」变成两份字节——docs/falsifier 就因此出过 4724 字节的偏差。
    """
    for p in sorted((TSMOM / "data").glob("*.csv")) + sorted((TSMOM / "funding").glob("*.csv")):
        raw = p.read_bytes()
        assert b"\r\n" not in raw, f"{p.name} 含 CRLF，应统一为 LF"


def test_funding_covers_the_research_window():
    """资金费必须覆盖样本窗口。

    端点不带 startTime 时只返回最近 500 条（166 天），看起来完整实则缺九年。
    用那样的数据算持仓成本，等于把最大的那项成本悄悄抹掉。
    """
    for sym in ("BTCUSDT", "ETHUSDT"):
        p = TSMOM / "funding" / f"{sym}.csv"
        if not p.is_file():
            pytest.skip(f"{sym} 资金费不存在")
        import csv as _csv
        with p.open(newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        assert len(rows) > 6000, f"{sym} 资金费只有 {len(rows)} 条，可能只拉了近半年"
        first = int(rows[0]["t"])
        assert first < 1_600_000_000_000, (
            f"{sym} 资金费起点晚于 2020-09，覆盖不到研究窗口"
        )

# --------------------------------------------------------------------------
# 4. 2026-11-28 审阅修订：配对比较、2×2 消融、裁决口径
# --------------------------------------------------------------------------

def test_paired_control_comparison_uses_same_48_configs():
    """对照组修订必须配对比较：300 vs 48 是不可比的，那是我犯过的错。"""
    scan = json.loads((TSMOM / "_out" / "scan_results.json").read_text(encoding="utf-8"))
    fair = json.loads((TSMOM / "_out" / "fair_control.json").read_text(encoding="utf-8"))
    scan_map = {(r.get("symbol"), r.get("variant"), r.get("lookback")): r
                for r in scan if "error" not in r}

    def passes(r):
        ci = r.get("control_ci")
        return bool(ci and not (ci[0] <= 0 <= ci[1]))

    pairs = [(scan_map[(f["symbol"], f["variant"], f["lookback"])], f) for f in fair
             if (f["symbol"], f["variant"], f["lookback"]) in scan_map]
    assert len(pairs) == 48
    naive = sum(1 for s, _ in pairs if passes(s))
    fixed = sum(1 for _, f in pairs if passes(f))
    assert naive == 34, f"朴素对照组通过数漂了：{naive}（应为 34 = 70.8%）"
    assert fixed == 3, f"公平对照组通过数漂了：{fixed}（应为 3 = 6.2%）"


def test_ablation_2x2_survivors_and_d_b():
    """D−B 是原假设真正要检验的量，必须固化为证据不足而非证伪。"""
    rows = json.loads((TSMOM / "_out" / "ablation_2x2.json").read_text(encoding="utf-8"))
    assert len(rows) == 20
    for key, want_median, want_n_pos in (("C_minus_A", -10.11, 8),
                                          ("B_minus_A", -25.01, 0),
                                          ("D_minus_B", -0.54, 8)):
        vals = [r[key]["ann_pct"] for r in rows]
        assert abs(statistics.median(vals) - want_median) < 0.02, (
            f"{key} 中位漂了：{statistics.median(vals)}")
        assert sum(1 for v in vals if v > 0) == want_n_pos
    # D−B 的 CI 全部跨 0 —— 这是「证据不足」而非「已证伪」的判据
    for r in rows:
        lo, hi = r["D_minus_B"]["ci95_ann_pct"]
        assert lo <= 0 <= hi, f"{r['symbol']} 的 D−B CI 不再跨 0，结论需重审"


def test_verdict_is_partial_implementation_not_falsified():
    """完整 TSMOM 未被证伪：裁决写错比数字写错更严重。"""
    report = (TSMOM / "REPORT.md").read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for name, text in (("REPORT.md", report), ("README.md", readme)):
        assert "INCONCLUSIVE — PARTIAL IMPLEMENTATION" in text, (
            f"{name} 丢掉了 PARTIAL IMPLEMENTATION 裁决")
        # 允许在「具体配置」语境里出现 FALSIFIED，也允许在否定句里出现
        # 「不能证伪」；禁止的是旧版那个无条件裁决句。
        for stale in ("**同样 FALSIFIED**", "10 币日线 300 个配置，**同样 FALSIFIED**",
                      "总表增至 16 条，仍为 0 条通过。详见"):
            assert stale not in text, f"{name} 仍残留旧版整体裁决：{stale}"


def test_replay_engine_compat_is_trade_identical():
    """回放器必须先逐位复现引擎，才有资格改引擎的成交假设。"""
    spec = importlib.util.spec_from_file_location("tsmom_replay", TSMOM / "replay.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    eng_spec = importlib.util.spec_from_file_location(
        "tsmom_engine_mod", REPO / "skill" / "strategy-falsifier" / "agensi" / "scripts" / "verify.py")
    eng = importlib.util.module_from_spec(eng_spec)
    sys.modules["tsmom_engine_mod"] = eng
    eng_spec.loader.exec_module(eng)

    sym, lb = "BTCUSDT", 30
    meta = sg.build(sym, "tsmom_long", lb, offset_start="2020-09-01", offset_end="2026-11-26")
    candles = rp.read_candles(TSMOM / "data" / f"{sym}.csv")
    ent_t = rp.read_signal_csv(TSMOM / meta["entries_path"], "side")
    exi_t = rp.read_signal_csv(TSMOM / meta["exits_path"], "reason")
    idx = {c["t"]: i for i, c in enumerate(candles)}
    rule = eng.Exit(timeout_bars=100000,
                    exit_signals={idx[t]: s for t, s in exi_t.items()})
    sim = eng.simulate(candles, {idx[t]: s for t, s in ent_t.items()},
                       rule, fee_rate=0.0004)
    mine = rp.replay_trades(candles, ent_t, exi_t, mode="engine_compat")
    assert len(sim.fills) == len(mine) == 94
    assert all(abs(f.net_pct - m["net"]) < 1e-9
               for f, m in zip(sim.fills, mine))
