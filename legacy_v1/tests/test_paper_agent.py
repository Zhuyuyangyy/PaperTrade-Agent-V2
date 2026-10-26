import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("paper_agent", ROOT / "paper_agent.py")
pa = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = pa
spec.loader.exec_module(pa)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = pa.Ledger(Path(self.tmp.name) / "paper.db", 1000)
        self.cfg = pa.load_config(ROOT / "settings.json")
        self.now = datetime(2024, 5, 1, tzinfo=timezone.utc)
        self.candles = pa.synthetic_candles("BTCUSDT", 120)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_buy_sell_fee_slippage_and_accounting(self):
        self.ledger.buy(self.now, "BTCUSDT", 100, 100, self.cfg, "test")
        p = self.ledger.positions()["BTCUSDT"]
        self.assertLess(p["qty"], 1.0)
        self.assertAlmostEqual(self.ledger.cash, 899.9)
        self.ledger.sell(self.now, "BTCUSDT", 100, self.cfg, "test")
        self.assertEqual(self.ledger.positions(), {})
        self.assertLess(self.ledger.cash, 1000)
        self.assertGreater(self.ledger.cash, 998)

    def test_no_pyramiding(self):
        self.ledger.buy(self.now, "BTCUSDT", 100, 100, self.cfg, "test")
        with self.assertRaises(AssertionError):
            self.ledger.buy(self.now, "BTCUSDT", 100, 100, self.cfg, "test")

    def test_reused_candle_cannot_buy_twice(self):
        self.candles = self._buy_candles()
        market = {"BTCUSDT": (self.candles, 100.0), "ETHUSDT": (self._buy_candles(), 100.0)}
        pa.step(self.ledger, market, self.now, self.cfg)
        n1 = self.ledger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        pa.step(self.ledger, market, self.now + timedelta(minutes=1), self.cfg)
        n2 = self.ledger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertGreaterEqual(n1, 1)

    def test_global_exposure_cap(self):
        market = {"BTCUSDT": (self._buy_candles(), 100), "ETHUSDT": (self._buy_candles(), 100)}
        pa.step(self.ledger, market, self.now, self.cfg)
        _, holdings, total = self.ledger.equity({"BTCUSDT": 100, "ETHUSDT": 100})
        self.assertLessEqual(holdings / total, self.cfg["max_total_exposure"] + 1e-6)
        self.assertGreaterEqual(self.ledger.cash, 0)

    def test_loss_cutoff_prevents_new_buy(self):
        self.ledger.conn.execute("INSERT INTO equity (ts,cash,positions_value,total) VALUES (?,?,?,?)",
                                 (pa.iso(self.now), 1000, 0, 1000))
        self.ledger.conn.execute("UPDATE state SET value=? WHERE key='cash'", ("970",))
        self.ledger.conn.commit()
        market = {"BTCUSDT": (self._buy_candles(), 100), "ETHUSDT": (self._buy_candles(), 100)}
        pa.step(self.ledger, market, self.now + timedelta(hours=1), self.cfg)
        self.assertEqual(self.ledger.positions(), {})
        outcomes = [x[0] for x in self.ledger.conn.execute("SELECT outcome FROM decisions")]
        self.assertTrue(all(x == "BLOCKED_DAILY_LOSS" for x in outcomes))

    def test_intrabar_stop_applies_on_duplicate_bar(self):
        market = {"BTCUSDT": (self._buy_candles(), 100), "ETHUSDT": (self._buy_candles(), 100)}
        pa.step(self.ledger, market, self.now, self.cfg)
        self.assertIn("BTCUSDT", self.ledger.positions())
        market2 = {"BTCUSDT": (market["BTCUSDT"][0], 96), "ETHUSDT": market["ETHUSDT"]}
        pa.step(self.ledger, market2, self.now + timedelta(minutes=1), self.cfg)
        self.assertNotIn("BTCUSDT", self.ledger.positions())

    @staticmethod
    def _buy_candles():
        candles = pa.synthetic_candles("BTCUSDT", 120)
        # deterministic rising path with predictable SMA ordering, capped at open/close coherence
        out = []
        for i, x in enumerate(candles):
            p = 50 + i*0.5
            out.append(pa.Candle(x.open_ms, p, p+1, p-1, p, 1, x.close_ms))
        return out


if __name__ == "__main__":
    unittest.main()
