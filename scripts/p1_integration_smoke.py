"""P1 端到端冒烟：双采集器真连 → EventStore → 对齐 → 标签 → A/B/C（只读）。

Sprint 0 后口径：
  - 对齐是 point-in-time 的：每个决策点只用该时刻已可用的数据；
  - 标签来自 label_engine（相对收益，且必须落在 origin+H 之后）；
  - A/B/C 用同一批决策点上的 payoff 比较；样本不足时诚实地给 INCONCLUSIVE。
"""
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
from research.cross_market.label_engine import label_from_store
from research.cross_market.market_mapping import load_mapping, researchable_markets
from research.cross_market.storage import EventStore


def main() -> None:
    mapping = researchable_markets(load_mapping(Path("config/market_mapping.yaml")))
    key, m = next(iter(mapping.items()))
    print(f"市场: {m.underlying_asset} {m.event_comparator} {m.event_threshold} ({key})")

    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td) / "p1.sqlite")
        # 1) Binance 微结构（3 次采样）
        bc = BinanceMicroCollector(store, ["BTCUSDT"])
        for _ in range(3):
            try:
                bc.snapshot("BTCUSDT")
            except Exception as e:  # noqa: BLE001
                print("binance 采集失败（如实）:", str(e)[:120])
                break
            time.sleep(1)
        # 2) Polymarket 盘口（2 次，验证去重）
        pc = PolyRestCollector(store, {key: {"gamma_id": m.gamma_id,
                                             "clob_token_ids": list(m.clob_token_ids)}})
        for _ in range(2):
            try:
                pc.poll_once()
            except Exception as e:  # noqa: BLE001
                print("polymarket 采集失败（如实）:", str(e)[:120])
                break
            time.sleep(1)
        print(f"落盘事件: {store.count()}（binance {bc.stats['snapshots']} + poly {pc.stats['events']}）")
        print(f"采集诊断: poly 重复跳过 {pc.stats['duplicates_skipped']} / "
              f"缺服务端时间戳 {pc.stats['skipped_missing_server_ts']} / "
              f"交叉盘口 {pc.stats['crossed_books']} / 时钟偏移 {pc.stats['skew_ms_last']}ms")

        # 3) 对齐（point-in-time；决策时刻上限 = now，不用未来数据回填历史点）
        now = int(time.time() * 1000)
        pts = align_features(store, key, list(m.clob_token_ids), "BTCUSDT",
                             now - 120_000, now + 1, decision_now_ms=now, step_ms=5_000)
        print(f"对齐点: {len(pts)}")
        for p in pts[-3:]:
            poly = p.feature.polymarket
            print(f"  t={p.decision_ts_ms} yes_mid={poly.get('yes_mid')} "
                  f"consistent={poly.get('consistent')} bin_price={p.feature.binance.get('price')}")

        # 4) 标签（未来窗口不足 → None，不猜）
        h = Hypothesis(hypothesis_id="smoke", market_key=key, underlying=m.underlying_asset,
                       primary_horizon_ms=300_000, event_comparator=m.event_comparator)
        labels = [label_from_store(store, "BTCUSDT", p.decision_ts_ms, h.primary_horizon_ms)
                  for p in pts]
        n_labelled = sum(1 for l in labels if l is not None)
        print(f"可用标签: {n_labelled}/{len(labels)}")

        # 5) A/B/C（样本极小 → 预期 INCONCLUSIVE，诚实输出）
        points = [{"decision_ts_ms": p.decision_ts_ms,
                   "polymarket": p.feature.polymarket,
                   "binance": p.feature.binance} for p in pts]
        v = run_abc(points, [l.future_return if l else None for l in labels], h)
        print(f"A/B/C: {v.verdict} — {v.reason}")
        print(f"  共同决策点 {v.aligned_n}/{v.n_decision_points}；"
              f"arms={ {k: (round(a.hit, 3), a.n) for k, a in v.arms.items()} }")
        store.close()


if __name__ == "__main__":
    main()