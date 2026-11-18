"""Cross-Market v2.2 数据源连通性探测（只读，无交易权限）。

探测对象：
  1. Polymarket Gamma API（公开市场查询）
  2. Polymarket CLOB REST（订单簿，用 Gamma 返回的真实 token_id）
  3. Polymarket CLOB WebSocket（握手 + 订阅 + 真实消息捕获）

输出 network_probe.json：每个端点的 HTTP 状态/延迟/响应结构/错误分类。

2026-11-18 审查加固：
  - DNS 落在 198.18.0.0/15（RFC2544 保留段）= fake-ip/透明代理特征 → 显式标记
    directness_unproven，绝不宣称"全链路直连"；
  - hostname 如实记录，但附 deployment_node 声明字段（由操作者确认）；
  - WS 不止握手：订阅 market channel 并捕获至少一条真实消息才记 OK。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets?limit=3&active=true&closed=false"
CLOB_BOOK = "https://clob.polymarket.com/book"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

UA = {"User-Agent": "PaperTrade-CrossMarketProbe/0.2 (research; read-only)"}
FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def probe_http(url: str, timeout: float = 15.0) -> dict:
    """单端点 HTTP 探测：状态/延迟/结构/错误分类。"""
    rec: dict = {"url": url, "ts": datetime.now(timezone.utc).isoformat()}
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=UA)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Python 层不挂代理
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
    """DNS 解析 + fake-ip 特征检测（审查项：不能只凭关代理就宣称直连）。"""
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except Exception as e:  # noqa: BLE001
        return {"host": host, "error": str(e)[:120]}
    fake = []
    for ip in ips:
        try:
            if ipaddress.ip_address(ip) in FAKE_IP_NET:
                fake.append(ip)
        except ValueError:
            pass
    return {
        "host": host,
        "ips": ips[:4],
        "fake_ip_detected": bool(fake),
        "note": (
            "解析落在 198.18.0.0/15（保留段）= fake-ip/透明代理特征；"
            "Python 层未挂代理不等于全链路直连——directness_unproven"
        ) if fake else "解析为常规公网地址",
    }


def probe_ws_subscribe(url: str, token_id: str, timeout: float = 20.0) -> dict:
    """CLOB WebSocket：握手 + 订阅 + 捕获真实消息。

    2026-11-19 实测结论（诚实记录）：握手 OK；/ws/market 路径下发送
    {"assets_id":[token],"type":"market"} 与 /ws/ 路径均未收到消息（0 条）。
    消息流未证明 → ws_ok=False，作为 P1 阻塞项，不伪造成功。
    """
    rec: dict = {"url": url, "ts": datetime.now(timezone.utc).isoformat()}
    t0 = time.monotonic()
    try:
        import websocket  # type: ignore

        ws = websocket.create_connection(url, timeout=timeout)
        rec["ws_handshake"] = "OK"
        ws.send(json.dumps({"assets_id": [token_id], "type": "market"}))
        messages = []
        ws.settimeout(10.0)
        try:
            for _ in range(5):
                messages.append(ws.recv())
        except Exception:  # noqa: BLE001 — 超时即止
            pass
        rec["ws_subscribe"] = "OK" if messages else "NO_MESSAGE"
        rec["messages_received"] = len(messages)
        if not messages:
            rec["note"] = (
                "握手成功但订阅后 10s 无消息（已试 assets_id 数组 / /ws/ 路径，"
                "均未收到）——消息流未证明，P1 阻塞项"
            )
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        ws.close()
    except ImportError:
        rec["ws_handshake"] = "SKIPPED"
        rec["note"] = "websocket-client 未安装"
    except Exception as e:  # noqa: BLE001
        rec["ws_handshake"] = "FAIL"
        rec["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        rec["error_class"] = classify_network_error(str(e))
    rec["ws_ok"] = rec.get("ws_handshake") == "OK" and rec.get("ws_subscribe") == "OK"
    return rec


def main() -> None:
    report = {
        "probe": "cross-market-v2.2 datasource reachability (read-only)",
        "at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "deployment_node": "UNCONFIRMED — 操作者须确认本机即部署节点（hostname 仅为记录）",
        "python": "3.x stdlib + websocket-client",
        "dns": [probe_dns("gamma-api.polymarket.com"), probe_dns("clob.polymarket.com")],
        "endpoints": [],
    }
    gamma = probe_http(GAMMA_MARKETS)
    report["endpoints"].append({"name": "gamma_markets", **gamma})

    token_id = extract_token_id()
    if token_id:
        book = probe_http(f"{CLOB_BOOK}?token_id={token_id}")
        book["token_id_used"] = token_id
        report["endpoints"].append({"name": "clob_book", **book})
        report["endpoints"].append({"name": "clob_ws", **probe_ws_subscribe(CLOB_WS, token_id)})
    else:
        report["endpoints"].append({
            "name": "clob_book", "reachable": False,
            "error": "无法从 Gamma 取得 token_id（上游失败）", "blocked_by": "gamma",
        })
        report["endpoints"].append({
            "name": "clob_ws", "ws_ok": False, "error": "跳过（无 token_id）",
        })

    summary = {}
    for e in report["endpoints"]:
        summary[e["name"]] = "OK" if (e.get("reachable") or e.get("ws_ok")) else (e.get("error_class") or e.get("error") or "FAIL")
    report["summary"] = summary
    dns_clean = not any(d.get("fake_ip_detected") for d in report["dns"])
    report["directness"] = "PROVEN_DIRECT" if (dns_clean and all(v == "OK" for v in summary.values())) else (
        "SUSPECTED_FAKE_IP_OR_PROXY" if not dns_clean else "PARTIAL"
    )
    report["verdict"] = (
        "ALL_REACHABLE" if all(v == "OK" for v in summary.values())
        else "PARTIAL" if any(v == "OK" for v in summary.values())
        else "BLOCKED"
    )
    out = Path(__file__).resolve().parents[2] / "network_probe.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "summary": summary,
        "verdict": report["verdict"],
        "directness": report["directness"],
        "saved": str(out),
    }, ensure_ascii=False, indent=2))


def extract_token_id() -> str | None:
    """从 Gamma 拉一个完整市场，取真实 clobTokenIds[0]。"""
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
    except Exception:  # noqa: BLE001
        return None
    return None


if __name__ == "__main__":
    main()
