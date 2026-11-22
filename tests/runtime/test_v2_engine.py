"""V2 引擎测试：三账本、5m 决策、tick 止损、早退、双模型缺省、情报闭环、已收盘约束。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from research.decision import Decision, MarketSnapshot
from research.intel.contracts import IntelDigest, NewsEvent, SourceRef
from research.market.contracts import TF_5M_MS
from research.runtime.v2_engine import BOOKS, V2Engine
from research.risk.engine import RiskLimits

D = Decimal
BASE = (1_795_000_000 // 300) * 300
NOW = (BASE + 320 * 300) * 1000


class FakeFeed:
    """只提供 K 线与价格；``bars`` 可随模拟时钟往前长。"""

    STEPS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

    def __init__(self, price: float = 100.0):
        self.price_value = price
        self.seeded_until_ms = NOW

    def series(self, tf: str, limit: int = 500):
        step = self.STEPS.get(tf, 300)
        n = 320 if tf == "5m" else 500
        end_s = (self.seeded_until_ms // 1000 // step) * step
        start = end_s - (n - 1) * step
        out, p = [], 100.0
        for i in range(n):
            o = p
            p += 0.05
            out.append({"time": start + i * step, "open": o, "high": p + 0.2,
                        "low": o - 0.2, "close": p, "volume": 1.0})
        return out[-limit:]

    def base_series(self, tf: str):
        return self.series(tf)

    def latest(self, tf: str = "5m"):
        return self.series(tf, 2)[-1]

    def price(self):
        return self.price_value

    def status(self):
        return {"mode": "test"}


class CountingProvider:
    """记录每次 decide 的实际输入，用来证明 digest 真的到了模型手里。"""

    name = "deepseek"
    version = "t"

    def __init__(self, choice: str = "LONG"):
        self.choice = choice
        self.calls: list[MarketSnapshot] = []

    def available(self) -> bool:
        return True

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        self.calls.append(snapshot)
        return Decision(self.name, "t", "OK", self.choice, 0.8, D("0.7"), "fake", ("e",))


def _engine(tmp_path: Path, deepseek=None, second=None, intel_fn=None) -> V2Engine:
    eng = V2Engine(tmp_path / "v2", FakeFeed(),
                   RiskLimits(min_volatility_budget_bps=D("0"), fee_bps=D("0"),
                              slippage_bps=D("0")),
                   deepseek=deepseek, second=second, intel_fn=intel_fn)
    eng.load_history = lambda as_of_ms=None: (setattr(eng, "_history_ts", as_of_ms or 0), True)[1]
    return eng


def _digest(ts: int) -> IntelDigest:
    return IntelDigest(as_of_ms=ts, news=(
        NewsEvent(event_id="e1", headline="ETF inflows hit record",
                  facts=(), occurred_at_ms=ts, first_seen_ms=ts,
                  sources=(SourceRef("https://example.com/a", "example", ts, ts, ts),),
                  credibility=D("0.9"), horizon_ms=3_600_000, direction="bullish",
                  confidence=D("0.8")),
    ))


# ---------- 账本隔离 ----------
def test_three_books_are_isolated(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.on_5m_close(NOW)
    for name in BOOKS:
        assert (tmp_path / "v2" / f"{name}.sqlite").is_file()
    assert e.books["deepseek_5m"]._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert e.books["dual_5m"]._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_prefilter_blocks_when_cost_too_high(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.limits = RiskLimits(min_volatility_budget_bps=D("1000"), fee_bps=D("50"),
                          slippage_bps=D("50"))
    e.on_5m_close(NOW)
    log = (tmp_path / "v2" / "deepseek_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["decision"]["rationale"] == "COST_TOO_HIGH"


# ---------- 止损/早退 ----------
def test_tick_stop_closes_position(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    stop = Decimal(pos["stop_price"])
    closed = e.on_tick(float(stop) - 1.0, NOW + 1000)
    assert any(c.reason == "STOP_LOSS" for c in closed)


def test_early_exit_after_two_bars(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    entry = Decimal(pos["entry_price"])
    e.feed.price_value = float(entry) - 0.01
    e.on_5m_close(NOW + 2 * 300_000)
    closed = e.books["deepseek_5m"]._db.execute(
        "SELECT exit_reason FROM positions WHERE status='closed'").fetchall()
    assert any(r["exit_reason"] == "EARLY_EXIT" for r in closed)


# ---------- Dual 语义 ----------
def test_dual_without_second_marks_unavailable(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider(), second=None)
    e.on_5m_close(NOW)
    log = (tmp_path / "v2" / "dual_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(log[-1])
    assert row["decision"]["choice"] == "HOLD"
    assert "dual_paused" in row["decision"]["rationale"]


def test_deepseek_called_once_per_snapshot(tmp_path: Path) -> None:
    """Dual 盘缺第二模型时，一轮 5m 只允许一次 DeepSeek 调用。"""
    ds = CountingProvider()
    e = _engine(tmp_path, deepseek=ds, second=None)
    e.on_5m_close(NOW)
    assert len(ds.calls) == 1, f"DeepSeek 被调用 {len(ds.calls)} 次"
    assert e.last_inputs["model_calls"] == ["deepseek_5m"]


def test_dual_with_second_calls_each_model_once(tmp_path: Path) -> None:
    ds, sd = CountingProvider("LONG"), CountingProvider("LONG")
    sd.name = "second"
    e = _engine(tmp_path, deepseek=ds, second=sd)
    e.on_5m_close(NOW)
    assert len(ds.calls) == 1 and len(sd.calls) == 1
    log = (tmp_path / "v2" / "dual_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(log[-1])
    assert row["decision"]["choice"] == "LONG"
    assert row["decision"]["version"] == "dual"


# ---------- 情报闭环 ----------
def test_intel_digest_reaches_the_model(tmp_path: Path) -> None:
    """回归：on_5m_close 曾经根本不调用 intel_fn，digest 永远是 None。"""
    ds = CountingProvider()
    seen_ts: list[int] = []

    def intel_fn(ts: int) -> IntelDigest:
        seen_ts.append(ts)
        return _digest(ts)

    e = _engine(tmp_path, deepseek=ds, intel_fn=intel_fn)
    e.on_5m_close(NOW)
    assert seen_ts == [NOW], "intel_fn 必须真的被调用"
    assert ds.calls, "模型必须被调用"
    intel = ds.calls[0].intel
    assert intel is not None and intel.news["bias"] > 0, "新闻必须进到 snapshot"
    assert e.last_inputs["intel"]["status"] == "OK"
    assert e.last_inputs["intel"]["news"] == 1
    assert e.last_inputs["intel_features"]["news"]["count"] > 0


def test_intel_failure_does_not_block_trading(tmp_path: Path) -> None:
    ds = CountingProvider()

    def boom(ts):
        raise RuntimeError("feed down")

    e = _engine(tmp_path, deepseek=ds, intel_fn=boom)
    e.on_5m_close(NOW)
    assert ds.calls, "情报失败也要照常决策"
    assert e.last_inputs["intel"]["status"] == "ERROR"
    log = (tmp_path / "v2" / "rules_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["intel"]["status"] == "ERROR"


def test_intel_disabled_is_recorded_not_silent(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider(), intel_fn=None)
    e.on_5m_close(NOW)
    assert e.last_inputs["intel"]["status"] == "DISABLED"


# ---------- 已收盘 K 线 ----------
def test_views_exclude_the_forming_bar(tmp_path: Path) -> None:
    """决策只能用已收盘 K 线：末根 close_ms 必须 <= as_of - settle。"""
    e = _engine(tmp_path, deepseek=CountingProvider())
    as_of = NOW
    views = e._views(as_of)
    last = views["5m"][-1]
    assert last.close_ms <= as_of - e.config.settle_ms
    assert last.open_ms == ((as_of - e.config.settle_ms) // TF_5M_MS) * TF_5M_MS - TF_5M_MS


def test_latest_closed_5m_ignores_forming_bar(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    bar_open_s = NOW // 1000
    open_ms = (bar_open_s // 300) * 300 * 1000

    # 刚开盘 10s：当前这根未收盘，且上一根才收盘 10s，未过 settle(30s) → 只能到上上根
    assert e.latest_closed_5m(open_ms + 10_000) == open_ms - 2 * TF_5M_MS
    # 开盘 40s：上一根已收盘 40s > settle → 可以用上一根，仍不能用正在形成的这根
    assert e.latest_closed_5m(open_ms + 40_000) == open_ms - TF_5M_MS
    # 开盘 400s：这根本身已收盘 100s，才轮到用它
    assert e.latest_closed_5m(open_ms + 400_000) == open_ms


# ---------- 历史刷新 ----------
def test_history_refresh_only_advances_on_success(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    calls: list[int] = []
    e.load_history = lambda as_of_ms=None: (calls.append(as_of_ms), False)[1]   # 一直失败
    e._history_ts = 0
    e.on_5m_close(NOW)
    assert calls, "到点必须尝试刷新"
    assert e._history_ts != NOW, "刷新失败不得推进成功时间戳"
    log = (tmp_path / "v2" / "rules_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(log[-1])
    assert row["history_refresh"]["attempted"] is True
    assert row["history_refresh"]["ok"] is False


def test_history_refresh_advances_on_success(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    e._history_ts = 0
    e.on_5m_close(NOW)
    assert e._history_ts == NOW


def test_history_refresh_is_throttled(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    calls: list[int] = []
    e.load_history = lambda as_of_ms=None: (calls.append(as_of_ms), True)[1]
    e.on_5m_close(NOW)                       # 首次：刷新
    e.on_5m_close(NOW + 60_000)              # 1 分钟内：节流
    assert len(calls) == 1
    e.on_5m_close(NOW + 320_000)             # 超过 5 分钟：再刷
    assert len(calls) == 2


def test_nine_hours_of_clocks_advance_real_bars(tmp_path: Path) -> None:
    """9 小时验收：走真实入口 step_once，看每轮吃到的 K 线是否真的往前推。

    不是「函数被调用 108 次」——那只能证明定时器没死。这里校验：
    每次决策基于的 K 线 open 递增、相邻间隔恰好 5 分钟、刷新次数与成功次数一致。
    """
    e = _engine(tmp_path, deepseek=CountingProvider())
    loads: list[int] = []

    def load(as_of_ms=None):
        loads.append(as_of_ms)
        return True

    e.load_history = load
    bars: list[int] = []
    advances: list[int] = []

    for minute in range(0, 9 * 60, 5):                 # 每 5 分钟推一次时钟
        now = NOW + minute * 60_000
        e.feed.seeded_until_ms = now                   # 行情随时钟前进
        before = e._last_bar
        out = e.step_once(now)
        if out and out.get("rules_5m"):
            row = out["rules_5m"]
            bars.append(row["trigger_bar_ms"])
            assert row["closed_bar_ms"] == row["trigger_bar_ms"] + TF_5M_MS
            assert row["closed_bar_ms"] <= now - e.config.settle_ms, "触发 K 线必须已收盘"
            if e._last_bar != before:
                advances.append(e._last_bar)

    assert len(bars) >= 100, f"9 小时只做了 {len(bars)} 轮决策"
    assert len(set(bars)) == len(bars), "不得对同一根 K 线重复决策"
    assert bars == sorted(bars), "K 线时间只能前进"
    gaps = {b - a for a, b in zip(bars, bars[1:])}
    assert gaps == {5 * 60_000}, f"相邻决策的 K 线间隔必须是 5 分钟，实测 {gaps}"
    assert len(loads) >= 100, f"9 小时只刷新了 {len(loads)} 次历史（应约 108 次）"


def test_last_bar_is_the_trigger_bar_not_as_of(tmp_path: Path) -> None:
    """回归：_last_bar 存 as_of 会让第二根之后永远不触发（我的实现曾踩过）。"""
    e = _engine(tmp_path, deepseek=CountingProvider())
    out = e.on_5m_close(NOW, bar_open_ms=NOW - 400_000)
    assert out
    assert e._last_bar == NOW - 400_000
    assert e._last_bar != NOW


def test_step_once_fires_once_per_closed_bar(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    fired = []
    orig = e.on_5m_close

    def spy(ts, bar_open_ms=None):
        fired.append(bar_open_ms)
        return orig(ts, bar_open_ms=bar_open_ms)     # 必须真的执行，_last_bar 才会推进

    e.on_tick = lambda *a, **k: []
    e.on_5m_close = spy                              # type: ignore[method-assign]
    bar = (NOW // 300_000) * 300_000
    # 同一根 K 线内多轮轮询：只允许触发一次
    for delta in (400_000, 401_000, 500_000, 650_000):
        e.step_once(bar + delta)
    assert len(fired) == 1, f"同一根收盘 K 线触发 {len(fired)} 次"
    # 下一根收盘后必须能继续触发（行情也要跟着前进，否则数据还停在原地）
    for delta in (1_000_000, 1_100_000):
        e.feed.seeded_until_ms = bar + delta
        e.step_once(bar + delta)
    assert len(fired) == 2, "第二根 K 线没有触发——引擎会静默停摆"
    assert fired[1] > fired[0]
    e.on_5m_close = orig                             # type: ignore[method-assign]


# ---------- 运行循环 ----------
def test_run_triggers_on_new_closed_bar_only(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    rounds: list[int] = []
    orig = e.on_5m_close

    def spy(ts):
        rounds.append(ts)
        return orig(ts)

    e.on_5m_close = spy                     # type: ignore[method-assign]
    e.run(minutes=0.02, poll_s=0.0)          # 极短跑，时钟几乎不动
    assert len(rounds) <= 1, "同一根已收盘 K 线不得重复决策"

# ---------- Demo 镜像 ----------
class _FakeExecutor:
    def __init__(self, ok: bool = True, raise_on_open: bool = False):
        self.ok = ok
        self.raise_on_open = raise_on_open
        self.opened: list = []
        self.closed: list = []

    def open(self, intent, notional):
        if self.raise_on_open:
            raise RuntimeError("adapter blew up")
        from research.execution.adapter import ExecutionResult
        self.opened.append((intent.intent_id, notional))
        return ExecutionResult(self.ok, "protected" if self.ok else "entry", intent.intent_id,
                               qty=D("0.001"), entry_price=intent.entry_ref_price,
                               error="" if self.ok else "boom")

    def close(self, intent, qty, reason=""):
        from research.execution.adapter import ExecutionResult
        self.closed.append((intent.intent_id, reason))
        return ExecutionResult(True, "close", intent.intent_id, qty=qty)


def test_no_executor_means_no_exchange_orders(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.on_5m_close(NOW)
    assert e.demo_book is None and e.executor is None


def test_executor_mirrors_open_only_for_demo_book(tmp_path: Path) -> None:
    ex = _FakeExecutor()
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    assert len(ex.opened) == 1, "只镜像指定的那本账"
    log = json.loads((tmp_path / "v2" / "deepseek_5m_decisions.jsonl")
                     .read_text(encoding="utf-8").splitlines()[-1])
    assert log["entry"]["demo"]["ok"] is True


def test_executor_close_is_called_on_stop(tmp_path: Path) -> None:
    ex = _FakeExecutor()
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    e.on_tick(float(Decimal(pos["stop_price"])) - 1.0, NOW + 1000)
    assert [c[1] for c in ex.closed] == ["STOP_LOSS"]


def test_executor_failure_does_not_lose_local_fill(tmp_path: Path) -> None:
    ex = _FakeExecutor(raise_on_open=True)
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    assert e.books["deepseek_5m"].open_positions(), "交易所失败不能丢掉本地成交"
    assert any("adapter blew up" in x for x in e.errors)


# ---------- Demo 镜像 ----------
class _FakeExecutor:
    def __init__(self, ok: bool = True, raise_on_open: bool = False):
        self.ok = ok
        self.raise_on_open = raise_on_open
        self.opened: list = []
        self.closed: list = []

    def open(self, intent, notional):
        if self.raise_on_open:
            raise RuntimeError("adapter blew up")
        from research.execution.adapter import ExecutionResult
        self.opened.append((intent.intent_id, notional))
        return ExecutionResult(self.ok, "protected" if self.ok else "entry", intent.intent_id,
                               qty=D("0.001"), entry_price=intent.entry_ref_price,
                               error="" if self.ok else "boom")

    def close(self, intent, qty, reason=""):
        from research.execution.adapter import ExecutionResult
        self.closed.append((intent.intent_id, reason))
        return ExecutionResult(True, "close", intent.intent_id, qty=qty)


def test_no_executor_means_no_exchange_orders(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.on_5m_close(NOW)
    assert e.demo_book is None and e.executor is None


def test_executor_mirrors_open_only_for_demo_book(tmp_path: Path) -> None:
    ex = _FakeExecutor()
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    assert len(ex.opened) == 1, "只镜像指定的那本账"
    log = json.loads((tmp_path / "v2" / "deepseek_5m_decisions.jsonl")
                     .read_text(encoding="utf-8").splitlines()[-1])
    assert log["entry"]["demo"]["ok"] is True


def test_executor_close_is_called_on_stop(tmp_path: Path) -> None:
    ex = _FakeExecutor()
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    e.on_tick(float(Decimal(pos["stop_price"])) - 1.0, NOW + 1000)
    assert [c[1] for c in ex.closed] == ["STOP_LOSS"]


def test_executor_failure_does_not_lose_local_fill(tmp_path: Path) -> None:
    ex = _FakeExecutor(raise_on_open=True)
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    assert e.books["deepseek_5m"].open_positions(), "交易所失败不能丢掉本地成交"
    assert any("adapter blew up" in x for x in e.errors)


# ---------- 镜像失败不得登记为已有仓位 ----------
def test_mirror_failure_does_not_register_position(tmp_path: Path) -> None:
    from research.execution.adapter import ExecutionResult

    class FailingExecutor:
        def open(self, intent, notional):
            return ExecutionResult(False, "entry", intent.intent_id,
                                   error="TimeoutError: The read operation timed out")

        def close(self, intent, qty, reason):  # pragma: no cover - 不该被调用
            raise AssertionError("镜像没建立，不该平仓")

        def reconcile(self):
            return {"ok": True, "diffs": []}

    ex = FailingExecutor()
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    assert e._mirrored == {}, "镜像失败绝不能登记成已有仓位"
    log = json.loads((tmp_path / "v2" / "deepseek_5m_decisions.jsonl")
                     .read_text(encoding="utf-8").splitlines()[-1])
    assert log["entry"]["demo"]["ok"] is False
    assert e.books["deepseek_5m"].open_positions(), "本地成交必须保留"


def test_mirror_halts_after_uncertain_failure(tmp_path: Path) -> None:
    from research.execution.adapter import ExecutionResult

    class Exec:
        def __init__(self):
            self.calls = 0

        def open(self, intent, notional):
            self.calls += 1
            return ExecutionResult(False, "probe", intent.intent_id,
                                   error="无法确认订单是否已存在（查询失败），拒绝下单以免重复")

        def close(self, intent, qty, reason):  # pragma: no cover
            raise AssertionError("不该平仓")

        def reconcile(self):
            return {"ok": True, "diffs": []}

    ex = Exec()
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.executor, e.demo_book = ex, "deepseek_5m"
    e.on_5m_close(NOW)
    assert e.mirror_halted is True
    e.on_5m_close(NOW + 300_000)
    assert ex.calls == 1, "已经 halt 就不该再向交易所发单"
