"""Demo 执行适配器：把 V2 的 ``TradeIntent`` 映射成 Binance Demo 的真实虚拟订单。

这一层是 V2 引擎与交易所之间**唯一**的写路径。它负责六件事：

  1. **数量/价格合法化**：从 ``/fapi/v1/exchangeInfo`` 读 stepSize / tickSize /
     minQty / minNotional，向下取整（宁可小，不可超）；止损价按方向取保守侧。
  2. **开仓**：MARKET 单（``newClientOrderId`` 由 intent_id 派生），确认成交回执；
     成交后才挂保护单。
  3. **保护单**：STOP_MARKET / TAKE_PROFIT_MARKET 走 ``/fapi/v1/algoOrder``，
     ``reduceOnly=true`` + ``workingType=MARK_PRICE``，避免插针按最后成交价误触发。
  4. **失败兜底**：保护单挂不上 → 立刻市价平掉，绝不留裸仓；任一步失败都落库留痕。
  5. **重启对账**：交易所持仓 / 普通挂单 / 条件单 vs 本地意图表，输出
     ``orphan_position`` / ``unprotected_position`` / ``missing_position`` 三类差异。
  6. **重复订单保护**：``clientOrderId`` 主键，本地先查、交易所再查，重复调用不重复下单。

默认 ``enabled=False``：没有 Demo 凭据时只做离线校验，一个订单都不会发出去。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from research.strategy.contracts import TradeIntent

D = Decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_orders (
    client_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,               -- entry / stop / tp / close
    exchange_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    qty TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    ts_ms INTEGER NOT NULL,
    payload TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_demo_intent ON demo_orders (intent_id);
CREATE TABLE IF NOT EXISTS demo_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


class ExecutionRefused(RuntimeError):
    """前置校验不通过（不是交易所错误，而是我们主动不发单）。"""


@dataclass(frozen=True)
class SymbolFilters:
    step_size: Decimal = D("0.001")
    tick_size: Decimal = D("0.1")
    min_qty: Decimal = D("0.001")
    min_notional: Decimal = D("5")
    price_precision: int = 1
    quantity_precision: int = 3

    @classmethod
    def from_exchange_info(cls, info: dict, symbol: str) -> "SymbolFilters":
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            f = {x.get("filterType"): x for x in s.get("filters", [])}
            price = f.get("PRICE_FILTER") or {}
            lot = f.get("LOT_SIZE") or {}
            notional = f.get("MIN_NOTIONAL") or {}
            return cls(
                step_size=D(str(lot.get("stepSize", "0.001"))),
                tick_size=D(str(price.get("tickSize", "0.1"))),
                min_qty=D(str(lot.get("minQty", "0.001"))),
                min_notional=D(str(notional.get("notional", notional.get("minNotional", "5")))),
                price_precision=int(s.get("pricePrecision", 1)),
                quantity_precision=int(s.get("quantityPrecision", 3)),
            )
        raise ExecutionRefused(f"exchangeInfo 里没有 {symbol}")

    def qty(self, raw: Decimal) -> Decimal:
        if self.step_size <= 0:
            return raw
        return (raw / self.step_size).to_integral_value(rounding=ROUND_DOWN) * self.step_size

    def price(self, raw: Decimal, side: str = "nearest") -> Decimal:
        """side: nearest / down / up。止损价按方向取保守侧（LONG 止损向下、SHORT 向上）。"""
        if self.tick_size <= 0:
            return raw
        r = {"down": ROUND_DOWN, "up": ROUND_UP}.get(side)
        n = raw / self.tick_size
        if r is None:
            return n.to_integral_value() * self.tick_size
        return n.to_integral_value(rounding=r) * self.tick_size


@dataclass
class ExecutionResult:
    ok: bool
    stage: str
    intent_id: str
    qty: Decimal = D("0")
    entry_price: Decimal = D("0")
    order_id: str = ""
    stop_algo_id: str = ""
    tp_algo_id: str = ""
    error: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "stage": self.stage, "intent_id": self.intent_id,
                "qty": str(self.qty), "entry_price": str(self.entry_price),
                "order_id": self.order_id, "stop_algo_id": self.stop_algo_id,
                "tp_algo_id": self.tp_algo_id, "error": self.error}


class DemoExecutionAdapter:
    """``client`` 是 ``DemoClient``（或其测试替身），只要求实现同名方法。"""

    def __init__(self, client, symbol: str = "BTCUSDT", root: Path | str | None = None,
                 place_tp: bool = True, poll_s: float = 0.5, poll_tries: int = 6):
        self.client = client
        self.symbol = symbol
        self.place_tp = place_tp
        self.poll_s = poll_s
        self.poll_tries = poll_tries
        self.filters: SymbolFilters | None = None
        self.errors: list[str] = []
        self._db = None
        if root is not None:
            p = Path(root) / "demo_execution.sqlite"
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(p)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---------- 基础设施 ----------
    def available(self) -> bool:
        return bool(self.client.available()) and self._db is not None

    def preflight(self) -> dict:
        """只读体检：凭据、服务器时间、持仓模式、过滤器、余额、挂单。

        每一步单独 try：一个接口挂了不能掩盖其余信息（联调时最怕只看到第一个报错）。
        只有「读到账户」成功且「单向持仓」才 ok。
        """
        out: dict = {"symbol": self.symbol, "steps": {}, "errors": []}
        if not self.client.available():
            out.update({"ok": False, "reason": "DEMO_UNAVAILABLE: 没有 Demo 凭据"})
            return out

        def step(label: str, fn):
            try:
                val = fn()
                out["steps"][label] = "OK"
                return val
            except Exception as exc:  # noqa: BLE001
                out["steps"][label] = f"FAIL: {type(exc).__name__}: {exc}"
                out["errors"].append(f"{label}: {type(exc).__name__}: {exc}")
                return None

        offset = step("sync_time", self.client.sync_time)
        out["server_time_offset_ms"] = offset
        out["recv_window_ms"] = self.client.config.recv_window

        dual = step("position_mode", lambda: self.client._request(
            "GET", "/fapi/v1/positionSide/dual", signed=True))
        out["hedge_mode"] = None if dual is None else bool(dual.get("dualSidePosition"))

        def load_filters():
            info = self.client._request("GET", "/fapi/v1/exchangeInfo")
            self.filters = SymbolFilters.from_exchange_info(info, self.symbol)
            return {"step_size": str(self.filters.step_size),
                    "tick_size": str(self.filters.tick_size),
                    "min_qty": str(self.filters.min_qty),
                    "min_notional": str(self.filters.min_notional)}

        out["filters"] = step("exchange_info", load_filters)
        out["balances"] = step("account", self.client.balances)
        out["open_positions"] = step("positions", lambda: self.client.positions(self.symbol))
        out["open_orders"] = step("open_orders", lambda: self.client.open_orders(self.symbol))
        out["open_algo_orders"] = step("open_algo_orders",
                                       lambda: self.client.open_algo_orders(self.symbol))

        reasons = []
        if out["balances"] is None:
            reasons.append("读不到账户（凭据/权限/签名问题）")
        if out["hedge_mode"]:
            reasons.append("账户是双向持仓模式，本适配器只支持单向持仓")
        if out["filters"] is None:
            reasons.append("读不到 exchangeInfo 过滤器，无法安全下单")
        out["ok"] = not reasons
        if reasons:
            out["reason"] = "REFUSED: " + "；".join(reasons)
        return out

    def _ensure_filters(self) -> SymbolFilters:
        if self.filters is None:
            info = self.client._request("GET", "/fapi/v1/exchangeInfo")
            self.filters = SymbolFilters.from_exchange_info(info, self.symbol)
        return self.filters

    def _record(self, cid: str, intent_id: str, kind: str, *, exchange_id: str = "",
                status: str = "", qty: Decimal = D("0"), price: Decimal = D("0"),
                trigger: Decimal = D("0"), payload: dict | None = None) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT INTO demo_orders (client_order_id, intent_id, kind, exchange_id, status,"
            " qty, price, trigger, ts_ms, payload) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(client_order_id) DO UPDATE SET exchange_id=excluded.exchange_id,"
            " status=excluded.status, payload=excluded.payload",
            (cid, intent_id, kind, exchange_id, status, str(qty), str(price), str(trigger),
             int(time.time() * 1000), json.dumps(payload or {}, ensure_ascii=False)[:4000]),
        )
        self._db.commit()

    def _known(self, cid: str) -> dict | None:
        if self._db is None:
            return None
        row = self._db.execute("SELECT * FROM demo_orders WHERE client_order_id = ?",
                               (cid,)).fetchone()
        return dict(row) if row else None

    def _entry_cid(self, intent: TradeIntent) -> str:
        return f"oa-{intent.intent_id}"[:36]

    def _probe_existing(self, cid: str):
        """查交易所是否已有这张单。

        返回 dict = 已存在；None = 明确不存在；"UNREACHABLE" = 查不了（必须停）。
        Binance 的「不存在」是 -2013 Order does not exist / -2011 Unknown order。
        """
        try:
            row = self.client.order_status(self.symbol, client_id=cid)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "-2013" in msg or "-2011" in msg or "does not exist" in msg.lower() \
                    or "unknown order" in msg.lower():
                return None
            self.errors.append(f"probe {cid}: {type(exc).__name__}: {exc}")
            return "UNREACHABLE"
        if isinstance(row, dict) and row.get("orderId"):
            return row
        # 空 dict / 无 orderId：不能当成"已存在"，也不能当成"确认不存在"
        self.errors.append(f"probe {cid}: 查询返回无 orderId（{str(row)[:120]}）")
        return "UNREACHABLE"

    # ---------- 成交确认 ----------
    def _await_fill(self, order: dict, cid: str, tries: int | None = None) -> dict:
        """MARKET 单通常直接 FILLED；若是 NEW/PARTIALLY_FILLED 就轮询到终态。"""
        st = str(order.get("status") or "")
        oid = order.get("orderId")
        for _ in range(tries if tries is not None else self.poll_tries):
            if st in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                return order
            if oid is None:
                break
            time.sleep(self.poll_s)
            order = self.client.order_status(self.symbol, order_id=int(oid))
            st = str(order.get("status") or "")
        return order

    @staticmethod
    def _avg(order: dict) -> Decimal:
        for k in ("avgPrice", "price"):
            v = order.get(k)
            if v not in (None, "", "0", "0.00000", 0):
                return D(str(v))
        return D("0")

    # ---------- 开仓 ----------
    def open(self, intent: TradeIntent, notional: Decimal) -> ExecutionResult:
        if not self.available():
            return ExecutionResult(False, "preflight", intent.intent_id,
                                   error="DEMO_UNAVAILABLE: 没有 Demo 凭据或未配置账本")
        f = self._ensure_filters()
        cid = self._entry_cid(intent)
        known = self._known(cid)
        if known and known["status"] in ("FILLED", "NEW", "PARTIALLY_FILLED"):
            return ExecutionResult(True, "idempotent", intent.intent_id,
                                   qty=D(known["qty"] or "0"), order_id=known["exchange_id"])
        # 交易所侧再查一次：本地库丢了也不会重复开仓。
        # 只有明确的「订单不存在」才算没有；网络/签名/限流错误必须停下，
        # 否则会把「查不到」当成「不存在」而重复下单。
        existing = self._probe_existing(cid)
        if existing == "UNREACHABLE":
            return ExecutionResult(False, "probe", intent.intent_id,
                                   error="无法确认订单是否已存在（查询失败），拒绝下单以免重复")
        if isinstance(existing, dict):
            q = D(str(existing.get("executedQty") or "0"))
            if q <= 0:
                # 有 orderId 但没成交量：不是已成交，不能当成幂等成功
                return ExecutionResult(False, "probe", intent.intent_id,
                                       error=f"交易所已有订单 {existing.get('orderId')} 但成交量为 0，"
                                             f"status={existing.get('status')}；需人工核对",
                                       order_id=str(existing.get("orderId", "")),
                                       payload=existing)
            return ExecutionResult(True, "idempotent_exchange", intent.intent_id, qty=q,
                                   entry_price=self._avg(existing),
                                   order_id=str(existing.get("orderId")), payload=existing)

        if intent.entry_ref_price <= 0:
            return ExecutionResult(False, "validate", intent.intent_id, error="entry_ref_price<=0")
        qty = f.qty(notional / intent.entry_ref_price)
        if qty < f.min_qty:
            return ExecutionResult(False, "validate", intent.intent_id,
                                   error=f"数量 {qty} < minQty {f.min_qty}（名义 {notional}）")
        if qty * intent.entry_ref_price < f.min_notional:
            return ExecutionResult(False, "validate", intent.intent_id,
                                   error=f"名义 {qty * intent.entry_ref_price} < minNotional "
                                         f"{f.min_notional}")

        side = "BUY" if intent.side == "LONG" else "SELL"
        try:
            order = self.client.new_order(symbol=self.symbol, side=side, qty=qty,
                                          order_type="MARKET", client_id=cid)
            order = self._await_fill(order, cid)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"entry {cid}: {type(exc).__name__}: {exc}")
            # 超时/断连时订单**可能已经落地**：必须查一次再判失败，
            # 否则本地记 FAILED、交易所却真有仓（或反之），对账永远对不上。
            landed = self._probe_existing(cid)
            filled_q = D(str(landed.get("executedQty") or "0")) if isinstance(landed, dict) else D("0")
            if filled_q > 0:
                order = landed
                self.errors.append(f"entry {cid}: 请求异常但交易所已成交 "
                                   f"{filled_q}，按回执继续挂保护单")
            else:
                self._record(cid, intent.intent_id, "entry", status="FAILED",
                             qty=qty, payload={"error": str(exc)})
                err = f"{type(exc).__name__}: {exc}"
                if landed == "UNREACHABLE":
                    err += "；且无法确认订单是否已落地，需人工核对"
                return ExecutionResult(False, "entry", intent.intent_id, qty=qty, error=err)
        status = str(order.get("status") or "")
        if status not in ("FILLED", "PARTIALLY_FILLED"):
            self._record(cid, intent.intent_id, "entry", exchange_id=str(order.get("orderId", "")),
                         status=status, qty=qty, payload=order)
            return ExecutionResult(False, "entry", intent.intent_id, qty=qty,
                                   order_id=str(order.get("orderId", "")),
                                   error=f"未成交: status={status}", payload=order)

        filled = D(str(order.get("executedQty") or qty))
        avg = self._avg(order) or intent.entry_ref_price
        self._record(cid, intent.intent_id, "entry", exchange_id=str(order.get("orderId", "")),
                     status=status, qty=filled, price=avg, payload=order)
        res = ExecutionResult(True, "entry", intent.intent_id, qty=filled, entry_price=avg,
                              order_id=str(order.get("orderId", "")), payload=order)
        return self._protect(intent, res, f)

    # ---------- 保护单 ----------
    def _protect(self, intent: TradeIntent, res: ExecutionResult,
                 f: SymbolFilters) -> ExecutionResult:
        """挂止损（+止盈）。挂不上就市价平掉 —— 不允许裸仓过夜。"""
        exit_side = "SELL" if intent.side == "LONG" else "BUY"
        # 止损价取保守侧：LONG 向下取整，SHORT 向上取整，避免比意图更近而提前触发
        stop = f.price(intent.stop_price, "down" if intent.side == "LONG" else "up")
        tp = f.price(intent.take_profit_price, "up" if intent.side == "LONG" else "down")
        stop_cid = f"oa-{intent.intent_id}-s"[:36]
        tp_cid = f"oa-{intent.intent_id}-t"[:36]
        try:
            algo = self.client.new_order(symbol=self.symbol, side=exit_side, qty=res.qty,
                                         order_type="STOP_MARKET", stop_price=stop,
                                         client_id=stop_cid, reduce_only=True,
                                         working_type="MARK_PRICE")
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"stop {stop_cid}: {type(exc).__name__}: {exc}")
            self._record(stop_cid, intent.intent_id, "stop", status="FAILED",
                         qty=res.qty, trigger=stop, payload={"error": str(exc)})
            return self._fail_safe(intent, res, f, f"保护单失败: {exc}")
        stop_id = str(algo.get("algoId", ""))
        self._record(stop_cid, intent.intent_id, "stop", exchange_id=stop_id,
                     status=str(algo.get("algoStatus") or algo.get("status") or "NEW"),
                     qty=res.qty, trigger=stop, payload=algo)
        res.stop_algo_id = stop_id

        if self.place_tp:
            try:
                talgo = self.client.new_order(symbol=self.symbol, side=exit_side, qty=res.qty,
                                              order_type="TAKE_PROFIT_MARKET", stop_price=tp,
                                              client_id=tp_cid, reduce_only=True,
                                              working_type="MARK_PRICE")
                res.tp_algo_id = str(talgo.get("algoId", ""))
                self._record(tp_cid, intent.intent_id, "tp", exchange_id=res.tp_algo_id,
                             status=str(talgo.get("algoStatus") or "NEW"),
                             qty=res.qty, trigger=tp, payload=talgo)
            except Exception as exc:  # noqa: BLE001 - 止盈失败不致命：止损已在，仓位有保护
                self.errors.append(f"tp {tp_cid}: {type(exc).__name__}: {exc}")
                self._record(tp_cid, intent.intent_id, "tp", status="FAILED",
                             qty=res.qty, trigger=tp, payload={"error": str(exc)})
        res.stage = "protected"
        return res

    def _fail_safe(self, intent: TradeIntent, res: ExecutionResult, f: SymbolFilters,
                   reason: str) -> ExecutionResult:
        side = "SELL" if intent.side == "LONG" else "BUY"
        cid = f"oa-{intent.intent_id}-x"[:36]
        try:
            order = self.client.new_order(symbol=self.symbol, side=side, qty=res.qty,
                                          order_type="MARKET", client_id=cid,
                                          reduce_only=True)
            order = self._await_fill(order, cid)
            self._record(cid, intent.intent_id, "close", exchange_id=str(order.get("orderId", "")),
                         status=str(order.get("status") or ""), qty=res.qty, payload=order)
            return ExecutionResult(False, "emergency_close", intent.intent_id, qty=res.qty,
                                   entry_price=res.entry_price, order_id=res.order_id,
                                   error=reason, payload={"close": order})
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"emergency_close {cid}: {type(exc).__name__}: {exc}")
            self._record(cid, intent.intent_id, "close", status="FAILED",
                         qty=res.qty, payload={"error": str(exc)})
            # 平不掉是最高级别事故：必须冒泡给调用方停引擎
            raise ExecutionRefused(
                f"裸仓无法平掉 intent={intent.intent_id}: {reason}; close_failed={exc}") from exc

    # ---------- 平仓 ----------
    def close(self, intent: TradeIntent, qty: Decimal, reason: str = "") -> ExecutionResult:
        if not self.available():
            return ExecutionResult(False, "preflight", intent.intent_id,
                                   error="DEMO_UNAVAILABLE")
        f = self._ensure_filters()
        # 先撤条件单，避免平仓后残留一个反向触发单
        for kind, cid, cfun in (
                ("stop", f"oa-{intent.intent_id}-s"[:36],
                 lambda c: self.client.cancel_algo(self.symbol, client_algo_id=c)),
                ("tp", f"oa-{intent.intent_id}-t"[:36],
                 lambda c: self.client.cancel_algo(self.symbol, client_algo_id=c))):
            row = self._known(cid)
            try:
                cfun(cid)
                if row:
                    self._record(cid, intent.intent_id, kind, exchange_id=row["exchange_id"],
                                 status="CANCELED", qty=D(row["qty"] or "0"),
                                 trigger=D(row["trigger"] or "0"))
            except Exception as exc:  # noqa: BLE001 - 撤不到可能是已触发，继续平仓
                self.errors.append(f"cancel {cid}: {type(exc).__name__}: {exc}")

        side = "SELL" if intent.side == "LONG" else "BUY"
        q = f.qty(qty)
        cid = f"oa-{intent.intent_id}-c"[:36]
        try:
            order = self.client.new_order(symbol=self.symbol, side=side, qty=q,
                                          order_type="MARKET", client_id=cid, reduce_only=True)
            order = self._await_fill(order, cid)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"close {cid}: {type(exc).__name__}: {exc}")
            self._record(cid, intent.intent_id, "close", status="FAILED", qty=q,
                         payload={"error": str(exc)})
            return ExecutionResult(False, "close", intent.intent_id, qty=q,
                                   error=f"{type(exc).__name__}: {exc}")
        self._record(cid, intent.intent_id, "close", exchange_id=str(order.get("orderId", "")),
                     status=str(order.get("status") or ""), qty=q,
                     price=self._avg(order), payload=order)
        ok = str(order.get("status") or "") in ("FILLED", "PARTIALLY_FILLED")
        return ExecutionResult(ok, "close", intent.intent_id, qty=q,
                               entry_price=self._avg(order),
                               order_id=str(order.get("orderId", "")),
                               error="" if ok else f"平仓未成交: {order.get('status')}",
                               payload=order)

    # ---------- 对账 ----------
    def reconcile(self, local_open_intents: list[str] | None = None) -> dict:
        """重启对账：交易所为准，本地意图为辅，输出三类差异。"""
        out: dict = {"checked_at_ms": int(time.time() * 1000), "diffs": [], "ok": True}
        if not self.client.available():
            out.update({"ok": False, "reason": "DEMO_UNAVAILABLE"})
            return out
        positions = self.client.positions(self.symbol)
        open_plain = self.client.open_orders(self.symbol)
        open_algo = self.client.open_algo_orders(self.symbol)
        out["positions"] = positions
        out["open_orders"] = open_plain
        out["open_algo_orders"] = open_algo
        stop_ids = {str(o.get("clientAlgoId", "")) for o in open_algo}
        local = set(local_open_intents or [])
        for pos in positions:
            amt = float(pos.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            cids = {f"oa-{i}-s"[:36] for i in local} if local else set()
            protected = bool(cids & stop_ids) or bool(stop_ids)
            out["diffs"].append({
                "kind": "unprotected_position" if not protected else "position_ok",
                "symbol": pos.get("symbol"), "amount": pos.get("positionAmt"),
                "entry": pos.get("entryPrice"), "mark": pos.get("markPrice"),
                "pnl": pos.get("unRealizedProfit"),
            })
        if local and not positions:
            out["diffs"].append({"kind": "missing_position",
                                 "note": "本地记录有持仓，交易所没有（可能已被止损平掉）",
                                 "intents": sorted(local)})
        if self._db is not None:
            rows = self._db.execute(
                "SELECT intent_id, kind, status FROM demo_orders WHERE status IN"
                " ('NEW','PARTIALLY_FILLED')").fetchall()
            out["local_pending"] = [dict(r) for r in rows]
        out["ok"] = not any(d["kind"] in ("unprotected_position", "missing_position")
                            for d in out["diffs"])
        return out

    # ---------- 持仓管理动作 ----------
    def _current_algo(self, intent_id: str, kind: str) -> dict | None:
        if self._db is None:
            return None
        row = self._db.execute(
            "SELECT * FROM demo_orders WHERE intent_id = ? AND kind = ? AND status = 'NEW'"
            " ORDER BY ts_ms DESC LIMIT 1", (intent_id, kind)).fetchone()
        return dict(row) if row else None

    def _replace_algo(self, intent: TradeIntent, kind: str, order_type: str,
                      trigger: Decimal, suffix: str) -> ExecutionResult:
        """换保护单：**先挂新的，确认 NEW，再撤旧的**。

        顺序反了就会出现"已经撤掉旧止损、新止损还没挂上"的裸仓窗口，
        哪怕只有几十毫秒，也可能正好撞上插针。
        """
        if not self.available():
            return ExecutionResult(False, "preflight", intent.intent_id, error="DEMO_UNAVAILABLE")
        f = self._ensure_filters()
        old = self._current_algo(intent.intent_id, kind)
        qty = D(str(old["qty"])) if old else None
        if qty is None:
            row = self._db.execute(
                "SELECT qty FROM demo_orders WHERE intent_id = ? AND kind = 'entry'"
                " ORDER BY ts_ms DESC LIMIT 1", (intent.intent_id,)).fetchone() if self._db else None
            qty = D(str(row["qty"])) if row and row["qty"] else None
        if not qty:
            return ExecutionResult(False, "replace", intent.intent_id,
                                   error="找不到仓位数量，拒绝改保护单")
        side = "SELL" if intent.side == "LONG" else "BUY"
        n = int((old["exchange_id"] or "0")[-4:] or 0) if old else 0
        cid = f"oa-{intent.intent_id}-{suffix}{n + 1}"[:36]
        try:
            new = self.client.new_order(symbol=self.symbol, side=side, qty=qty,
                                        order_type=order_type, stop_price=f.price(trigger),
                                        client_id=cid, reduce_only=True,
                                        working_type="MARK_PRICE")
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"replace {kind} {cid}: {type(exc).__name__}: {exc}")
            return ExecutionResult(False, "replace", intent.intent_id, qty=qty,
                                   error=f"{type(exc).__name__}: {exc}")
        algo_id = str(new.get("algoId") or "")
        self._record(cid, intent.intent_id, kind, exchange_id=algo_id, status="NEW",
                     qty=qty, trigger=f.price(trigger), payload=new)
        if old:
            try:
                self.client.cancel_algo(self.symbol, algo_id=int(old["exchange_id"] or 0),
                                        client_algo_id=old["client_order_id"])
                self._record(old["client_order_id"], intent.intent_id, kind,
                             exchange_id=old["exchange_id"], status="CANCELED",
                             qty=D(old["qty"] or "0"), trigger=D(old["trigger"] or "0"))
            except Exception as exc:                 # noqa: BLE001
                # 撤旧单失败 = 两张同时在，仍是受保护的；记日志不掩盖
                self.errors.append(f"cancel old {kind}: {type(exc).__name__}: {exc}")
                return ExecutionResult(True, "replace_partial", intent.intent_id, qty=qty,
                                       order_id=algo_id,
                                       error=f"新单已挂，旧单撤销失败：{exc}")
        return ExecutionResult(True, "replace", intent.intent_id, qty=qty, order_id=algo_id)

    def replace_stop(self, intent: TradeIntent, new_stop: Decimal) -> ExecutionResult:
        return self._replace_algo(intent, "stop", "STOP_MARKET", new_stop, "s")

    def replace_tp(self, intent: TradeIntent, new_tp: Decimal) -> ExecutionResult:
        return self._replace_algo(intent, "tp", "TAKE_PROFIT_MARKET", new_tp, "t")

    def reduce(self, intent: TradeIntent, qty: Decimal, reason: str = "") -> ExecutionResult:
        """部分减仓：reduceOnly 市价，数量按 stepSize 向下取整。"""
        if not self.available():
            return ExecutionResult(False, "preflight", intent.intent_id, error="DEMO_UNAVAILABLE")
        f = self._ensure_filters()
        q = f.qty(qty)
        if q <= 0:
            return ExecutionResult(False, "validate", intent.intent_id, error="减仓数量取整后为 0")
        side = "SELL" if intent.side == "LONG" else "BUY"
        cid = f"oa-{intent.intent_id}-r{int(time.time()) % 100000}"[:36]
        try:
            order = self.client.new_order(symbol=self.symbol, side=side, qty=q,
                                          order_type="MARKET", client_id=cid, reduce_only=True)
            order = self._await_fill(order, cid)
        except Exception as exc:                     # noqa: BLE001
            self.errors.append(f"reduce {cid}: {type(exc).__name__}: {exc}")
            return ExecutionResult(False, "reduce", intent.intent_id, qty=q,
                                   error=f"{type(exc).__name__}: {exc}")
        self._record(cid, intent.intent_id, "reduce", exchange_id=str(order.get("orderId", "")),
                     status=str(order.get("status") or ""), qty=q, price=self._avg(order),
                     payload=order)
        return ExecutionResult(True, "reduce", intent.intent_id, qty=q,
                               entry_price=self._avg(order),
                               order_id=str(order.get("orderId", "")), payload=order)

    def close_db(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None