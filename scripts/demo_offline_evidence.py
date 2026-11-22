"""用 Binance Demo **真实** exchangeInfo 跑离线全链路，输出可核对的请求参数证据。

不发任何真实订单：fetch 全部注入。目的只有一个——在拿到凭据前，
确认「发出去的请求长什么样」是对的（端点、参数名、签名、reduceOnly）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

from research.execution.adapter import DemoExecutionAdapter, SymbolFilters
from research.execution.binance_demo import DemoClient, DemoConfig
from research.strategy.contracts import TradeIntent

D = Decimal
OUT = Path("reports/demo_offline_evidence.json")


def real_filters() -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "https://demo-fapi.binance.com/fapi/v1/exchangeInfo",
        headers={"User-Agent": "PaperTrade-OpenAlpha/1.0 (read-only)"})
    with op.open(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    info = real_filters()
    f = SymbolFilters.from_exchange_info(info, "BTCUSDT")
    mark = D("58867")

    # 每次用干净的临时账本：否则上一次的 intent 会命中幂等分支，证据就不是全链路了
    work = Path("reports/_offline_ex")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    sent: list[dict] = []

    def capture(method, url, headers, body, timeout):
        # 只记录端点与参数；签名本身是 HMAC 结果，不是密钥
        parsed = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(body.decode() if body else parsed.query))
        sent.append({"method": method, "path": parsed.path,
                     "host": parsed.hostname, "params": params,
                     "api_key_header_present": bool(headers.get("X-MBX-APIKEY"))})
        t = params.get("type", "")
        if parsed.path == "/fapi/v1/algoOrder":
            return {"algoId": 2146760 + len(sent), "algoStatus": "NEW",
                    "clientAlgoId": params.get("clientAlgoId", "")}
        if parsed.path == "/fapi/v1/order" and method == "POST":
            return {"orderId": 1000 + len(sent), "status": "FILLED",
                    "executedQty": params.get("quantity", "0"),
                    "avgPrice": str(mark), "clientOrderId": params.get("newClientOrderId", "")}
        if parsed.path == "/fapi/v1/order" and method == "DELETE":
            return {"orderId": params.get("orderId"), "status": "CANCELED",
                    "code": "200", "msg": "success"}
        if parsed.path == "/fapi/v2/account":
            return {"totalWalletBalance": "10000", "assets": [
                {"asset": "USDT", "walletBalance": "10000", "availableBalance": "9900",
                 "unrealizedProfit": "0"}]}
        if parsed.path == "/fapi/v2/positionRisk":
            return []
        if parsed.path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            return []
        if parsed.path == "/fapi/v1/order" and method == "GET":
            # 真实交易所：订单不存在返回 -2013
            return {"code": -2013, "msg": "Order does not exist."}
        if parsed.path == "/fapi/v1/exchangeInfo":
            return info
        if parsed.path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        return {}

    cfg = DemoConfig(api_key="PLACEHOLDER_KEY_NOT_REAL",
                     api_secret="PLACEHOLDER_SECRET_NOT_REAL")
    client = DemoClient(cfg, fetch=capture)
    adapter = DemoExecutionAdapter(client, symbol="BTCUSDT", root=work,
                                  poll_s=0.0, poll_tries=1)
    adapter.filters = f

    pre = adapter.preflight()
    intent = TradeIntent("offline-abc123", int(time.time() * 1000), "BTCUSDT", "LONG",
                         "offline-check", "v1", mark, D("58600"), D("59100"),
                         600_000, D("0.8"), "offline parameter evidence", shadow=False)
    opened = adapter.open(intent, D("120"))
    closed = adapter.close(intent, opened.qty, "offline_check")

    # 签名可复算性：用占位密钥验证 HMAC 构造正确（真实密钥不参与）
    qs = urllib.parse.urlencode({k: v for k, v in sent[0]["params"].items()
                                 if k != "signature"})
    recomputed = hmac.new(cfg.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    sig_ok = recomputed == sent[0]["params"]["signature"]

    evidence = {
        "generated_at_ms": int(time.time() * 1000),
        "source": "demo-fapi.binance.com/fapi/v1/exchangeInfo (真实只读)",
        "credentials_used": False,
        "note": "全部请求由注入的 fetch 捕获，未发往交易所；密钥为占位符",
        "demo_btcusdt_filters": {"step_size": str(f.step_size), "tick_size": str(f.tick_size),
                                 "min_qty": str(f.min_qty), "min_notional": str(f.min_notional)},
        "preflight_ok": pre.get("ok"),
        "open_result": opened.to_dict(),
        "close_result": closed.to_dict(),
        "signature_recomputable": sig_ok,
        "requests": sent,
        "assertions": {
            "market_order_on_legacy_endpoint": any(
                r["path"] == "/fapi/v1/order" and r["params"].get("type") == "MARKET"
                for r in sent),
            "stop_on_algo_endpoint": any(
                r["path"] == "/fapi/v1/algoOrder"
                and r["params"].get("type") == "STOP_MARKET" for r in sent),
            "tp_on_algo_endpoint": any(
                r["path"] == "/fapi/v1/algoOrder"
                and r["params"].get("type") == "TAKE_PROFIT_MARKET" for r in sent),
            "no_stop_price_param_anywhere": not any(
                "stopPrice" in r["params"] for r in sent),
            "all_algo_reduce_only": all(
                r["params"].get("reduceOnly") == "true"
                for r in sent
                if r["path"] == "/fapi/v1/algoOrder" and r["method"] == "POST"),
            "protective_orders_are_opposite_side": (
                [r["params"]["side"] for r in sent
                 if r["path"] == "/fapi/v1/algoOrder" and r["method"] == "POST"] == ["SELL", "SELL"]
            ),
            "close_cancels_both_protective_orders": sum(
                1 for r in sent
                if r["path"] == "/fapi/v1/algoOrder" and r["method"] == "DELETE") == 2,
            "closing_market_order_is_reduce_only": any(
                r["path"] == "/fapi/v1/order" and r["method"] == "POST"
                and r["params"].get("reduceOnly") == "true" for r in sent),
            "all_hosts_are_demo": all(r["host"] == "demo-fapi.binance.com" for r in sent),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in evidence.items() if k != "requests"},
                     ensure_ascii=False, indent=2))
    print(f"\nrequests={len(sent)} -> {OUT}")
    return 0 if all(evidence["assertions"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())