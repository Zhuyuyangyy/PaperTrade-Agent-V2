"""Binance Demo 客户端测试：签名、demo-only、下单参数、错误处理（零网络）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from research.execution.binance_demo import DemoClient, DemoConfig


def _client(fetch=None, base="https://demo-fapi.binance.com"):
    return DemoClient(DemoConfig(api_key="k", api_secret="s", base_url=base), fetch=fetch)


def test_rejects_production_endpoint() -> None:
    with pytest.raises(ValueError):
        DemoConfig(base_url="https://fapi.binance.com")


def test_unavailable_without_credentials() -> None:
    c = DemoClient(DemoConfig())
    assert c.available() is False
    with pytest.raises(RuntimeError, match="DEMO_UNAVAILABLE"):
        c.account()


def test_signed_request_has_signature() -> None:
    seen = {}

    def fake(method, url, headers, body, timeout):
        seen.update({"method": method, "url": url, "headers": headers})
        return {"totalWalletBalance": "1000"}

    c = _client(fake)
    c.account()
    assert "signature=" in seen["url"] and "timestamp=" in seen["url"]
    assert seen["headers"]["X-MBX-APIKEY"] == "k"


def test_new_order_parameters() -> None:
    seen = {}

    def fake(method, url, headers, body, timeout):
        seen["method"], seen["body"] = method, body.decode()
        return {"orderId": 1, "status": "FILLED"}

    c = _client(fake)
    r = c.new_order(symbol="BTCUSDT", side="SELL", qty=Decimal("0.001"),
                    stop_price=Decimal("59000"), order_type="STOP_MARKET",
                    client_id="oa-1", reduce_only=True)
    assert r["orderId"] == 1
    assert "STOP_MARKET" in seen["body"] and "reduceOnly=true" in seen["body"]
    assert "stopPrice=59000" in seen["body"]


def test_binance_error_raised_and_recorded() -> None:
    def fake(method, url, headers, body, timeout):
        return {"code": -2019, "msg": "Margin is insufficient."}

    c = _client(fake)
    with pytest.raises(RuntimeError, match="Margin"):
        c.new_order(symbol="BTCUSDT", side="BUY", qty=Decimal("1"))
    assert c.errors


def test_positions_filters_zero() -> None:
    def fake(method, url, headers, body, timeout):
        return [{"symbol": "BTCUSDT", "positionAmt": "0"},
                {"symbol": "BTCUSDT", "positionAmt": "0.002"}]

    c = _client(fake)
    assert len(c.positions()) == 1