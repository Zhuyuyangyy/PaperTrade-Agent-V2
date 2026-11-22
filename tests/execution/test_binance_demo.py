"""Binance Demo 客户端测试：demo-only 主机校验、签名、条件单走 algoOrder、错误处理（零网络）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from research.execution.binance_demo import (
    CONDITIONAL_TYPES,
    DEMO_BASE,
    DemoClient,
    DemoConfig,
)


def _client(fetch=None, base=DEMO_BASE):
    return DemoClient(DemoConfig(api_key="k", api_secret="s", base_url=base), fetch=fetch)


# ---------- 主机隔离 ----------
@pytest.mark.parametrize("url", [
    "https://fapi.binance.com",
    "https://api.binance.com",
    "https://demo-fapi.binance.com.evil.com",
    "https://evil.com/demo-fapi.binance.com",
    "https://demo-fapi.binance.com:8443",
    "http://demo-fapi.binance.com",
    "https://demo-fapi.binance.com/fapi",
    "https://demo-fapi.binance.com?x=1",
    "https://user:pw@demo-fapi.binance.com",
    "https://demo-fapi.binance.com@fapi.binance.com",
])
def test_rejects_non_demo_hosts(url: str) -> None:
    with pytest.raises(ValueError):
        DemoConfig(base_url=url)


def test_base_url_rewritten_even_if_config_is_tampered() -> None:
    """构造后直接把 config.base_url 改成生产地址，请求也必须被拒绝。"""
    c = _client(lambda *a: {})
    c.config.base_url = "https://fapi.binance.com"
    with pytest.raises(ValueError):
        c.account()


def test_unavailable_without_credentials() -> None:
    c = DemoClient(DemoConfig())
    assert c.available() is False
    with pytest.raises(RuntimeError, match="DEMO_UNAVAILABLE"):
        c.account()


# ---------- 签名 ----------
def test_signed_request_has_signature() -> None:
    seen = {}

    def fake(method, url, headers, body, timeout):
        seen.update({"method": method, "url": url, "headers": headers})
        return {"totalWalletBalance": "1000"}

    c = _client(fake)
    c.account()
    assert "signature=" in seen["url"] and "timestamp=" in seen["url"]
    assert seen["headers"]["X-MBX-APIKEY"] == "k"


# ---------- 普通单 ----------
def test_plain_market_order_goes_to_order_endpoint() -> None:
    seen = {}

    def fake(method, url, headers, body, timeout):
        seen["url"] = url
        seen["body"] = body.decode()
        return {"orderId": 7, "status": "FILLED", "executedQty": "0.002"}

    c = _client(fake)
    r = c.new_order(symbol="BTCUSDT", side="BUY", qty=Decimal("0.002"), order_type="MARKET",
                    client_id="oa-1")
    assert r["orderId"] == 7
    assert seen["url"].endswith("/fapi/v1/order")
    assert "type=MARKET" in seen["body"] and "quantity=0.002" in seen["body"]


def test_limit_order_requires_price_and_time_in_force() -> None:
    seen = {}

    def fake(method, url, headers, body, timeout):
        seen["body"] = body.decode()
        return {"orderId": 8, "status": "NEW"}

    c = _client(fake)
    c.new_order(symbol="BTCUSDT", side="BUY", qty=Decimal("0.002"), order_type="LIMIT",
                price=Decimal("60000"))
    assert "price=60000" in seen["body"] and "timeInForce=GTC" in seen["body"]
    with pytest.raises(ValueError):
        c.new_order(symbol="BTCUSDT", side="BUY", qty=Decimal("1"), order_type="LIMIT")


# ---------- 条件单 → algoOrder ----------
@pytest.mark.parametrize("otype", sorted(CONDITIONAL_TYPES))
def test_conditional_types_are_routed_to_algo_endpoint(otype: str) -> None:
    seen = {}

    def fake(method, url, headers, body, timeout):
        seen["url"] = url
        seen["body"] = body.decode()
        return {"algoId": 2146760, "algoStatus": "NEW"}

    c = _client(fake)
    kw = {"symbol": "BTCUSDT", "side": "SELL", "qty": Decimal("0.002"), "order_type": otype,
          "client_id": "oa-1-s", "reduce_only": True}
    if otype == "TRAILING_STOP_MARKET":
        kw.pop("reduce_only")
        r = c.new_order(**kw, stop_price=Decimal("1"),
                        activate_price=Decimal("59000"), callback_rate=Decimal("1"))
    else:
        r = c.new_order(**kw, stop_price=Decimal("59000"),
                        price=Decimal("58900") if otype in ("STOP", "TAKE_PROFIT") else None)
    assert r["algoId"] == 2146760
    assert seen["url"].endswith("/fapi/v1/algoOrder"), seen["url"]
    assert "algoType=CONDITIONAL" in seen["body"]
    if otype == "TRAILING_STOP_MARKET":
        # 追踪止损用 activatePrice + callbackRate，本来就不用 triggerPrice
        assert "activatePrice=59000" in seen["body"]
        assert "callbackRate=1" in seen["body"]
        assert "triggerPrice" not in seen["body"]
        return
    assert "triggerPrice=" in seen["body"]
    if otype in ("STOP", "TAKE_PROFIT"):
        assert "price=58900" in seen["body"]
    assert "stopPrice" not in seen["body"], "旧接口才用 stopPrice"
    assert "clientAlgoId=oa-1-s" in seen["body"]


def test_stop_market_never_hits_legacy_order_endpoint() -> None:
    """回归：-4120 STOP_ORDER_SWITCH_ALGO 的根因就是把条件单发给 /fapi/v1/order。"""
    urls = []

    def fake(method, url, headers, body, timeout):
        urls.append(url)
        return {"algoId": 1, "algoStatus": "NEW"}

    c = _client(fake)
    c.new_order(symbol="BTCUSDT", side="SELL", qty=Decimal("0.001"),
                order_type="STOP_MARKET", stop_price=Decimal("59000"))
    assert len(urls) == 1 and "/fapi/v1/algoOrder" in urls[0]
    assert "/fapi/v1/order?" not in urls[0]


def test_algo_cancel_and_query() -> None:
    seen = []

    def fake(method, url, headers, body, timeout):
        seen.append((method, url))
        return {"algoId": 2146760, "code": "200", "msg": "success"}

    c = _client(fake)
    assert c.cancel_algo("BTCUSDT", algo_id=2146760)["code"] == "200"
    c.algo_status("BTCUSDT", algo_id=2146760)
    c.open_algo_orders("BTCUSDT")
    assert seen[0][0] == "DELETE" and "/fapi/v1/algoOrder" in seen[0][1]
    assert seen[1][0] == "GET" and "/fapi/v1/algoOrder" in seen[1][1]
    assert "/fapi/v1/openAlgoOrders" in seen[2][1]


def test_algo_cancel_success_code_is_not_an_error() -> None:
    """撤销条件单返回 {"code":"200"}：旧实现会把成功当异常抛出来。"""
    c = _client(lambda *a: {"algoId": 1, "code": "200", "msg": "success"})
    assert c.cancel_algo("BTCUSDT", algo_id=1)["msg"] == "success"
    assert not c.errors


# ---------- 错误处理 ----------
def test_binance_error_raised_and_recorded() -> None:
    def fake(method, url, headers, body, timeout):
        return {"code": -4120, "msg": "Order type not supported for this endpoint."}

    c = _client(fake)
    with pytest.raises(RuntimeError, match="not supported"):
        c.new_order(symbol="BTCUSDT", side="SELL", qty=Decimal("0.001"),
                    order_type="STOP_MARKET", stop_price=Decimal("59000"))
    assert c.errors


def test_positions_filters_zero() -> None:
    def fake(method, url, headers, body, timeout):
        return [{"symbol": "BTCUSDT", "positionAmt": "0"},
                {"symbol": "BTCUSDT", "positionAmt": "0.002"}]

    c = _client(fake)
    assert len(c.positions()) == 1

# ---------- .env 补凭据 ----------
def test_env_file_only_fills_missing_and_whitelisted(tmp_path, monkeypatch) -> None:
    from research.execution.binance_demo import load_env_file

    p = tmp_path / ".env"
    p.write_text(
        "# 注释行\n"
        "BINANCE_DEMO_API_KEY=from_file\n"
        "BINANCE_DEMO_API_SECRET='secret_from_file'\n"
        "PATH=/evil\n"           # 白名单外，必须被忽略
        "export BINANCE_DEMO_API_SECRET=NO_OVERRIDE\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BINANCE_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_DEMO_API_SECRET", raising=False)
    filled = load_env_file(p)
    import os
    assert filled == ["BINANCE_DEMO_API_KEY", "BINANCE_DEMO_API_SECRET"]
    assert os.environ["BINANCE_DEMO_API_KEY"] == "from_file"
    # 引号被剥掉；第二次出现因为是 setdefault 语义不会覆盖
    assert os.environ["BINANCE_DEMO_API_SECRET"] == "secret_from_file"
    assert os.environ["PATH"] != "/evil"


def test_env_var_wins_over_file(tmp_path, monkeypatch) -> None:
    from research.execution.binance_demo import load_env_file

    p = tmp_path / ".env"
    p.write_text("BINANCE_DEMO_API_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "from_env")
    assert load_env_file(p) == []
    import os
    assert os.environ["BINANCE_DEMO_API_KEY"] == "from_env"


def test_missing_env_file_is_silent(tmp_path) -> None:
    from research.execution.binance_demo import load_env_file

    assert load_env_file(tmp_path / "nope.env") == []


def test_env_candidates_prioritise_root(tmp_path) -> None:
    from research.execution.binance_demo import env_candidates

    cands = [str(p) for p in env_candidates(tmp_path)]
    assert cands[0] == str(tmp_path / ".env")
    assert len(cands) == len(set(c.lower() for c in cands))
