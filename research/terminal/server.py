"""OpenAlpha Trading Terminal V1：只读终端（HTTP + WebSocket + Lightweight Charts）。

数据来源：
  - K 线：Binance 公开 REST（只读）；
  - 三本账：真实 SQLite（只读连接），不伪造任何成交；
  - 决策理由：decisions.jsonl / deepseek_decisions.jsonl 的最后记录。

默认只监听 127.0.0.1；绑定非本机地址必须提供 --token（Bearer 或 ?token=）。
交易逻辑不在这里，终端只展示。
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from research.market.contracts import TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.runtime.heartbeat import read as read_heartbeat
from .feed import BinanceFeed
from research.runtime.metrics import summarize_books

FAPI = "https://fapi.binance.com/fapi/v1/klines"
INTERVALS = {"1m": 60_000, "5m": TF_5M_MS, "15m": TF_15M_MS,
             "1h": TF_1H_MS, "4h": TF_4H_MS}
UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (terminal; read-only)"}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass
class TerminalConfig:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8787
    token: str = ""
    symbol: str = "BTCUSDT"


@dataclass
class KlineCache:
    ttl_s: float = 8.0
    _data: dict = field(default_factory=dict)
    _ts: dict = field(default_factory=dict)

    def get(self, interval: str, limit: int = 500) -> list[dict]:
        now = time.time()
        key = (interval, limit)
        if key in self._data and now - self._ts.get(key, 0) < self.ttl_s:
            return self._data[key]
        url = f"{FAPI}?symbol=BTCUSDT&interval={interval}&limit={limit}"
        req = urllib.request.Request(url, headers=UA)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        out = [{"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
               for r in rows]
        self._data[key] = out
        self._ts[key] = now
        return out


def _jsonl_last(path: Path, n: int = 1) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_state(cfg: TerminalConfig, prices=None) -> dict:
    root = cfg.root
    hb = read_heartbeat(root / "state" / "heartbeat.json")
    if hb is None:
        v2 = root / "state" / "heartbeat_v2.json"
        if v2.is_file():
            try:
                from research.runtime.heartbeat import Heartbeat

                data = json.loads(v2.read_text(encoding="utf-8"))
                ts = int(data.get("updated_at_ms") or 0)
                hb = Heartbeat(updated_at_ms=ts, last_ok_ms=ts, rounds=0, pid=0)
            except (ValueError, OSError):
                hb = None
    books = summarize_books(root, prices=prices)
    decisions = _jsonl_last(root / "data" / "decisions.jsonl")
    ds = _jsonl_last(root / "data" / "deepseek_decisions.jsonl")
    rules_last = None
    v2_rules = _jsonl_last(root / "rules_5m_decisions.jsonl")
    if not decisions and v2_rules:
        d = v2_rules[-1].get("decision") or {}
        rules_last = {"as_of_ms": v2_rules[-1].get("as_of_ms"),
                      "decision": d.get("choice"), "strategy": d.get("provider"),
                      "reason": d.get("rationale"), "shadow": True}
    if decisions:
        sel = decisions[-1].get("selection") or {}
        intent = sel.get("intent") or {}
        rules_last = {"as_of_ms": decisions[-1].get("round_ts_ms"),
                      "decision": sel.get("decision"),
                      "strategy": intent.get("strategy_id"),
                      "reason": sel.get("hold_reason") or intent.get("reason") or "",
                      "shadow": intent.get("shadow")}
    v2_ds = _jsonl_last(root / "deepseek_5m_decisions.jsonl")
    if not ds and v2_ds:
        d = v2_ds[-1].get("decision") or {}
        ds = [{"as_of_ms": v2_ds[-1].get("as_of_ms"), "choice": d.get("choice"),
               "status": d.get("status"), "confidence": d.get("confidence"),
               "rationale": d.get("rationale"), "version": d.get("version")}]
    deepseek_last = None
    if ds:
        d = ds[-1]
        latency = None
        prov = _jsonl_last(root / "data" / "providers.jsonl")
        if prov:
            for item in (prov[-1].get("decisions") or []):
                if item.get("provider") == "deepseek":
                    latency = item.get("latency_ms")
        deepseek_last = {"as_of_ms": d.get("as_of_ms"), "choice": d.get("choice"),
                         "status": d.get("status"), "confidence": d.get("confidence"),
                         "rationale": d.get("rationale"), "version": d.get("version"),
                         "latency_ms": latency}
    return {
        "books": books,
        "heartbeat": hb.to_dict() if hb else None,
        "rules_last": rules_last,
        "deepseek_last": deepseek_last,
        "server_time_ms": int(time.time() * 1000),
        "root": str(root),
    }


def _reasons(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (root / "data" / "decisions.jsonl").read_text(encoding="utf-8").splitlines() \
            if (root / "data" / "decisions.jsonl").is_file() else []:
        try:
            e = json.loads(line)
            intent = (e.get("selection") or {}).get("intent") or {}
            if intent.get("intent_id"):
                out[intent["intent_id"]] = intent.get("reason", "")
        except ValueError:
            continue
    for d in (json.loads(l) for l in (root / "data" / "deepseek_decisions.jsonl")
              .read_text(encoding="utf-8").splitlines()) if \
            (root / "data" / "deepseek_decisions.jsonl").is_file() else []:
        fill = (d or {}).get("fill") or {}
        if fill.get("intent_id"):
            out[fill["intent_id"]] = (d.get("rationale") or "")[:160]
    return out


def read_marks(cfg: TerminalConfig) -> dict:
    import sqlite3

    root = cfg.root
    reasons = _reasons(root)
    markers: list[dict] = []
    details: dict[str, dict] = {}
    for book in ("shadow", "deepseek_shadow", "verified"):
        path = root / f"{book}.sqlite"
        if not path.is_file():
            continue
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT o.intent_id, o.ts_ms, o.side, o.price, o.qty, o.strategy_id,"
                " p.exit_ts, p.exit_price, p.exit_reason, p.pnl, p.status"
                " FROM orders o LEFT JOIN positions p ON p.intent_id = o.intent_id"
                " ORDER BY o.ts_ms"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            iid = f"{book}:{r['intent_id']}"
            side = r["side"]
            t_open = int(r["ts_ms"]) // 1000
            markers.append({"time": t_open, "position": "belowBar" if side == "LONG" else "aboveBar",
                            "shape": "arrowUp" if side == "LONG" else "arrowDown",
                            "color": ("#22d3ee" if side == "LONG" else "#a855f7") if book == "deepseek_shadow" else ("#16a34a" if side == "LONG" else "#dc2626"),
                            "text": "▲" if side == "LONG" else "▼", "id": iid})
            details[iid] = {"book": book, "side": side, "strategy": r["strategy_id"],
                            "price": r["price"], "qty": r["qty"],
                            "reason": reasons.get(r["intent_id"], ""), "status": r["status"]}
            if r["exit_ts"]:
                t_exit = int(r["exit_ts"]) // 1000
                pnl = float(r["pnl"] or 0)
                markers.append({"time": t_exit, "position": "aboveBar", "shape": "circle",
                                "color": ("#22d3ee" if pnl > 0 else "#a855f7") if book == "deepseek_shadow" else ("#16a34a" if pnl > 0 else "#dc2626"),
                                "text": ("TP" if pnl > 0 else "SL") if r['exit_reason'] == "TAKE_PROFIT" else ("SL" if r['exit_reason'] == "STOP_LOSS" else "EX"), "id": iid})
                details[iid].update({"exit_reason": r["exit_reason"], "pnl": r["pnl"],
                                     "exit_price": r["exit_price"]})
    markers.sort(key=lambda m: (m["time"], m["id"]))
    return {"markers": markers, "details": details}


def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _ws_frame(text: str) -> bytes:
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        return bytes([0x81, n]) + data
    if n < 65536:
        return bytes([0x81, 126]) + struct.pack(">H", n) + data
    return bytes([0x81, 127]) + struct.pack(">Q", n) + data


class Hub:
    def __init__(self) -> None:
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()

    def add(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.add(sock)

    def remove(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.discard(sock)

    def broadcast(self, text: str) -> None:
        frame = _ws_frame(text)
        with self._lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(frame)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._clients.discard(c)


class Terminal:
    def __init__(self, cfg: TerminalConfig):
        self.cfg = cfg
        self.hub = Hub()
        self.klines = KlineCache()
        self.feed = BinanceFeed(symbol="btcusdt")
        self.feed.start()
        self._stop = threading.Event()

    def _prices(self):
        from decimal import Decimal

        p = self.feed.price()
        return {"BTC/USDT": Decimal(str(p))} if p else None

    # ---------- 广播 ----------
    def snapshot(self) -> str:
        try:
            state = read_state(self.cfg, self._prices())
            marks = read_marks(self.cfg)
            klines = {tf: self.feed.latest(tf) for tf in ("1m", "5m", "15m", "1h", "4h")}
            return json.dumps({"type": "tick", "state": state, "klines": klines,
                               "marks": marks, "feed": self.feed.status()},
                              ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    def run_broadcast(self) -> None:
        while not self._stop.is_set():
            self.hub.broadcast(self.snapshot())
            time.sleep(10)

    # ---------- HTTP ----------
    def handler(self):
        cfg, hub, klines, feed, term = self.cfg, self.hub, self.klines, self.feed, self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D401 - 静默
                return

            def _authed(self, qs: dict) -> bool:
                if not cfg.token:
                    return True
                if qs.get("token", [""])[0] == cfg.token:
                    return True
                return self.headers.get("Authorization", "") == f"Bearer {cfg.token}"

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if not self._authed(qs):
                    self._send(401, b'{"error":"unauthorized"}', "application/json")
                    return
                try:
                    if parsed.path == "/":
                        self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    elif parsed.path == "/static/chart.js":
                        data = (Path(__file__).parent / "static" /
                                "lightweight-charts.standalone.production.js").read_bytes()
                        self._send(200, data, "application/javascript")
                    elif parsed.path == "/api/state":
                        body = json.dumps({**read_state(cfg, term._prices()), "feed": feed.status()}, ensure_ascii=False).encode()
                        self._send(200, body, "application/json")
                    elif parsed.path == "/api/marks":
                        body = json.dumps(read_marks(cfg), ensure_ascii=False).encode()
                        self._send(200, body, "application/json")
                    elif parsed.path == "/api/klines":
                        tf = qs.get("tf", ["1m"])[0]
                        if tf not in INTERVALS:
                            self._send(400, b'{"error":"bad tf"}', "application/json")
                            return
                        body = json.dumps(feed.series(tf, 500) or klines.get(tf, 500), ensure_ascii=False).encode()
                        self._send(200, body, "application/json")
                    elif parsed.path == "/ws":
                        self._ws(parsed)
                    else:
                        self._send(404, b"not found", "text/plain")
                except Exception as exc:  # noqa: BLE001
                    self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                               "application/json")

            def _ws(self, parsed) -> None:
                key = self.headers.get("Sec-WebSocket-Key", "")
                if not key:
                    self._send(400, b"no ws key", "text/plain")
                    return
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
                self.end_headers()
                sock = self.connection
                hub.add(sock)
                try:
                    sock.sendall(_ws_frame(self_snapshot(cfg, term._prices())))
                    while True:
                        head = sock.recv(2)
                        if not head:
                            break
                        opcode = head[0] & 0x0F
                        if opcode == 0x8:
                            break
                        length = head[1] & 0x7F
                        if length == 126:
                            sock.recv(2)
                        elif length == 127:
                            sock.recv(8)
                        mask = sock.recv(4) if head[1] & 0x80 else b""
                        if length:
                            sock.recv(length)
                        if mask is None:
                            break
                except OSError:
                    pass
                finally:
                    hub.remove(sock)
                    try:
                        sock.close()
                    except OSError:
                        pass

        return Handler

    def serve(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), self.handler())
        threading.Thread(target=self.run_broadcast, daemon=True).start()
        return server


def self_snapshot(cfg: TerminalConfig) -> str:
    return json.dumps({"type": "tick", "state": read_state(cfg),
                       "marks": read_marks(cfg)}, ensure_ascii=False)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>OpenAlpha Trading Terminal</title>
<style>
:root{--bg:#0b0f16;--panel:#121826;--line:#1f2937;--text:#e5e7eb;--muted:#9ca3af;--up:#16a34a;--down:#dc2626;--accent:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0} .pill{padding:2px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
main{display:grid;grid-template-columns:1fr 360px;gap:12px;padding:12px;height:calc(100vh - 84px)}
#chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;min-height:420px}
aside{display:flex;flex-direction:column;gap:10px;overflow:auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px}
.card h2{font-size:12px;color:var(--muted);margin:0 0 8px;font-weight:600;letter-spacing:.04em}
.row{display:flex;justify-content:space-between;padding:2px 0}
.k{color:var(--muted)} .v{font-variant-numeric:tabular-nums}
.up{color:var(--up)} .down{color:var(--down)}
.tf{display:flex;gap:6px;margin-left:auto}
.tf button{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:6px;padding:2px 8px;cursor:pointer}
.tf button.on{color:var(--text);border-color:var(--accent)}
#status{display:flex;gap:14px;padding:6px 16px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center}
#modal.on{display:flex} #modal .box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;max-width:520px}
#modal h3{margin:0 0 8px;font-size:14px} #modal p{margin:4px 0;color:var(--muted)}
footer{color:var(--muted);font-size:11px;text-align:center;padding:4px}
</style></head><body>
<header>
  <h1>OpenAlpha Trading Terminal</h1><span class="pill" id="symbol">BTCUSDT</span>
  <span class="pill" id="mode">PAPER ONLY</span>
  <div class="tf" id="tfs"></div>
</header>
<main>
  <div style="display:flex;flex-direction:column;gap:8px;min-width:0">
    <div id="chart" style="flex:1"></div>
    <footer>TradingView Lightweight Charts™ © TradingView</footer>
  </div>
  <aside>
    <div class="card"><h2>ACCOUNTS (1000 USDT each)</h2><div id="books"></div></div>
    <div class="card"><h2>DEEPSEEK DESK</h2><div id="ds"></div></div>
    <div class="card"><h2>RULES</h2><div id="rules"></div></div>
    <div class="card"><h2>LAST EVENTS</h2><div id="events"></div></div>
  </aside>
</main>
<div id="status"><span id="st-proc">process: ?</span><span id="st-lat">latency: ?</span>
<span id="st-ws">ws: connecting</span><span id="st-hb">heartbeat: ?</span></div>
<div id="modal"><div class="box"><h3 id="m-title"></h3><div id="m-body"></div></div></div>
<script src="/static/chart.js"></script>
<script>window.onerror=(m)=>{if(/ResizeObserver/.test(m))return;document.getElementById('st-ws').textContent='JS ERROR: '+m;};function poll(){fetch('/api/state').then(r=>r.json()).then(s=>{drawBooks(s.books||{});drawDS(s.deepseek_last);drawRules(s.rules_last);setStatus(s);document.getElementById('mode').textContent=(/forward|replay/i.test(s.root||'')?'REPLAY':'LIVE');});fetch('/api/klines?tf='+tf).then(r=>r.json()).then(d=>{if(d&&d.length&&series){series.setData(d);}});}function setStatus(s){const hb=s.heartbeat||{};document.getElementById('st-proc').textContent='process: '+(hb.pid||'?')+(hb.halted?' HALTED':'');document.getElementById('st-hb').textContent='heartbeat: '+(hb.last_ok_ms?new Date(hb.last_ok_ms).toLocaleTimeString():'?');document.getElementById('st-lat').textContent='latency: '+(hb.last_ok_ms?((s.server_time_ms-hb.last_ok_ms)/1000).toFixed(0)+'s':'?');if(s.feed){const f=s.feed;document.getElementById('st-ws').textContent='feed: '+f.mode+(f.latency_ms!=null?(' '+f.latency_ms+'ms'):(f.poll_ago_ms!=null?(' poll '+f.poll_ago_ms+'ms'):''));}}
const TF=['1m','5m','15m','1h','4h'];const TF_SEC={'1m':60,'5m':300,'15m':900,'1h':3600,'4h':14400};let tf='1h',chart,series,marksData={details:{}},lastKline=null,focused=false;
const fmt=(x)=>x==null?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
const pct=(x)=>x==null?'—':(Number(x)*100).toFixed(2)+'%';
function drawBooks(b){
  const rows=Object.keys(b||{}).filter(k=>k!=='root').map(k=>[k,k]);
  document.getElementById('books').innerHTML=rows.map(([label,key])=>{
    const x=b[key];if(!x)return `<div class="row"><span class="k">${label}</span><span class="v">—</span></div>`;
    const eq=Number(x.equity), cash=Number(x.cash), margin=Number(x.margin_used||0);
    const unr=Number(x.unrealized_pnl||0), pnl=Number(x.realized_pnl)||0;
    return `<div class="row"><span class="k">${label} equity</span><span class="v"><b>${fmt(eq)}</b></span></div>
      <div class="row"><span class="k">cash / margin</span><span class="v">${fmt(cash)} / ${fmt(margin)}</span></div>
      <div class="row"><span class="k">unrealized</span><span class="v ${unr>=0?'up':'down'}">${unr>=0?'+':''}${fmt(unr)}</span></div>
      <div class="row"><span class="k">realized</span><span class="v ${pnl>=0?'up':'down'}">${pnl>=0?'+':''}${fmt(pnl)}</span></div>
      <div class="row"><span class="k">win / dd</span><span class="v">${pct(x.win_rate)} / ${pct(x.max_drawdown)}</span></div>
      <div style="height:6px"></div>`;
  }).join('');
}
function drawDS(d){
  const el=document.getElementById('ds');
  if(!d){el.innerHTML='<span class="k">no decision yet</span>';return}
  el.innerHTML=`<div class="row"><span class="k">choice</span><span class="v ${d.choice==='LONG'?'up':d.choice==='SHORT'?'down':''}">${d.choice}</span></div>
  <div class="row"><span class="k">status / version</span><span class="v">${d.status} / ${d.version}</span></div>
  <div class="row"><span class="k">confidence</span><span class="v">${d.confidence}</span></div>
  <div class="row"><span class="k">inference</span><span class="v">${d.latency_ms==null?'-':d.latency_ms+'ms'}</span></div>
  <div class="row"><span class="k">reason</span><span class="v" style="max-width:210px;text-align:right">${d.rationale||''}</span></div>`;
}
function drawRules(r){document.getElementById('rules').innerHTML=r?`<div class="row"><span class="k">decision</span><span class="v">${r.decision}</span></div><div class="row"><span class="k">strategy</span><span class="v">${r.strategy||'—'}</span></div><div class="row"><span class="k">reason</span><span class="v" style="max-width:210px;text-align:right">${r.reason||''}</span></div>`:'<span class="k">no decision yet</span>'}
function drawEvents(m){
  const recent=(m.markers||[]).slice(-6).reverse();
  document.getElementById('events').innerHTML=recent.map(x=>`<div class="row"><span class="k">${new Date(x.time*1000).toLocaleTimeString()}</span><span class="v">${x.text}</span></div>`).join('')||'<span class="k">no fills yet</span>';
}
function applyMarks(m){
  marksData=m||{details:{}};
  if(series){const sec=TF_SEC[tf]||60;const seen=new Set();const arr=(m.markers||[]).map(x=>{const tt=Math.floor(x.time/sec)*sec;const k=tt+'|'+x.shape;if(seen.has(k))return null;seen.add(k);return {time:tt,position:x.position,shape:x.shape,color:x.color,text:x.text};}).filter(Boolean);series.setMarkers(arr);}
  drawEvents(m);
  if(!focused){const ms=m.markers||[];if(ms.length){focused=true;const ts=ms.map(x=>x.time);chart.timeScale().setVisibleRange({from:Math.min(...ts)-86400,to:Math.max(...ts)+86400});}}
}
function setTf(next){
  tf=next;focused=false;document.querySelectorAll('#tfs button').forEach(b=>b.classList.toggle('on',b.dataset.tf===tf));
  fetch(`/api/klines?tf=${tf}`).then(r=>r.json()).then(data=>{series.setData(data);chart.timeScale().fitContent()});
  fetch('/api/marks').then(r=>r.json()).then(applyMarks);
}
function init(){
  const el=document.getElementById('chart');
  chart=LightweightCharts.createChart(el,{layout:{background:{color:'#121826'},textColor:'#9ca3af'},grid:{vertLines:{color:'#1f2937'},horzLines:{color:'#1f2937'}},rightPriceScale:{borderColor:'#1f2937'},timeScale:{borderColor:'#1f2937',timeVisible:true}});
  series=chart.addCandlestickSeries({upColor:'#16a34a',downColor:'#dc2626',borderVisible:false,wickUpColor:'#16a34a',wickDownColor:'#dc2626'});
  const tfs=document.getElementById('tfs');TF.forEach(t=>{const b=document.createElement('button');b.textContent=t;b.dataset.tf=t;b.onclick=()=>setTf(t);tfs.appendChild(b)});
  setTf('1h');
  chart.subscribeClick(param=>{
    if(!param.time)return;
    const near=(marksData.markers||[]).filter(m=>Math.abs(m.time-param.time)<=90).slice(-1)[0];
    if(!near)return;const d=marksData.details[near.id]||{};
    document.getElementById('m-title').textContent=`${d.book} · ${d.side} ${d.strategy||''}`;
    document.getElementById('m-body').innerHTML=`<p>entry: ${d.price} qty: ${d.qty}</p><p>reason: ${d.reason||'—'}</p><p>exit: ${d.exit_reason||'open'} ${d.pnl?('pnl '+d.pnl):''}</p>`;
    document.getElementById('modal').classList.add('on');
  });
  document.getElementById('modal').onclick=()=>document.getElementById('modal').classList.remove('on');
  new ResizeObserver(()=>chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
  const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws'+location.search);
  ws.onopen=()=>document.getElementById('st-ws').textContent='ws: connected';
  ws.onerror=()=>{document.getElementById('st-ws').textContent='ws: error (polling)';};
  ws.onclose=()=>{document.getElementById('st-ws').textContent='ws: closed (polling)';setTimeout(()=>location.reload(),10000);};
  setInterval(poll,5000); poll();
  ws.onmessage=e=>{const msg=JSON.parse(e.data);
    if(msg.state){const s=msg.state;setStatus({...s,feed:msg.feed});drawBooks(s.books||{});drawDS(s.deepseek_last);drawRules(s.rules_last);document.getElementById('mode').textContent=(/forward|replay/i.test(s.root||'')?'REPLAY':'LIVE');
      const hb=s.heartbeat||{};document.getElementById('st-proc').textContent='process: '+(hb.pid||'?')+(hb.halted?' HALTED':'');
      document.getElementById('st-hb').textContent='heartbeat: '+(hb.last_ok_ms?new Date(hb.last_ok_ms).toLocaleTimeString():'?');
      document.getElementById('st-lat').textContent='latency: '+(hb.last_ok_ms?((s.server_time_ms-hb.last_ok_ms)/1000).toFixed(0)+'s':'?');}
    if(msg.klines&&series){const bar=msg.klines[tf];if(bar)series.update(bar);if(msg.klines['1m']&&tf==='1m')lastKline=msg.klines['1m'];}
    if(msg.feed){const f=msg.feed;document.getElementById('st-ws').textContent='feed: '+(f.connected?'live':'down')+(f.latency_ms!=null?(' '+f.latency_ms+'ms'):'');}
    if(msg.marks)applyMarks(msg.marks);
  };
  fetch('/api/state').then(r=>r.json()).then(s=>{drawBooks(s.books||{});drawDS(s.deepseek_last);drawRules(s.rules_last);setStatus(s);document.getElementById('mode').textContent=(/forward|replay/i.test(s.root||'')?'REPLAY':'LIVE');});
  fetch('/api/marks').then(r=>r.json()).then(applyMarks);
}
init();
</script></body></html>"""