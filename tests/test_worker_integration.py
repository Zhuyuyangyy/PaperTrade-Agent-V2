"""Simulated transport exercises full CCXT reader -> voters -> JSON publisher."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import json

from research import worker
from user_data.strategies.signal_gate import approved_from_file

NOW = int(datetime(2026, 10, 23, 22, 5, tzinfo=timezone.utc).timestamp() * 1000)


class FakeKraken:
    id = "kraken"
    markets = {"BTC/USDT": {}, "ETH/USDT": {}}
    def __init__(self, bad_pair=None, bad_ticker_pair=None):
        self.bad_pair = bad_pair
        self.bad_ticker_pair = bad_ticker_pair
        self.calls = []
        self.ticker_calls = []
    def fetch_ohlcv(self, pair, timeframe, limit):
        self.calls.append((pair, timeframe, limit))
        rows = worker.fake_closed_rows(NOW, factor=2.0 if pair == "ETH/USDT" else 1.0)
        if pair == self.bad_pair:
            rows[-1][0] += 80_000
        return rows
    def fetch_ticker(self, pair):
        self.ticker_calls.append(pair)
        if pair == self.bad_ticker_pair:
            raise RuntimeError("simulated ticker outage")
        rows = worker.fake_closed_rows(NOW, factor=2.0 if pair == "ETH/USDT" else 1.0)
        return {"last": rows[-1][4], "timestamp": NOW}


def test_full_public_reader_to_2_tradeable_json_signals(tmp_path, monkeypatch):
    fake = FakeKraken()
    monkeypatch.setattr(worker, "open_public_exchange", lambda: fake)
    monkeypatch.setattr(worker, "utc_ms", lambda: NOW)
    out = tmp_path / "real_signals"
    log = tmp_path / "decisions.jsonl"
    marks = tmp_path / "marks"
    decisions = worker.run_once(out, log, "off", tmp_path / "ta", marks)
    assert len(decisions) == 2
    assert len(fake.calls) == 2
    assert len(fake.ticker_calls) == 2
    for pair in ("BTC/USDT", "ETH/USDT"):
        mark = json.loads((marks / f"{pair.replace('/', '_')}.json").read_text())
        assert mark["source"] == "ccxt-public:kraken"
        assert mark["pair"] == pair
        assert mark["price"] > 0
    for d in decisions:
        assert d["source"] == "ccxt-public:kraken"
        assert approved_from_file(out, d["pair"], d["candle_open_ms"], NOW)[0] is True
    assert len(log.read_text().splitlines()) == 2
    worker.run_once(out, log, "off", tmp_path / "ta", marks)
    assert len(log.read_text().splitlines()) == 2  # Same closed candle is idempotent.


def test_one_pair_corrupt_prevents_writing_either_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "open_public_exchange", lambda: FakeKraken(bad_pair="ETH/USDT"))
    monkeypatch.setattr(worker, "utc_ms", lambda: NOW)
    out = tmp_path / "signals"
    with pytest.raises(ValueError):
        worker.run_once(out, tmp_path / "log", "off", tmp_path / "ta")
    assert not out.exists() or not list(out.iterdir())


def test_ticker_outage_does_not_erase_valid_signals_but_mark_is_missing(tmp_path, monkeypatch):
    fake = FakeKraken(bad_ticker_pair="ETH/USDT")
    monkeypatch.setattr(worker, "open_public_exchange", lambda: fake)
    monkeypatch.setattr(worker, "utc_ms", lambda: NOW)
    out = tmp_path / "signals"
    marks = tmp_path / "marks"
    decisions = worker.run_once(out, tmp_path / "decisions.jsonl", "off", tmp_path / "ta", marks)
    assert len(decisions) == 2
    assert (out / "BTC_USDT.json").is_file()
    assert (out / "ETH_USDT.json").is_file()
    assert (marks / "BTC_USDT.json").is_file()
    assert not (marks / "ETH_USDT.json").exists()


def test_public_reader_rejects_non_kraken_and_unapproved_pairs():
    fake = FakeKraken()
    fake.id = "other"
    with pytest.raises(RuntimeError):
        worker.fetch_public(fake, "BTC/USDT")
    fake.id = "kraken"
    with pytest.raises(RuntimeError):
        worker.fetch_public(fake, "DOGE/USDT")


def test_public_mark_rejects_stale_exchange_timestamp(monkeypatch):
    fake = FakeKraken()
    fake.fetch_ticker = lambda pair: {"last": 100.0, "timestamp": NOW - 6 * 60 * 1000}
    monkeypatch.setattr(worker, "utc_ms", lambda: NOW)
    with pytest.raises(ValueError, match="stale"):
        worker.fetch_public_mark(fake, "BTC/USDT")
