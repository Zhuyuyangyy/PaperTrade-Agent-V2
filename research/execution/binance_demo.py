"""Binance Futures Demo Trading 客户端（只允许 demo 主机，永不触碰正式交易端点）。

凭据读取顺序：``BINANCE_DEMO_API_KEY`` / ``BINANCE_DEMO_API_SECRET`` 环境变量优先；
缺失时由 CLI 调用 ``load_demo_env()`` 从 ``<root>/.env`` → ``./.env`` → ``<repo>/.env``
补充（只补缺失项，只认这两个键，永不打印值）。
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
import pathlib
import time
import urllib.error
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


#: 允许从 .env 文件补充的键（白名单）。.env 里写别的键一律忽略，
#: 避免把 .env 当成任意环境变量注入。
#: DEEPSEEK_API_KEY 也放进来：决策模型和 Demo 凭据经常一起放在同一个 .env 里。
ENV_KEYS = ("BINANCE_DEMO_API_KEY", "BINANCE_DEMO_API_SECRET", "DEEPSEEK_API_KEY")


def load_env_file(path) -> list:
    """从单个 .env 文件补变量，**只补当前进程缺失的**，返回实际补上的键名。

    只认 ``ENV_KEYS`` 白名单；任何值都不会被打印或写日志。
    """
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return []
    filled = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        if key not in ENV_KEYS:
            continue
        val = val.strip().strip('"').strip("'")
        if not val or os.environ.get(key):
            continue  # 已有值就不覆盖：环境变量优先级高于文件
        os.environ[key] = val
        filled.append(key)
    return filled


def env_candidates(root=None) -> list:
    """按优先级返回 .env 候选路径（账本目录 → 当前目录 → 仓库根）。"""
    here = pathlib.Path(__file__).resolve().parents[2]
    out = []
    if root:
        out.append(pathlib.Path(root) / ".env")
    out.append(pathlib.Path.cwd() / ".env")
    out.append(here / ".env")
    seen, uniq = set(), []
    for p in out:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def load_demo_env(root=None) -> dict:
    """尝试所有候选 .env；返回 {"checked": [...], "filled": [...], "sources": [...]}。"""
    checked, filled, sources = [], [], []
    for p in env_candidates(root):
        checked.append(str(p))
        got = load_env_file(p)
        if got:
            filled.extend(got)
            sources.append(str(p))
    return {"checked": checked, "filled": sorted(set(filled)), "sources": sources}


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
    #: Binance 上限 60000；本机到 Demo 的 RTT 有 4s 级抖动，5000 会被 -1021 打回
    recv_window: int = 60000
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
    #: 服务器时间 - 本地时间（毫秒），由 sync_time() 校准
    time_offset_ms: int = 0

    def available(self) -> bool:
        return self.config.available

    # ---------- 签名与请求 ----------
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        return hmac.new(self.config.api_secret.encode(), qs.encode(),
                        hashlib.sha256).hexdigest()

    # ---------- 服务器时间 ----------
    def sync_time(self, samples: int = 3) -> int:
        """用 Demo 的 /fapi/v1/time 校准本地时钟偏差，返回 offset 毫秒。

        本地时钟慢/快几秒都会被 -1021 拒绝；签名用的 timestamp 必须是**服务器时间**。
        取样 3 次，取 **RTT 最小**的那次（NTP 思路）：RTT 越大，中点估计越偏。
        """
        best = None            # (rtt, offset)
        for _ in range(max(1, samples)):
            t0 = time.time() * 1000
            payload = self._request("GET", "/fapi/v1/time")
            t1 = time.time() * 1000
            srv = int(payload.get("serverTime") or 0)
            if srv <= 0:
                raise RuntimeError(f"time: 响应缺少 serverTime（{str(payload)[:120]}）")
            rtt = t1 - t0
            offset = srv - (t0 + t1) / 2
            if best is None or rtt < best[0]:
                best = (rtt, offset)
        self.time_offset_ms = int(round(best[1]))
        return self.time_offset_ms

    def server_now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def _safe_sync_time(self) -> None:
        try:
            self.sync_time()
        except Exception as exc:  # noqa: BLE001 - 校准失败不掩盖原始错误
            self.errors.append(f"sync_time: {type(exc).__name__}: {exc}")

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> dict:
        if not self.available():
            raise RuntimeError("DEMO_UNAVAILABLE: missing api key/secret")
        # 每次请求前重新校验：config 被改写也绕不过去
        base = validate_base_url(self.config.base_url)
        fn = self.fetch or self._default_fetch
        last: Exception | None = None
        for attempt in (0, 1):
            p = dict(params or {})
            if signed:
                p["timestamp"] = self.server_now_ms()
                p["recvWindow"] = self.config.recv_window
                p["signature"] = self._sign(p)
            headers = {"X-MBX-APIKEY": self.config.api_key}
            if method == "GET":
                url = f"{base}{path}?{urllib.parse.urlencode(p)}"
                body = None
            else:
                url = base + path
                body = urllib.parse.urlencode(p).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            try:
                payload = fn(method, url, headers, body, self.config.timeout_s)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if signed and attempt == 0 and "-1021" in str(exc):
                    self.errors.append(f"{path}: -1021 时间戳越界，校准后重试一次")
                    self._safe_sync_time()
                    continue
                self.errors.append(f"{path}: {type(exc).__name__}: {exc}")
                raise
            code = payload.get("code") if isinstance(payload, dict) else None
            if code not in _OK_CODES:
                if signed and attempt == 0 and str(code) == "-1021":
                    self.errors.append(f"{path}: -1021 时间戳越界，校准后重试一次")
                    self._safe_sync_time()
                    continue
                self.errors.append(f"{path}: {code} {payload.get('msg')}")
                raise RuntimeError(f"binance_error {code}: {payload.get('msg')}")
            return payload if isinstance(payload, (dict, list)) else {}
        raise last if last is not None else RuntimeError(f"{path}: unknown error")

    @staticmethod
    def _default_fetch(method: str, url: str, headers: dict, body: bytes | None,
                       timeout: float) -> dict:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            # Binance 的错误码/原因全在响应体里；不读 body 就只剩 "HTTP Error 400"，
            # 无法判断是签名错、权限不足还是参数错。这里必须把 body 捞出来。
            raw = err.read() if hasattr(err, "read") else b""
            text = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(text or "{}")
            except ValueError:
                payload = {}
            code = payload.get("code", f"HTTP_{err.code}")
            msg = payload.get("msg") or text[:200] or err.reason
            raise RuntimeError(f"HTTP {err.code} {code}: {msg}") from None
        return json.loads(raw.decode("utf-8", errors="replace"))

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