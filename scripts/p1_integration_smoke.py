"""P1 端到端冒烟：双采集器真连 → EventStore → 对齐 → A/B/C（只读）。"""
from __future__ import annotations

import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.cross_market.collectors.binance_microstructure import BinanceMicroCollector
from research.cross_market.collectors.polymarket_rest import PolyRestCollector
from research.cross_market.experiment import Hypothesis, run_abc
from research.cross_market.feature_engine import align_features
from research.cross_market.market_mapping import load_mapping, researchable_markets
from research.cross_market.storage import EventStore


def main() -> None:
    mapping = researchable_markets(load_mapping(Path("config/market_mapping.yaml")))
    mk = next(iter(mapping.items()))
    key, m = mk
    print(f"市场: {m.underlying_asset} {m.event_comparator} {m.event_threshold} ({key})")

    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td) / "p1.sqlite")
        # 1) Binance 微结构（3 次采样）
        bc = BinanceMicroCollector(store, ["BTCUSDT"])
        for _ in range(3):
            try:
                bc.snapshot("BTCUSDT")
            except Exception as e:  # noqa: BLE001
                print("binance 采集失败（如实）:", str(e)[:100])
                break
            time.sleep(1)
        # 2) Polymarket 盘口（1 次）
        pc = PolyRestCollector(store, {key: {"gamma_id": m.gamma_id, "clob_token_ids": list(m.clob_token_ids)}})
        try:
            pc.poll_once()
        except Exception as e:  # noqa: BLE001
            print("polymarket 采集失败（如实）:", str(e)[:100])
        print(f"落盘事件: {store.count()}（binance {bc.stats['snapshots']} + poly {pc.stats['events']}）")

        # 3) 对齐（决策时刻取 now+10s：容纳 CLOB 服务端时钟超前 ~4s 的保守延迟）
        now = int(time.time() * 1000)
        decision = now + 10_000
        pts = align_features(store, key, list(m.clob_token_ids), "BTCUSDT",
                             now - 3_600_000, decision + 1000, decision_now_ms=decision, step_ms=60_000)
        print(f"对齐点: {len(pts)}")
        for p in pts[-2:]:
            print(f"  t={p.decision_ts_ms} poly_keys={sorted(p.feature.polymarket)} bin_keys={sorted(p.feature.binance)}")

        # 4) A/B/C（样本极小 → 预期 INCONCLUSIVE，诚实输出）
        h = Hypothesis(hypothesis_id="smoke", market_key=key, underlying=m.underlying_asset,
                       primary_horizon_ms=300_000)
        points = [{"polymarket": p.feature.polymarket, "binance": p.feature.binance} for p in pts]
        labels = [Decimal("0")] * len(points)  # 冒烟无未来标签
        v = run_abc(points, labels, h)
        print(f"A/B/C: {v.verdict} — {v.reason}")
        store.close()


if __name__ == "__main__":
    main()
