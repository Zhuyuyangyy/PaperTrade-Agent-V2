"""Binance Futures 实时行情（WebSocket），交易引擎与图表共用同一份数据。

- 订阅 ``btcusdt@kline_1m``（逐 1m K 线更新，含未收盘 bar）；
- 维护 1m 序列并从它聚合 5m/15m/1h/4h，保证所有周期同步增量更新；
- 断线自动重连；每帧记录交易所事件时间与本地接收时间，供延迟统计；
- 只读公开行情，不涉及下单。
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from research.market.contracts import TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS

HOST = "fstream.binance.com"
PATH = "/stream?streams=btcusdt@kline_1m"
REST = "https://fapi.binance.com/fapi/v1/klines"
SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (feed; read-only)"}


@dataclass
class Tick:
    price: float
    event_ms: int
    recv_ms: int
    kline: dict
    source: str = "ws"

    @property
    def latency_ms(self) -> int:
        return max(0, self.recv_ms - self.event_ms)


@dataclass
class BinanceFeed:
    symbol: str = "btcusdt"
    rest_limit: int = 500
    _bars: list[dict] = field(default_factory=list)
    _base: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    last_tick: Tick | None = None
    connected: bool = False
    reconnects: int = 0
    errors: int = 0
    rest_polls: int = 0

    # ---------- 启动 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        threading.Thread(target=self._load_history, daemon=True).start()
        threading.Thread(target=self._rest_loop, daemon=True).start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _rest_loop(self) -> None:
        """WS 不可用时的降级：每 3 秒拉最近 2 根 1m，仍驱动价格与估值。"""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        n = 0
        while not self._stop.is_set():
            n += 1
            if n % 20 == 0:
                self._ensure_history()
            try:
                url = f"{REST}?symbol={self.symbol.upper()}&interval=1m&limit=2"
                with opener.open(urllib.request.Request(url, headers=UA), timeout=10) as resp:
                    rows = json.loads(resp.read().decode("utf-8"))
                now = int(time.time() * 1000)
                for r in rows:
                    bar = {"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                           "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                    with self._lock:
                        if self._bars and self._bars[-1]["time"] == bar["time"]:
                            self._bars[-1] = bar
                        elif not self._bars or bar["time"] > self._bars[-1]["time"]:
                            self._bars.append(bar)
                if rows:
                    last = {"close": float(rows[-1][4]), "t": int(rows[-1][0])}
                    self.last_tick = Tick(price=last["close"], event_ms=last["t"],
                                          recv_ms=now, kline={"time": last["t"] // 1000},
                                          source="rest")
                    self.rest_polls += 1
            except Exception:  # noqa: BLE001
                self.errors += 1
            time.sleep(3)

    def stop(self) -> None:
        self._stop.set()

    def _ensure_history(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for tf in SEC:
            if tf == "1m" or self._base.get(tf):
                continue
            try:
                url = (f"{REST}?symbol={self.symbol.upper()}&interval={tf}"
                       f"&limit={self.rest_limit}")
                with opener.open(urllib.request.Request(url, headers=UA), timeout=10) as resp:
                    rows = json.loads(resp.read().decode("utf-8"))
                self._base[tf] = [{"time": int(r[0]) // 1000, "open": float(r[1]),
                                   "high": float(r[2]), "low": float(r[3]),
                                   "close": float(r[4]), "volume": float(r[5])} for r in rows]
            except Exception:  # noqa: BLE001
                self.errors += 1

    def _load_history(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for tf in SEC:
            try:
                url = (f"{REST}?symbol={self.symbol.upper()}&interval={tf}"
                       f"&limit={self.rest_limit}")
                with opener.open(urllib.request.Request(url, headers=UA), timeout=20) as resp:
                    rows = json.loads(resp.read().decode("utf-8"))
                bars = [{"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                         "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                        for r in rows]
                if tf == "1m":
                    with self._lock:
                        self._bars = bars
                else:
                    self._base[tf] = bars
            except Exception:  # noqa: BLE001
                self.errors += 1

    # ---------- 连接 ----------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception:  # noqa: BLE001
                self.errors += 1
            self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            time.sleep(2)

    def _connect_once(self) -> None:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((HOST, 443), timeout=20)
        sock = ctx.wrap_socket(raw, server_hostname=HOST)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        req = (f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("handshake closed")
            head += chunk
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("handshake rejected")
        self.connected = True
        while not self._stop.is_set():
            opcode, payload = self._read_frame(sock)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                self._send_pong(sock, payload)
                continue
            if opcode != 0x1:
                continue
            msg = json.loads(payload.decode("utf-8"))
            data = msg.get("data") or {}
            self._handle(data)

    @staticmethod
    def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
        head = _recv_exact(sock, 2)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        return opcode, _recv_exact(sock, length)

    @staticmethod
    def _send_pong(sock: socket.socket, payload: bytes) -> None:
        mask = b"\x00\x00\x00\x00"
        sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + payload)

    # ---------- 数据处理 ----------
    def _handle(self, data: dict) -> None:
        k = data.get("k")
        if not k:
            return
        bar = {"time": int(k["t"]) // 1000, "open": float(k["o"]), "high": float(k["h"]),
               "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"])}
        with self._lock:
            if self._bars and self._bars[-1]["time"] == bar["time"]:
                self._bars[-1] = bar
            elif not self._bars or bar["time"] > self._bars[-1]["time"]:
                self._bars.append(bar)
            if len(self._bars) > self.rest_limit + 50:
                self._bars = self._bars[-self.rest_limit:]
        self.last_tick = Tick(price=bar["close"], event_ms=int(k["T"]),
                              recv_ms=int(time.time() * 1000), kline=bar, source="ws")

    # ---------- 读取 ----------
    def series(self, tf: str, limit: int = 500) -> list[dict]:
        tf_sec = SEC.get(tf, 60)
        with self._lock:
            bars = list(self._bars)
        if tf == "1m":
            return bars[-limit:]
        out: dict[int, dict] = {}
        for b in bars:
            t = b["time"] - b["time"] % tf_sec
            cur = out.get(t)
            if cur is None:
                out[t] = {"time": t, "open": b["open"], "high": b["high"],
                          "low": b["low"], "close": b["close"], "volume": b["volume"]}
            else:
                cur["high"] = max(cur["high"], b["high"])
                cur["low"] = min(cur["low"], b["low"])
                cur["close"] = b["close"]
                cur["volume"] += b["volume"]
        agg = sorted(out.values(), key=lambda x: x["time"])
        base = [b for b in self._base.get(tf, []) if not agg or b["time"] < agg[0]["time"]]
        return (base + agg)[-limit:]

    def base_series(self, tf: str) -> list[dict]:
        """纯 REST 历史（不混入 1m 聚合），用于 1h/4h 趋势判断。"""
        with self._lock:
            return list(self._base.get(tf, []))

    def latest(self, tf: str = "1m") -> dict | None:
        s = self.series(tf, 2)
        return s[-1] if s else None

    def price(self) -> float | None:
        with self._lock:
            return self._bars[-1]["close"] if self._bars else None

    def status(self) -> dict:
        t = self.last_tick
        mode = t.source if t else "none"
        return {"mode": mode, "connected": self.connected, "reconnects": self.reconnects,
                "rest_polls": self.rest_polls, "errors": self.errors,
                "last_event_ms": t.event_ms if t else None,
                "latency_ms": (t.latency_ms if (t and t.source == "ws") else None),
                "poll_ago_ms": (max(0, int(time.time() * 1000) - t.recv_ms)
                                if (t and t.source == "rest") else None),
                "price": t.price if t else None}


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf