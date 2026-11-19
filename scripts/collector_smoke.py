"""P1 采集器真跑一次（只读）：对映射市场拉盘口 → EventStore → 可用性门禁演示。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.cross_market.collectors.polymarket_rest import PolyRestCollector
from research.cross_market.market_mapping import load_mapping, researchable_markets
from research.cross_market.replay import ReplayEngine, usable_at
from research.cross_market.storage import EventStore


def main() -> None:
    mapping = researchable_markets(load_mapping(Path("config/market_mapping.yaml")))
    print(f"可研究市场: {len(mapping)} 个")
    markets = {k: {"gamma_id": v.gamma_id, "clob_token_ids": list(v.clob_token_ids)} for k, v in mapping.items()}

    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td) / "smoke.sqlite")
        c = PolyRestCollector(store, markets)
        events = c.poll_once()
        print(f"采集: {len(events)} 个盘口事件（stats: {c.stats}）")
        for ev in events[:4]:
            print(f"  {ev.market_id[:12]}… token={ev.token_id[:8]}… bid={ev.bid} ask={ev.ask} "
                  f"spread_bps={ev.spread_bps} event_ts={ev.event_ts_ms} ingest_ts={ev.ingest_ts_ms}")

        # 可用性门禁演示：用真实事件的 (event_ts, ingest_ts) 构造决策时刻
        if events:
            ev = events[0]
            avail = usable_at(ev.event_ts_ms, ev.ingest_ts_ms)
            print(f"\n可用性门禁演示: 事件在 max(event={ev.event_ts_ms}, ingest={ev.ingest_ts_ms}) = {avail} 才可用")
            state = ReplayEngine(store).run(0, avail + 1, decision_now_ms=avail)
            print(f"决策时刻={avail}: 可用事件 {state.seen_events} 条，被挡 {state.skipped_not_yet_available} 条")
            f = ReplayEngine(store).feature_at(ev.token_id, "ETHUSDT")
            print(f"特征可取到该市场盘口: {f['polymarket'] is not None}")


if __name__ == "__main__":
    main()
