"""The desk was losing data to its own impatience, not to the market.

A scan fires eight GETs back to back and Jupiter answers some with 429. The
old client raised on the first one, so every scan logged "数据源异常" and the
source was dropped -- which is why the organic rankings that feed the score
were missing from scan after scan, on every book, comparably or not.
"""
from __future__ import annotations

import email.message
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.memehunter as mh  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _http429(retry_after=None):
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("u", 429, "Too Many Requests", headers, None)


def test_a_429_is_retried_not_dropped(monkeypatch):
    calls = {"n": 0}

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http429()
        return _Resp({"ok": True})

    monkeypatch.setattr(mh.urllib.request, "urlopen", flaky)
    assert mh._get("https://example.invalid/x") == {"ok": True}
    assert calls["n"] == 2, "the source must be retried, not abandoned"


def test_retry_after_is_honoured(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(mh.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http429(7)
        return _Resp([])

    monkeypatch.setattr(mh.urllib.request, "urlopen", flaky)
    mh._get("https://example.invalid/x")
    # the first sleep is the throttle gap between calls; the retry wait must
    # also be in there, exactly as the server asked for it
    assert 7.0 in slept, f"honour the server's Retry-After, slept {slept}"


def test_a_persistent_429_still_raises_after_the_retries(monkeypatch):
    def always(request, timeout=None):
        raise _http429()

    monkeypatch.setattr(mh.urllib.request, "urlopen", always)
    monkeypatch.setattr(mh.time, "sleep", lambda s: None)
    with pytest.raises(mh.SourceError) as exc:
        mh._get("https://example.invalid/x")
    assert "429" in str(exc.value)
