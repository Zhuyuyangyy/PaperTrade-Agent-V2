"""Demo 执行适配器测试：数量合法化、幂等、保护单走 algo、裸仓兜底、对账（零网络）。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.execution.adapter import (
    DemoExecutionAdapter,
    ExecutionRefused,
    SymbolFilters,
)
from research.strategy.contracts import TradeIntent

D = Decimal
SYMBOL = "BTCUSDT"

EXCHANGE_INFO = {
    "symbols": [{
        "symbol": "BTCUSDT", "pricePrecision": 2, "quantityPrecision": 3,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }]
}


class FakeClient:
    """只实现适配器用到的方法；条件单记录到 algo 通道，普通单记录到 plain 通道。"""

    def __init__(self):
        self.plain: list[dict] = []
        self.algo: list[dict] = []
        self.cancels: list[tuple[str, str]] = []
        self.fail_algo = False
        self.fail_plain = False
        self.pos_rows: list[dict] = []
        self._orders: dict[str, dict] = {}
        self._algos: dict[str, dict] = {}

    #: preflight 会读这两个；测试里时钟就是准的
    time_offset_ms = 0

    class _Cfg:
        recv_window = 60000

    config = _Cfg()

    def sync_time(self, samples: int = 1) -> int:
        return 0

    #: preflight 会读这两个；测试里时钟就是准的
    time_offset_ms = 0

    class _Cfg:
        recv_window = 60000

    config = _Cfg()

    def sync_time(self, samples: int = 1) -> int:
        return 0

    def available(self) -> bool:
        return True

    def _request(self, method, path, params=None, signed=False):
        if path.endswith("/exchangeInfo"):
            return EXCHANGE_INFO
        if path.endswith("/positionSide/dual"):
            return {"dualSidePosition": False}
        raise AssertionError(f"unexpected {path}")

    def balances(self):
        return {"USDT": {"wallet": "1000", "available": "1000", "unrealized": "0"}}

    def positions(self, symbol=SYMBOL):
        return list(self.pos_rows)

    def open_orders(self, symbol=SYMBOL):
        return [o for o in self.plain if o["status"] == "NEW"]

    def open_algo_orders(self, symbol=SYMBOL):
        return [{"clientAlgoId": k, "algoId": v["algoId"]} for k, v in self._algos.items()]

    def order_status(self, symbol=SYMBOL, order_id=None, client_id=""):
        if client_id and client_id in self._orders:
            return self._orders[client_id]
        raise RuntimeError("Order does not exist")

    def new_order(self, *, symbol, side, qty, order_type="MARKET", price=None,
                  stop_price=None, client_id="", reduce_only=False, close_position=False,
                  working_type="MARK_PRICE", activate_price=None, callback_rate=None):
        from research.execution.binance_demo import CONDITIONAL_TYPES
        if order_type in CONDITIONAL_TYPES:
            if self.fail_algo:
                raise RuntimeError("binance_error -4120: STOP_ORDER_SWITCH_ALGO")
            oid = 9000 + len(self.algo)
            rec = {"algoId": oid, "algoStatus": "NEW", "clientAlgoId": client_id,
                   "type": order_type, "trigger": stop_price, "qty": qty,
                   "reduceOnly": reduce_only, "side": side}
            self.algo.append(rec)
            self._algos[client_id] = rec
            return {"algoId": oid, "algoStatus": "NEW"}
        if self.fail_plain:
            raise RuntimeError("binance_error -2019: Margin is insufficient")
        oid = 1000 + len(self.plain)
        rec = {"orderId": oid, "status": "FILLED", "executedQty": str(qty),
               "avgPrice": str(price or 60000), "side": side, "type": order_type,
               "clientOrderId": client_id, "reduceOnly": reduce_only}
        self.plain.append(rec)
        self._orders[client_id] = rec
        return rec

    def cancel_algo(self, symbol=SYMBOL, algo_id=None, client_algo_id=""):
        self.cancels.append(("algo", client_algo_id))
        self._algos.pop(client_algo_id, None)
        return {"algoId": algo_id, "code": "200", "msg": "success"}


def _intent(side: str = "LONG", intent_id: str = "abc123", **kw) -> TradeIntent:
    base = dict(intent_id=intent_id, decision_ts_ms=1_795_000_000_000, symbol=SYMBOL,
                side=side, strategy_id="rules", strategy_version="v1",
                entry_ref_price=D("60000"), expected_holding_ms=3_600_000,
                confidence=D("0.7"), reason="test", shadow=False)
    if side == "LONG":
        base.update(stop_price=D("59000"), take_profit_price=D("62000"))
    else:
        base.update(stop_price=D("61000"), take_profit_price=D("58000"))
    base.update(kw)
    return TradeIntent(**base)


def _adapter(tmp_path: Path, client=None, **kw) -> DemoExecutionAdapter:
    a = DemoExecutionAdapter(client or FakeClient(), symbol=SYMBOL, root=tmp_path / "ex",
                             poll_s=0.0, poll_tries=1, **kw)
    a.filters = SymbolFilters.from_exchange_info(EXCHANGE_INFO, SYMBOL)
    return a


# ---------- 过滤器 ----------
def test_qty_rounds_down_never_up() -> None:
    f = SymbolFilters.from_exchange_info(EXCHANGE_INFO, SYMBOL)
    assert f.qty(D("0.0019")) == D("0.001")
    assert f.qty(D("0.00199")) == D("0.001")


def test_stop_price_rounds_to_conservative_side() -> None:
    f = SymbolFilters.from_exchange_info(EXCHANGE_INFO, SYMBOL)
    # LONG 止损向下取整（离得远一点，不会比意图更早触发）
    assert f.price(D("59000.37"), "down") == D("59000.30")
    # SHORT 止损向上取整
    assert f.price(D("61000.37"), "up") == D("61000.40")


def test_preflight_refuses_hedge_mode() -> None:
    class Hedge(FakeClient):
        def _request(self, method, path, params=None, signed=False):
            if path.endswith("/positionSide/dual"):
                return {"dualSidePosition": True}
            return super()._request(method, path, params, signed)

    a = _adapter(Path("."), Hedge())
    r = a.preflight()
    assert r["ok"] is False and "双向持仓" in r["reason"]


def test_preflight_is_read_only() -> None:
    c = FakeClient()
    a = _adapter(Path("."), c)
    a.preflight()
    assert not c.plain and not c.algo, "体检不允许下单"


# ---------- 开仓 ----------
def test_open_places_market_then_stop_via_algo(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok and res.stage == "protected"
    assert len(c.plain) == 1 and c.plain[0]["type"] == "MARKET" and c.plain[0]["side"] == "BUY"
    assert len(c.algo) == 2, "止损 + 止盈"
    stop = [x for x in c.algo if x["type"] == "STOP_MARKET"][0]
    assert stop["side"] == "SELL" and stop["reduceOnly"] is True
    assert Decimal(stop["trigger"]) == D("59000.0")
    assert stop["clientAlgoId"].endswith("-s")


def test_short_uses_buy_to_close_and_upper_stop(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    res = a.open(_intent("SHORT", "sh1"), D("100"))
    assert res.ok
    assert c.plain[0]["side"] == "SELL"
    stop = [x for x in c.algo if x["type"] == "STOP_MARKET"][0]
    assert stop["side"] == "BUY" and Decimal(stop["trigger"]) == D("61000.0")


def test_duplicate_open_is_idempotent(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    first = a.open(_intent(), D("100"))
    second = a.open(_intent(), D("100"))
    assert second.ok and second.stage == "idempotent"
    assert second.stage != "protected"
    assert len(c.plain) == 1, "重复调用不得重复下单"
    assert first.qty == second.qty


def test_qty_below_min_is_refused_before_sending(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    # 名义 100 / 6e6 ≈ 1.6e-5 BTC，远小于 minQty 0.001
    big = _intent(entry_ref_price=D("6000000"), stop_price=D("5900000"),
                  take_profit_price=D("6200000"))
    res = a.open(big, D("100"))
    assert res.ok is False and res.stage == "validate"
    assert not c.plain and not c.algo, "校验不过不许发单"


# ---------- 保护单失败兜底 ----------
def test_stop_failure_triggers_emergency_close(tmp_path: Path) -> None:
    c = FakeClient()
    c.fail_algo = True
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok is False and res.stage == "emergency_close"
    # 一张开仓 + 一张市价平仓，且不留裸仓
    assert len(c.plain) == 2
    assert c.plain[1]["side"] == "SELL" and c.plain[1]["reduceOnly"] is True


def test_emergency_close_failure_raises_and_blocks(tmp_path: Path) -> None:
    c = FakeClient()
    c.fail_algo = True
    a = _adapter(tmp_path, c)
    calls = {"n": 0}

    orig = c.new_order

    def flaky(**kw):
        if kw.get("order_type") == "MARKET" and kw.get("reduce_only"):
            calls["n"] += 1
            raise RuntimeError("network down")
        return orig(**kw)

    c.new_order = flaky                                   # type: ignore[method-assign]
    with pytest.raises(ExecutionRefused, match="裸仓"):
        a.open(_intent(), D("100"))
    assert calls["n"] == 1


def test_no_tp_still_has_stop(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c, place_tp=False)
    res = a.open(_intent(), D("100"))
    assert res.ok and len(c.algo) == 1
    assert c.algo[0]["type"] == "STOP_MARKET"


# ---------- 平仓 ----------
def test_close_cancels_algo_then_market_reduces(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    a.open(_intent(), D("100"))
    res = a.close(_intent(), D("0.001"), "manual")
    assert res.ok and res.stage == "close"
    assert len(c.cancels) == 2, "止损和止盈都要撤"
    assert c.plain[-1]["reduceOnly"] is True and c.plain[-1]["side"] == "SELL"


# ---------- 对账 ----------
def test_reconcile_flags_unprotected_position(tmp_path: Path) -> None:
    c = FakeClient()
    c.pos_rows = [{"symbol": SYMBOL, "positionAmt": "0.002", "entryPrice": "60000",
                    "markPrice": "60100", "unRealizedProfit": "0.2"}]
    a = _adapter(tmp_path, c)
    r = a.reconcile(local_open_intents=["abc123"])
    assert r["ok"] is False
    assert any(d["kind"] == "unprotected_position" for d in r["diffs"])


def test_reconcile_flags_missing_position(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    r = a.reconcile(local_open_intents=["abc123"])
    assert any(d["kind"] == "missing_position" for d in r["diffs"])
    assert r["ok"] is False


def test_reconcile_clean_when_protected(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    a.open(_intent(), D("100"))
    c.pos_rows = [{"symbol": SYMBOL, "positionAmt": "0.001", "entryPrice": "60000"}]
    r = a.reconcile(local_open_intents=["abc123"])
    assert r["ok"] is True, r["diffs"]


def test_unavailable_client_refuses_everything(tmp_path: Path) -> None:
    class NoCreds(FakeClient):
        def available(self):
            return False

    c = NoCreds()
    a = _adapter(tmp_path, c)
    assert a.open(_intent(), D("100")).ok is False
    assert a.close(_intent(), D("0.001")).ok is False
    assert a.reconcile()["ok"] is False
    assert not c.plain and not c.algo

# ---------- Demo 真实过滤器（取自 demo-fapi 2026-11-22 实测） ----------
DEMO_EXCHANGE_INFO = {
    "symbols": [{
        "symbol": "BTCUSDT", "pricePrecision": 2, "quantityPrecision": 4,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "MIN_NOTIONAL", "notional": "50"},
        ],
    }]
}


def test_demo_real_filters_are_parsed() -> None:
    f = SymbolFilters.from_exchange_info(DEMO_EXCHANGE_INFO, SYMBOL)
    assert f.step_size == D("0.0001")
    assert f.min_qty == D("0.0001")
    assert f.min_notional == D("50")
    assert f.tick_size == D("0.10")


def test_demo_20_usdt_notional_is_below_minimum(tmp_path: Path) -> None:
    """实测：Demo BTCUSDT minNotional=50、stepSize=0.0001。

    20 USDT @ 58867 → 0.0003 BTC → 名义 17.66 < 50，必须被拒，不能真发单。
    """
    c = FakeClient()
    a = DemoExecutionAdapter(c, symbol=SYMBOL, root=tmp_path / "ex", poll_s=0.0, poll_tries=1)
    a.filters = SymbolFilters.from_exchange_info(DEMO_EXCHANGE_INFO, SYMBOL)
    mark = D("58867")
    intent = _intent(entry_ref_price=mark, stop_price=D("58600"), take_profit_price=D("59100"))
    res = a.open(intent, D("20"))
    assert res.ok is False and res.stage == "validate"
    assert "minNotional" in res.error or "minQty" in res.error
    assert not c.plain and not c.algo, "低于门槛不许发任何订单"


def test_demo_120_usdt_notional_passes(tmp_path: Path) -> None:
    c = FakeClient()
    a = DemoExecutionAdapter(c, symbol=SYMBOL, root=tmp_path / "ex", poll_s=0.0, poll_tries=1)
    a.filters = SymbolFilters.from_exchange_info(DEMO_EXCHANGE_INFO, SYMBOL)
    mark = D("58867")
    intent = _intent(entry_ref_price=mark, stop_price=D("58600"), take_profit_price=D("59100"))
    res = a.open(intent, D("120"))
    assert res.ok and res.stage == "protected"
    assert res.qty == D("0.0020"), f"120/58867 向下取整应为 0.0020，实测 {res.qty}"
    assert res.qty * mark >= D("50")


# ---------- Demo 真实过滤器（取自 demo-fapi 2026-11-22 实测） ----------
DEMO_EXCHANGE_INFO = {
    "symbols": [{
        "symbol": "BTCUSDT", "pricePrecision": 2, "quantityPrecision": 4,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
            {"filterType": "MIN_NOTIONAL", "notional": "50"},
        ],
    }]
}


def test_demo_real_filters_are_parsed() -> None:
    f = SymbolFilters.from_exchange_info(DEMO_EXCHANGE_INFO, SYMBOL)
    assert f.step_size == D("0.0001")
    assert f.min_qty == D("0.0001")
    assert f.min_notional == D("50")
    assert f.tick_size == D("0.10")


def test_demo_20_usdt_notional_is_below_minimum(tmp_path: Path) -> None:
    """实测：Demo BTCUSDT minNotional=50、stepSize=0.0001。

    20 USDT @ 58867 → 0.0003 BTC → 名义 17.66 < 50，必须被拒，不能真发单。
    """
    c = FakeClient()
    a = DemoExecutionAdapter(c, symbol=SYMBOL, root=tmp_path / "ex", poll_s=0.0, poll_tries=1)
    a.filters = SymbolFilters.from_exchange_info(DEMO_EXCHANGE_INFO, SYMBOL)
    mark = D("58867")
    intent = _intent(entry_ref_price=mark, stop_price=D("58600"), take_profit_price=D("59100"))
    res = a.open(intent, D("20"))
    assert res.ok is False and res.stage == "validate"
    assert "minNotional" in res.error or "minQty" in res.error
    assert not c.plain and not c.algo, "低于门槛不许发任何订单"


def test_demo_120_usdt_notional_passes(tmp_path: Path) -> None:
    c = FakeClient()
    a = DemoExecutionAdapter(c, symbol=SYMBOL, root=tmp_path / "ex", poll_s=0.0, poll_tries=1)
    a.filters = SymbolFilters.from_exchange_info(DEMO_EXCHANGE_INFO, SYMBOL)
    mark = D("58867")
    intent = _intent(entry_ref_price=mark, stop_price=D("58600"), take_profit_price=D("59100"))
    res = a.open(intent, D("120"))
    assert res.ok and res.stage == "protected"
    assert res.qty == D("0.0020"), f"120/58867 向下取整应为 0.0020，实测 {res.qty}"
    assert res.qty * mark >= D("50")


# ---------- 幂等预查：不能把「查不了」当成「不存在」 ----------
def test_probe_unreachable_refuses_to_place(tmp_path: Path) -> None:
    """网络/签名错误时必须停，不能当成订单不存在而重复下单。"""
    class Flaky(FakeClient):
        def order_status(self, symbol=SYMBOL, order_id=None, client_id=""):
            raise RuntimeError("binance_error -1022: Signature for this request is not valid")

    c = Flaky()
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok is False and res.stage == "probe"
    assert "拒绝下单" in res.error
    assert not c.plain and not c.algo, "查不了就不许发单"


def test_probe_order_not_found_proceeds(tmp_path: Path) -> None:
    """-2013 Order does not exist 是明确的「不存在」，应当继续下单。"""
    class NotFound(FakeClient):
        def order_status(self, symbol=SYMBOL, order_id=None, client_id=""):
            raise RuntimeError("binance_error -2013: Order does not exist.")

    c = NotFound()
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok and res.stage == "protected"
    assert len(c.plain) == 1


def test_probe_empty_response_refuses(tmp_path: Path) -> None:
    """空响应/无 orderId：既不能当已存在，也不能当确认不存在。"""
    class Empty(FakeClient):
        def order_status(self, symbol=SYMBOL, order_id=None, client_id=""):
            return {}

    c = Empty()
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok is False and res.stage == "probe"
    assert not c.plain and not c.algo


def test_probe_zero_filled_existing_order_refuses(tmp_path: Path) -> None:
    """有 orderId 但成交量 0：不是幂等成功，必须报出来而不是返回假成交。"""
    class ZeroFill(FakeClient):
        def order_status(self, symbol=SYMBOL, order_id=None, client_id=""):
            return {"orderId": 555, "status": "NEW", "executedQty": "0"}

    c = ZeroFill()
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok is False and res.stage == "probe"
    assert res.order_id == "555"
    assert not c.plain, "已存在未成交订单时不得再发新单"


# ---------- 下单超时但交易所已成交 ----------
class TimeoutButFilled(FakeClient):
    """POST 读超时，但订单其实已经成交（真实联调里就发生过这种）。"""

    def new_order(self, **kw):
        cid = kw.get("client_id", "")
        if kw.get("order_type") in ("MARKET",) and not kw.get("reduce_only"):
            rec = {"orderId": 777777, "status": "FILLED", "executedQty": "0.0017",
                   "avgPrice": "60000", "side": kw.get("side", "BUY"),
                   "clientOrderId": cid, "reduceOnly": False}
            self._orders[cid] = rec
            raise TimeoutError("The read operation timed out")
        return super().new_order(**kw)


def test_open_recovers_when_post_times_out_but_exchange_filled(tmp_path: Path) -> None:
    c = TimeoutButFilled()
    a = _adapter(tmp_path, c)
    res = a.open(_intent(), D("100"))
    assert res.ok and res.stage == "protected", res.to_dict()
    assert res.order_id == "777777"
    assert [x["type"] for x in c.algo] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]


def test_open_reports_unreachable_when_probe_also_fails(tmp_path: Path) -> None:
    class TimeoutAndUnreachable(FakeClient):
        def new_order(self, **kw):
            raise TimeoutError("The read operation timed out")

        def order_status(self, symbol=SYMBOL, order_id=None, client_id=""):
            raise RuntimeError("HTTP 500 Internal Server Error")

    a = _adapter(tmp_path, TimeoutAndUnreachable())
    res = a.open(_intent(), D("100"))
    assert res.ok is False and "无法确认" in res.error


# ---------- V3：部分减仓与保护单更换 ----------
def test_reduce_places_reduce_only_market(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    res = a.reduce(_intent(), D("0.002"), "MGR_PARTIAL")
    assert res.ok and res.stage == "reduce"
    o = c.plain[-1]
    assert o["reduceOnly"] is True and o["side"] == "SELL"   # LONG 的减仓方向
    assert Decimal(o["executedQty"]) == D("0.002")


def test_replace_stop_places_new_before_cancelling_old(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    opened = a.open(_intent(), D("100"))
    assert opened.ok
    old_stop = [x for x in c.algo if x["type"] == "STOP_MARKET"][0]
    res = a.replace_stop(_intent(), D("60500"))
    assert res.ok and res.stage == "replace"
    stops = [x for x in c.algo if x["type"] == "STOP_MARKET"]
    assert len(stops) == 2 and stops[-1]["clientAlgoId"] != old_stop["clientAlgoId"]
    assert ("algo", old_stop["clientAlgoId"]) in c.cancels


def test_replace_stop_keeps_both_when_cancel_fails(tmp_path: Path) -> None:
    class CancelFails(FakeClient):
        def cancel_algo(self, symbol=SYMBOL, algo_id=None, client_algo_id=""):
            raise RuntimeError("HTTP 500 cancel failed")

    c = CancelFails()
    a = _adapter(tmp_path, c)
    a.open(_intent(), D("100"))
    res = a.replace_stop(_intent(), D("60500"))
    assert res.ok and res.stage == "replace_partial"
    stops = [x for x in c.algo if x["type"] == "STOP_MARKET"]
    assert len(stops) == 2, "新单已挂、旧单撤销失败时必须两张都在，不能裸仓"
    assert "撤销失败" in res.error


# ---------- V3：部分减仓与保护单更换 ----------
def test_reduce_places_reduce_only_market(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    res = a.reduce(_intent(), D("0.002"), "MGR_PARTIAL")
    assert res.ok and res.stage == "reduce"
    o = c.plain[-1]
    assert o["reduceOnly"] is True and o["side"] == "SELL"   # LONG 的减仓方向
    assert Decimal(o["executedQty"]) == D("0.002")


def test_replace_stop_places_new_before_cancelling_old(tmp_path: Path) -> None:
    c = FakeClient()
    a = _adapter(tmp_path, c)
    opened = a.open(_intent(), D("100"))
    assert opened.ok
    old_stop = [x for x in c.algo if x["type"] == "STOP_MARKET"][0]
    res = a.replace_stop(_intent(), D("60500"))
    assert res.ok and res.stage == "replace"
    stops = [x for x in c.algo if x["type"] == "STOP_MARKET"]
    assert len(stops) == 2 and stops[-1]["clientAlgoId"] != old_stop["clientAlgoId"]
    assert ("algo", old_stop["clientAlgoId"]) in c.cancels


def test_replace_stop_keeps_both_when_cancel_fails(tmp_path: Path) -> None:
    class CancelFails(FakeClient):
        def cancel_algo(self, symbol=SYMBOL, algo_id=None, client_algo_id=""):
            raise RuntimeError("HTTP 500 cancel failed")

    c = CancelFails()
    a = _adapter(tmp_path, c)
    a.open(_intent(), D("100"))
    res = a.replace_stop(_intent(), D("60500"))
    assert res.ok and res.stage == "replace_partial"
    stops = [x for x in c.algo if x["type"] == "STOP_MARKET"]
    assert len(stops) == 2, "新单已挂、旧单撤销失败时必须两张都在，不能裸仓"
    assert "撤销失败" in res.error
