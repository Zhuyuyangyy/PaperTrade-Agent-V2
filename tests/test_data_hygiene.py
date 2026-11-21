"""Data hygiene: the holiday-glued-bar class of bug must be DISCLOSED.

Provenance: 345-strategies/thestrat-suite FIX GLUE-1 / LINEEND-CLOSE-1 -- CME
glues holiday half-sessions into the next trade date, producing one 47-hour
daily bar. The engine cannot rebuild period membership, but the anomalous
timestamp interval is visible and must surface in the report, never silently
swallowed. These tests pin that behaviour, plus the honest boundary: hygiene
facts NEVER change the verdict.
"""
from __future__ import annotations

from research.verify import data_hygiene, verify
from research.verify.spec import Spec

H = 3_600_000
T0 = 1_700_000_000_000


def _candles(ts: list[int]) -> list[dict]:
    return [{"t": t, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1.0}
            for t in ts]


class TestIntervalParsing:
    def test_common_intervals(self):
        assert data_hygiene.interval_ms("1h") == H
        assert data_hygiene.interval_ms("15m") == 15 * 60_000
        assert data_hygiene.interval_ms("1d") == 24 * H
        assert data_hygiene.interval_ms("4h") == 4 * H
        assert data_hygiene.interval_ms("1w") == 7 * 24 * H

    def test_unparseable_is_none_not_error(self):
        assert data_hygiene.interval_ms("renko") is None
        assert data_hygiene.interval_ms("") is None


class TestGapDisclosure:
    def test_regular_series_has_no_gaps(self):
        r = data_hygiene.check(_candles([T0 + i * H for i in range(100)]), "1h")
        assert r["n_gaps"] == 0
        assert r["interval_match"] is True
        assert not r["notes"]

    def test_holiday_glued_bar_is_disclosed(self):
        """One 47h gap in an hourly series -- the ES-after-Labor-Day shape."""
        ts = [T0 + i * H for i in range(50)]
        ts += [ts[-1] + 47 * H] + [ts[-1] + 47 * H + i * H for i in range(1, 50)]
        r = data_hygiene.check(_candles(ts), "1h")
        assert r["n_gaps"] == 1
        assert r["gaps"][0]["multiple"] >= 40
        assert any("异常间隔" in n for n in r["notes"])

    def test_interval_mismatch_is_disclosed(self):
        """Spec says 1h, data is actually 4h: bar-count rules change meaning."""
        r = data_hygiene.check(_candles([T0 + i * 4 * H for i in range(100)]), "1h")
        assert r["interval_match"] is False
        assert any("不符" in n for n in r["notes"])

    def test_duplicates_and_unordered_are_counted(self):
        ts = [T0, T0 + H, T0 + H, T0 + 2 * H, T0 + H]
        r = data_hygiene.check(_candles(ts), "1h")
        assert r["duplicates"] == 1
        assert r["unordered"] == 1

    def test_short_series_skips_not_crashes(self):
        r = data_hygiene.check(_candles([T0, T0 + H]), "1h")
        assert r["n_bars"] == 2
        assert r["gaps"] == []


class TestEngineIntegration:
    BASE = {
        "name": "hygiene-test", "symbol": "TESTUSDT", "interval": "1h",
        "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 12},
        "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 35},
        "control": {"enabled": False},
        "falsify_if": {"min_fills": 1},
    }

    def _run(self, candles):
        from research.verify.signals import rule_signals
        spec = Spec.from_dict(self.BASE)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        return verify(spec, candles, entries)

    def _gapped_candles(self):
        import random
        rng = random.Random(7)
        out, px = [], 100.0
        for i in range(400):
            px *= 1.0 + rng.gauss(0.0004, 0.01)
            o = px
            c = px * (1.0 + rng.gauss(0.0, 0.004))
            h = max(o, c) * 1.003
            l = min(o, c) * 0.997
            # a 47-hour hole after bar 200: the holiday-glued shape
            t = T0 + i * H + (47 * H if i > 200 else 0)
            out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
        return out

    def test_verify_result_carries_hygiene_and_report_renders_it(self):
        from research.verify import report
        candles = self._gapped_candles()
        r = self._run(candles)
        assert r["data_hygiene"]["n_gaps"] >= 1
        md = report.render(r)
        assert "数据卫生" in md
        assert "异常间隔" in md

    def test_hygiene_never_changes_the_verdict(self):
        """Same prices, same signals, one series with a timestamp hole:
        the verdict must be identical -- hygiene is disclosure, not evidence."""
        candles = self._gapped_candles()
        shifted = [dict(c, t=T0 + i * H) for i, c in enumerate(candles)]
        r1, r2 = self._run(candles), self._run(shifted)
        assert r1["verdict"]["outcome"] == r2["verdict"]["outcome"]
        assert r1["runs"]["worst_case"]["stats"] == r2["runs"]["worst_case"]["stats"]
        assert r1["data_hygiene"]["n_gaps"] >= 1
        assert r2["data_hygiene"]["n_gaps"] == 0

    def test_html_report_renders_hygiene(self):
        from research.verify.html_report import render_html
        r = self._run(self._gapped_candles())
        html = render_html(r)
        assert "数据卫生披露" in html
