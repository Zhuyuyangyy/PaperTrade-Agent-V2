"""Trade-record autopsy: the honest subset of the battery, no candles needed."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tv_csv(path: Path, pnls: list[float]) -> Path:
    """Write a TradingView-style 'List of Trades' export."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Trade #", "Type", "Signal", "Date/Time", "Price USD",
                    "Position USD", "Net P&L USD"])
        t0 = 1_700_000_000_000
        for i, p in enumerate(pnls, 1):
            w.writerow([i, "Exit long", "x", f"2024-01-{i % 28 + 1:02d} 10:00",
                        100.0 + i, 1000.0, f"{p:.2f}"])
    return path


def test_loads_tradingview_export(tmp_path: Path):
    from research.verify.csv_auditor import load_trades_csv

    p = _tv_csv(tmp_path / "trades.csv", [10.0, -5.0, 7.5, -2.0])
    trades = load_trades_csv(p)
    assert len(trades) == 4
    assert trades[0]["pnl"] == 10.0 and trades[1]["pnl"] == -5.0
    assert trades[0]["t"] is not None  # date parsed


def test_cost_stress_kills_the_big_swing_book(tmp_path: Path):
    """A +50/-20 swing book looks great at 1x and dies at 2x: its per-trade
    mean is +15, but doubling costs flips every winner and every loser."""
    from research.verify.csv_auditor import audit_trades, load_trades_csv

    pnls = [50.0] * 15 + [-20.0] * 15
    p = _tv_csv(tmp_path / "trades.csv", pnls)
    r = audit_trades(load_trades_csv(p))
    assert r["outcome"] == "FAILED_STRESS"
    assert "成本 ×2" in r["reason"]


def test_outlier_carried_book_fails_resample_bootstrap(tmp_path: Path):
    """Thirty small losses and three enormous wins: resample the same size
    and most draws never see the outliers -- the CI spans zero."""
    from research.verify.csv_auditor import audit_trades

    pnls = [-5.0] * 30 + [500.0, 480.0, 460.0]
    r = audit_trades(pnls)
    ci = r["sequence_bootstrap"]["ci95"]
    assert ci[0] <= 0 <= ci[1], r["sequence_bootstrap"]
    assert r["outcome"] == "FAILED_STRESS"
    assert "bootstrap" in r["reason"]


def test_cost_stress_kills_a_thin_edge(tmp_path: Path):
    from research.verify.csv_auditor import audit_trades

    # 40 trades, +0.8/-0.5: the mean is positive but the spread is wide --
    # doubling costs turns every winner into a loser
    pnls = [0.8] * 25 + [-0.5] * 15
    r = audit_trades(pnls)
    x2 = next(x for x in r["cost_sensitivity"] if x["fee_multiple"] == 2.0)
    assert x2["net_sum_pct"] < r["base"]["sum_pct"]
    assert r["outcome"] == "FAILED_STRESS"
    assert "成本 ×2" in r["reason"]


def test_robust_book_survives_with_disclosed_limits(tmp_path: Path):
    from research.verify.csv_auditor import audit_trades

    # steady, tight edge: survives doubled costs, resampling, and trimming
    pnls = [1.5] * 30 + [-0.3] * 10
    r = audit_trades(pnls)
    assert r["outcome"] == "SURVIVED_STRESS"
    # the survival must carry the limits on its face
    assert "不能做路径感知回放" in r["limits"]


def test_small_samples_are_insufficient_not_failed(tmp_path: Path):
    from research.verify.csv_auditor import audit_trades

    r = audit_trades([10.0, -5.0, 3.0])
    assert r["outcome"] == "INSUFFICIENT"
    assert "噪声" in r["reason"]


def test_render_states_the_boundary_first(tmp_path: Path):
    from research.verify.csv_auditor import audit_trades, render

    r = audit_trades([1.0] * 20 + [-0.5] * 20)
    text = render(r)
    assert "本模式的边界（先读）" in text
    assert "序列级" in text and "不是完整证伪" in text
