"""Binance Futures Demo Trading 客户端（只允许 demo 域名，永不触碰正式交易端点）。

凭据只从环境变量读取：``BINANCE_DEMO_API_KEY`` / ``BINANCE_DEMO_API_SECRET``。
缺凭据 → ``available() == False``，调用方显示 UNAVAILABLE，不伪造订单。
``fetch`` 可注入，测试零网络。

安全边界：
  - ``base_url`` 必须是 ``https://demo-fapi.binance.com``（或注入的假地址用于测试）；
  - 任何包含 ``fapi.binance.com``（非 demo）的地址直接拒绝；
  - 不实现提现/转账等资金接口。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal

DEMO_BASE = "https://demo-fapi.binance.com"
PROD_MARKERS = ("fapi.binance.com", "api.binance.com")


@dataclass
class DemoConfig:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = DEMO_BASE
    recv_window: int = 5000
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if any(m in self.base_url for m in PROD_MARKERS) and "demo-" not in self.base_url:
            raise ValueError("拒绝：Binance 正式端点不允许用于 Demo 执行")

    @classmethod
    def from_env(cls) -> "DemoConfig":
        return cls(api_key=os.environ.get("BINANCE_DEMO_API_KEY", ""),
                   api_secret=os.environ.get("BINANCE_DEMO_API_SECRET", ""))

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass
class DemoClient:
    config: DemoConfig
    fetch: object = None
    errors: list = field(default_factory=list)

    def available(self) -> bool:
        return self.config.available

    # ---------- 签名与请求 ----------
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        return hmac.new(self.config.api_secret.encode(), qs.encode(),
                        hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> dict:
        if not self.available():
            raise RuntimeError("DEMO_UNAVAILABLE: missing api key/secret")
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.config.recv_window
            params["signature"] = self._sign(params)
        url = self.config.base_url.rstrip("/") + path
        headers = {"X-MBX-APIKEY": self.config.api_key}
        if method == "GET":
            url = f"{url}?{urllib.parse.urlencode(params)}"
            body = None
        else:
            body = urllib.parse.urlencode(params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        fn = self.fetch or self._default_fetch
        try:
            payload = fn(method, url, headers, body, self.config.timeout_s)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            raise
        if isinstance(payload, dict) and payload.get("code") and payload.get("msg"):
            self.errors.append(f"{path}: {payload.get('code')} {payload.get('msg')}")
            raise RuntimeError(f"binance_error {payload.get('code')}: {payload.get('msg')}")
        return payload if isinstance(payload, (dict, list)) else {}

    @staticmethod
    def _default_fetch(method: str, url: str, headers: dict, body: bytes | None,
                       timeout: float) -> dict:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    # ---------- 查询 ----------
    def account(self) -> dict:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def balances(self) -> dict:
        acct = self.account()
        return {a["asset"]: {"wallet": a["walletBalance"], "available": a["availableBalance"],
                             "unrealized": a["unrealizedProfit"]}
                for a in acct.get("assets", []) if float(a.get("walletBalance", 0)) != 0}

    def positions(self, symbol: str = "BTCUSDT") -> list[dict]:
        rows = self._request("GET", "/fapi/v2/positionRisk",
                             {"symbol": symbol}, signed=True)
        return [p for p in rows if float(p.get("positionAmt", 0)) != 0]

    def open_orders(self, symbol: str = "BTCUSDT") -> list[dict]:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    # ---------- 下单 ----------
    def new_order(self, *, symbol: str, side: str, qty: Decimal,
                  order_type: str = "MARKET", stop_price: Decimal | None = None,
                  client_id: str = "", reduce_only: bool = False) -> dict:
        if side not in ("BUY", "SELL"):
            raise ValueError("side 必须是 BUY/SELL")
        params = {"symbol": symbol, "side": side, "type": order_type,
                  "quantity": str(qty), "newClientOrderId": client_id,
                  "newOrderRespType": "RESULT"}
        if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if stop_price is None:
                raise ValueError("条件单必须给 stop_price")
            params["stopPrice"] = str(stop_price)
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel(self, symbol: str, order_id: int) -> dict:
        return self._request("DELETE", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": order_id}, signed=True)

    def order_status(self, symbol: str, order_id: int) -> dict:
        return self._request("GET", "/fapi/v1/order",
                             {"symbol": symbol, "orderId": order_id}, signed=True)

    def reconcile(self, symbol: str = "BTCUSDT") -> dict:
        """重启对账：本地持仓/挂单 vs 交易所实际状态（只读）。"""
        return {"positions": self.positions(symbol),
                "open_orders": self.open_orders(symbol),
                "balances": self.balances()} if self.available() else {}