"""Binance Futures Demo Trading 客户端（只允许 demo 主机，永不触碰正式交易端点）。

凭据只从环境变量读取：``BINANCE_DEMO_API_KEY`` / ``BINANCE_DEMO_API_SECRET``。
缺凭据 → ``available() == False``，调用方显示 UNAVAILABLE，不伪造订单。
``fetch`` 可注入，测试零网络。

安全边界（每次请求前都重新校验，不只构造时校验一次）：
  - scheme 必须 https，host **精确等于** ``demo-fapi.binance.com``；
  - 拒绝 userinfo / 非 443 端口 / path / query / fragment（防止 ``?x=@prod`` 之类绕过）；
  - 任何 ``fapi.binance.com``、``api.binance.com`` 都会因 host 不等而被拒；
  - 不实现提现/转账等资金接口。

条件单（2025-12-09 起 U 本位合约迁移到 Algo Order API）：
  - ``STOP`` / ``STOP_MARKET`` / ``TAKE_PROFIT`` / ``TAKE_PROFIT_MARKET`` /
    ``TRAILING_STOP_MARKET`` → ``POST /fapi/v1/algoOrder``（``algoType=CONDITIONAL``），
    旧 ``/fapi/v1/order`` 会返回 ``-4120 STOP_ORDER_SWITCH_ALGO``；
  - 查询 / 撤销 / 挂单列表走 ``GET|DELETE /fapi/v1/algoOrder``、``GET /fapi/v1/openAlgoOrders``；
  - 普通 ``MARKET`` / ``LIMIT`` 仍走 ``/fapi/v1/order``。
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

DEMO_HOST = "demo-fapi.binance.com"
DEMO_BASE = f"https://{DEMO_HOST}"

#: 必须走 Algo Order API 的条件单类型
CONDITIONAL_TYPES = frozenset({
    "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET",
})
#: 仍走 /fapi/v1/order 的普通单类型
PLAIN_TYPES = frozenset({"MARKET", "LIMIT"})
#: Binance 成功响应里可能出现的 code（撤销条件单返回 {"code":"200","msg":"success"}）
_OK_CODES = frozenset({None, 0, "0", 200, "200"})


def validate_base_url(url: str) -> str:
    """返回规范化 base_url；任何非 demo 主机都直接拒绝。"""
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme.lower() != "https":
        raise ValueError(f"拒绝：Demo 必须使用 https，收到 scheme={parts.scheme!r}")
    if parts.username or parts.password:
        raise ValueError("拒绝：URL 不允许携带 userinfo")
    host = (parts.hostname or "").lower()
    if host != DEMO_HOST:
        raise ValueError(f"拒绝：Demo 只允许主机 {DEMO_HOST!r}，收到 {host!r}")
    if parts.port not in (None, 443):
        raise ValueError(f"拒绝：Demo 只允许 443 端口，收到 {parts.port}")
    if parts.path not in ("", "/"):
        raise ValueError(f"拒绝：base_url 不允许带 path，收到 {parts.path!r}")
    if parts.query or parts.fragment:
        raise ValueError("拒绝：base_url 不允许带 query/fragment")
    return DEMO_BASE


@dataclass
class DemoConfig:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = DEMO_BASE
    recv_window: int = 5000
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        self.base_url = validate_base_url(self.base_url)

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
        # 每次请求前重新校验：config 被改写也绕不过去
        base = validate_base_url(self.config.base_url)
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.config.recv_window
            params["signature"] = self._sign(params)
        url = base + path
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
        if isinstance(payload, dict) and payload.get("code") not in _OK_CODES:
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

    def open_algo_orders(self, symbol: str = "BTCUSDT") -> list[dict]:
        return self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    # ---------- 下单 ----------
    def new_order(self, *, symbol: str, side: str, qty: Decimal,
                  order_type: str = "MARKET", price: Decimal | None = None,
                  stop_price: Decimal | None = None, client_id: str = "",
                  reduce_only: bool = False, close_position: bool = False,
                  time_in_force: str = "", working_type: str = "MARK_PRICE",
                  activate_price: Decimal | None = None,
                  callback_rate: Decimal | None = None) -> dict:
        """普通单走 /fapi/v1/order；条件单自动改走 /fapi/v1/algoOrder（-4120 规避）。

        返回值保持交易所原始结构：条件单含 ``algoId``，普通单含 ``orderId``。
        """
        if side not in ("BUY", "SELL"):
            raise ValueError("side 必须是 BUY/SELL")
        t = (order_type or "").upper()
        if t in CONDITIONAL_TYPES:
            return self.new_algo_order(symbol=symbol, side=side, qty=qty, order_type=t,
                                       stop_price=stop_price, price=price, client_id=client_id,
                                       reduce_only=reduce_only, close_position=close_position,
                                       working_type=working_type,
                                       activate_price=activate_price,
                                       callback_rate=callback_rate)
        if t not in PLAIN_TYPES:
            raise ValueError(f"不支持的订单类型: {order_type}")
        params = {"symbol": symbol, "side": side, "type": t,
                  "newClientOrderId": client_id, "newOrderRespType": "RESULT"}
        if t == "LIMIT":
            if price is None:
                raise ValueError("LIMIT 必须给 price")
            params["price"] = str(price)
            params["timeInForce"] = time_in_force or "GTC"
        else:
            params["quantity"] = str(qty)
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def new_algo_order(self, *, symbol: str, side: str, qty: Decimal,
                       order_type: str = "STOP_MARKET", stop_price: Decimal | None = None,
                       price: Decimal | None = None, client_id: str = "",
                       reduce_only: bool = False, close_position: bool = False,
                       working_type: str = "MARK_PRICE", activate_price: Decimal | None = None,
                       callback_rate: Decimal | None = None) -> dict:
        """条件单：``POST /fapi/v1/algoOrder``（``algoType=CONDITIONAL``）。

        参数名是 ``triggerPrice``（不是 stopPrice）、``clientAlgoId``（不是 newClientOrderId）。
        """
        t = (order_type or "").upper()
        if t not in CONDITIONAL_TYPES:
            raise ValueError(f"不是条件单类型: {order_type}")
        if stop_price is None and t != "TRAILING_STOP_MARKET":
            raise ValueError("条件单必须给 stop_price（triggerPrice）")
        params = {"algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": t,
                  "workingType": working_type, "newOrderRespType": "RESULT"}
        if t == "TRAILING_STOP_MARKET":
            if activate_price is None or callback_rate is None:
                raise ValueError("TRAILING_STOP_MARKET 需要 activate_price 与 callback_rate")
            params["activatePrice"] = str(activate_price)
            params["callbackRate"] = str(callback_rate)
        else:
            params["triggerPrice"] = str(stop_price)
        if close_position:
            params["closePosition"] = "true"     # 与 quantity / reduceOnly 互斥
        else:
            params["quantity"] = str(qty)
            if reduce_only:
                params["reduceOnly"] = "true"
            if t in ("STOP", "TAKE_PROFIT"):
                if price is None:
                    raise ValueError(f"{t} 必须给触发后的限价 price")
                params["price"] = str(price)
        if client_id:
            params["clientAlgoId"] = client_id
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    # ---------- 撤单 ----------
    def cancel(self, symbol: str, order_id: int | None = None, client_id: str = "") -> dict:
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_id:
            params["origClientOrderId"] = client_id
        return self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_algo(self, symbol: str, algo_id: int | None = None,
                    client_algo_id: str = "") -> dict:
        params = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = int(algo_id)
        if client_algo_id:
            params["clientAlgoId"] = client_algo_id
        return self._request("DELETE", "/fapi/v1/algoOrder", params, signed=True)

    def cancel_all_algo(self, symbol: str = "BTCUSDT") -> dict:
        return self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)

    # ---------- 订单状态 ----------
    def order_status(self, symbol: str, order_id: int | None = None,
                     client_id: str = "") -> dict:
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_id:
            params["origClientOrderId"] = client_id
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def algo_status(self, symbol: str, algo_id: int | None = None,
                    client_algo_id: str = "") -> dict:
        params = {"symbol": symbol}
        if algo_id is not None:
            params["algoId"] = int(algo_id)
        if client_algo_id:
            params["clientAlgoId"] = client_algo_id
        return self._request("GET", "/fapi/v1/algoOrder", params, signed=True)

    def reconcile(self, symbol: str = "BTCUSDT") -> dict:
        """重启对账的只读快照：持仓 / 普通挂单 / 条件单 / 余额。"""
        if not self.available():
            return {}
        return {"positions": self.positions(symbol),
                "open_orders": self.open_orders(symbol),
                "open_algo_orders": self.open_algo_orders(symbol),
                "balances": self.balances()}