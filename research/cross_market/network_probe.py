"""Cross-Market v2.2 数据源连通性探测（只读，无交易权限）。

探测对象：
  1. Polymarket Gamma API（公开市场查询）
  2. Polymarket CLOB REST（订单簿，用 Gamma 返回的真实 token_id）
  3. Polymarket CLOB WebSocket（公开市场流）

输出 network_probe.json：每个端点的 HTTP 状态/延迟/响应结构/错误分类。
纪律：直连探测（不挂代理）；任何失败如实记录为 BLOCKED，不伪造成功。
"""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets?limit=3&active=true&closed=false"
CLOB_BOOK = "https://clob.polymarket.com/book"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

UA = {"User-Agent": "PaperTrade-CrossMarketProbe/0.1 (research; read-only)"}


def probe_http(url: str, timeout: float = 15.0) -> dict:
    """单端点 HTTP 探测：状态/延迟/结构/错误分类。"""
    rec: dict = {"url": url, "ts": datetime.now(timezone.utc).isoformat()}
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=UA)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(2_000_000)
            rec["http_status"] = resp.status
            rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            try:
                data = json.loads(body)
                if isinstance(data, list) and data:
                    rec["response_kind"] = "list"
                    rec["item_count"] = len(data)
                    rec["first_item_keys"] = sorted(data[0].keys())[:15] if isinstance(data[0], dict) else None
                    rec["sample"] = json.dumps(data[0], ensure_ascii=False)[:600]
                elif isinstance(data, dict):
                    rec["response_kind"] = "dict"
                    rec["keys"] = sorted(data.keys())[:15]
                    rec["sample"] = json.dumps(data, ensure_ascii=False)[:600]
            except json.JSONDecodeError:
                rec["response_kind"] = "non-json"
                rec["body_head"] = body[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        rec["http_status"] = e.code
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        rec["error"] = f"HTTP {e.code}"
        rec["body_head"] = e.read(300).decode("utf-8", "replace")
    except urllib.error.URLError as e:
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        reason = getattr(e, "reason", e)
        rec["error"] = f"URLError: {reason}"
        rec["error_class"] = classify_network_error(str(reason))
    except (socket.timeout, TimeoutError):
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        rec["error"] = "TIMEOUT"
        rec["error_class"] = "TIMEOUT"
    except Exception as e:  # noqa: BLE001 — 探测器必须永不带病吞错
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["error_class"] = "UNKNOWN"
    rec["reachable"] = "error" not in rec and "error_class" not in rec
    return rec


def classify_network_error(reason: str) -> str:
    r = reason.lower()
    if "name resolution" in r or "getaddrinfo" in r or "nodename" in r:
        return "DNS_FAIL"
    if "connection refused" in r:
        return "CONN_REFUSED"
    if "timed out" in r or "timeout" in r:
        return "TIMEOUT"
    if "ssl" in r or "certificate" in r:
        return "TLS_FAIL"
    if "reset" in r:
        return "CONN_RESET"
    return "OTHER"


def probe_dns(host: str) -> dict:
    try:
        ips = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return {"host": host, "ips": sorted({i[4][0] for i in ips})[:4]}
    except Exception as e:  # noqa: BLE001
        return {"host": host, "error": str(e)[:120]}


def probe_ws(url: str, timeout: float = 12.0) -> dict:
    """CLOB WebSocket 裸握手探测（不订阅完整逻辑，只验证升级）。"""
    rec: dict = {"url": url, "ts": datetime.now(timezone.utc).isoformat()}
    t0 = time.monotonic()
    try:
        import websocket  # type: ignore

        ws = websocket.create_connection(url, timeout=timeout)
        rec["ws_handshake"] = "OK"
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        ws.close()
    except ImportError:
        rec["ws_handshake"] = "SKIPPED"
        rec["note"] = "websocket-client 未安装（P0 阶段只探测 REST）"
    except Exception as e:  # noqa: BLE001
        rec["ws_handshake"] = "FAIL"
        rec["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        rec["error_class"] = classify_network_error(str(e))
    return rec


def main() -> None:
    report = {
        "probe": "cross-market-v2.2 datasource reachability (read-only)",
        "at": datetime.now(timezone.utc).isoformat(),
        "python": socket.gethostname(),
        "dns": [probe_dns("gamma-api.polymarket.com"), probe_dns("clob.polymarket.com")],
        "endpoints": [],
    }
    # 1) Gamma
    gamma = probe_http(GAMMA_MARKETS)
    report["endpoints"].append({"name": "gamma_markets", **gamma})
    # 2) CLOB REST：需要真实 token_id —— 从 Gamma 的市场里取 clobTokenIds
    token_id = extract_token_id(gamma)
    if token_id:
        book = probe_http(f"{CLOB_BOOK}?token_id={token_id}")
        book["token_id_used"] = token_id
        report["endpoints"].append({"name": "clob_book", **book})
    else:
        report["endpoints"].append({
            "name": "clob_book", "reachable": False, "error": "无法从 Gamma 取得 token_id（上游失败）",
            "blocked_by": "gamma",
        })
    # 3) CLOB WS
    report["endpoints"].append({"name": "clob_ws", **probe_ws(CLOB_WS)})

    summary = {
        e["name"]: ("OK" if e.get("reachable") or e.get("ws_handshake") == "OK" else e.get("error_class") or e.get("error") or "FAIL")
        for e in report["endpoints"]
    }
    report["summary"] = summary
    report["verdict"] = (
        "ALL_REACHABLE" if all(v == "OK" for v in summary.values())
        else "PARTIAL" if any(v == "OK" for v in summary.values())
        else "BLOCKED"
    )
    out = Path(__file__).resolve().parents[2] / "network_probe.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "verdict": report["verdict"], "saved": str(out)}, ensure_ascii=False, indent=2))


def extract_token_id(gamma_rec: dict) -> str | None:
    """从 Gamma 拉一个完整市场，取真实 clobTokenIds[0]（探测记录里的样本被截断，不可靠）。"""
    try:
        req = urllib.request.Request(GAMMA_MARKETS, headers=UA)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15.0) as resp:
            data = json.load(resp)
        for item in data:
            raw = item.get("clobTokenIds")
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, list) and raw and str(raw[0]).isdigit():
                return str(raw[0])
    except Exception:  # noqa: BLE001 — 探测失败由上层记录
        return None
    return None


if __name__ == "__main__":
    main()
