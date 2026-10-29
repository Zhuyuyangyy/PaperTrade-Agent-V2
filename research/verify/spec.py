"""The strategy spec: everything the engine needs, declared up front.

A spec is a small JSON document. It exists so that the falsification criteria
are written BEFORE anyone sees a single number -- the same discipline the
research arms used (`_arm` fields pinned in config before the run). A spec
without `falsify_if` is accepted, but the report will say the verdict is
unfalsifiable, which is the truth.

Example:

    {
      "name": "rsi-dip-btc-1h",
      "hypothesis": "BTC 1h RSI<30 后的均值回复",
      "symbol": "BTCUSDT", "interval": "1h", "venue": "futures",
      "fee_rate": 0.0004, "fill_model": "taker", "funding": true,
      "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
      "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30},
      "control": {"enabled": true, "n_random": 200, "seed": 42},
      "fee_multiples": [1, 2, 5],
      "falsify_if": {
        "control_lift_ci_includes_zero": true,
        "net_negative_at_fee_multiple": 2,
        "worst_case_net_negative": false,
        "min_fills": 30
      }
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class SpecError(ValueError):
    """Raised when a spec is malformed. The message says which field and why."""


VALID_FILL_MODELS = ("taker", "maker")
VALID_RULES = ("rsi_below", "rsi_above")
VALID_SIGNAL_KINDS = ("csv", "random", "rule")


@dataclass
class ExitSpec:
    take_profit: float | None = None
    stop_loss: float | None = None
    timeout_bars: int | None = None
    trailing: float | None = None
    trailing_offset: float | None = None
    exit_signals_path: str | None = None   # CSV(t[,reason]): bar-close exit signals

    def validate(self) -> None:
        if self.take_profit is not None and self.take_profit <= 0:
            raise SpecError("exit.take_profit 必须是正数（小数，如 0.03 表示 +3%）")
        if self.stop_loss is not None and self.stop_loss >= 0:
            raise SpecError("exit.stop_loss 必须是负数（如 -0.08 表示 -8%）")
        if self.timeout_bars is not None and self.timeout_bars < 1:
            raise SpecError("exit.timeout_bars 至少为 1")
        if all(v is None for v in (self.take_profit, self.stop_loss, self.timeout_bars,
                                   self.trailing, self.exit_signals_path)):
            raise SpecError("exit 至少要声明一个退出条件（take_profit / stop_loss / timeout_bars / trailing / exit_signals_path）")


@dataclass
class SignalSpec:
    kind: str = "rule"
    path: str | None = None
    side: str = "long"
    n_random: int = 200
    seed: int = 42
    rule: str | None = None
    period: int = 14
    threshold: float = 30.0

    def validate(self) -> None:
        if self.kind not in VALID_SIGNAL_KINDS:
            raise SpecError(f"signals.kind 必须是 {VALID_SIGNAL_KINDS} 之一，得到 {self.kind!r}")
        if self.kind == "csv" and not self.path:
            raise SpecError("signals.kind='csv' 时必须提供 signals.path")
        if self.kind == "rule":
            if self.rule not in VALID_RULES:
                raise SpecError(f"signals.rule 必须是 {VALID_RULES} 之一，得到 {self.rule!r}")
            if self.period < 2:
                raise SpecError("signals.period 至少为 2")
        if self.side not in ("long", "short"):
            raise SpecError("signals.side 必须是 'long' 或 'short'")


@dataclass
class ControlSpec:
    enabled: bool = True
    n_random: int = 200
    seed: int = 42

    def validate(self) -> None:
        if self.n_random < 10:
            raise SpecError("control.n_random 至少为 10，否则 bootstrap 没有意义")


@dataclass
class Spec:
    name: str
    symbol: str
    interval: str
    venue: str = "spot"
    fee_rate: float = 0.0004
    fill_model: str = "taker"
    worst_case: bool = True
    funding: bool = False
    exit: ExitSpec = field(default_factory=ExitSpec)
    signals: SignalSpec = field(default_factory=SignalSpec)
    control: ControlSpec = field(default_factory=ControlSpec)
    fee_multiples: tuple[float, ...] = (1.0, 2.0, 5.0)
    falsify_if: dict = field(default_factory=dict)
    hypothesis: str = ""
    source: str = ""

    def validate(self) -> None:
        if not self.name:
            raise SpecError("name 不能为空")
        if not self.symbol:
            raise SpecError("symbol 不能为空")
        if self.fill_model not in VALID_FILL_MODELS:
            raise SpecError(f"fill_model 必须是 {VALID_FILL_MODELS} 之一，得到 {self.fill_model!r}")
        if self.venue not in ("spot", "futures"):
            raise SpecError("venue 必须是 'spot' 或 'futures'")
        if not 0 <= self.fee_rate < 0.01:
            raise SpecError("fee_rate 是单边小数费率（如 0.0004 = 4bps），必须在 [0, 0.01) 内")
        if self.funding and self.venue != "futures":
            raise SpecError("funding 只对 futures 场所开放（现货没有资金费）")
        if not self.fee_multiples:
            raise SpecError("fee_multiples 不能为空")
        if 1.0 not in self.fee_multiples:
            raise SpecError("fee_multiples 必须包含 1（基准成本），否则敏感性表没有锚点")
        self.exit.validate()
        self.signals.validate()
        self.control.validate()

    # -- (de)serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "source": self.source,
            "symbol": self.symbol,
            "interval": self.interval,
            "venue": self.venue,
            "fee_rate": self.fee_rate,
            "fill_model": self.fill_model,
            "worst_case": self.worst_case,
            "funding": self.funding,
            "exit": {
                "take_profit": self.exit.take_profit,
                "stop_loss": self.exit.stop_loss,
                "timeout_bars": self.exit.timeout_bars,
                "trailing": self.exit.trailing,
                "trailing_offset": self.exit.trailing_offset,
                "exit_signals_path": self.exit.exit_signals_path,
            },
            "signals": {
                "kind": self.signals.kind,
                "path": self.signals.path,
                "side": self.signals.side,
                "n_random": self.signals.n_random,
                "seed": self.signals.seed,
                "rule": self.signals.rule,
                "period": self.signals.period,
                "threshold": self.signals.threshold,
            },
            "control": {
                "enabled": self.control.enabled,
                "n_random": self.control.n_random,
                "seed": self.control.seed,
            },
            "fee_multiples": list(self.fee_multiples),
            "falsify_if": self.falsify_if,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Spec":
        if not isinstance(d, dict):
            raise SpecError("spec 必须是一个 JSON 对象")
        for key in ("name", "symbol", "interval", "exit", "signals"):
            if key not in d:
                raise SpecError(f"spec 缺少必填字段 {key!r}")
        exit_d = d.get("exit") or {}
        sig_d = d.get("signals") or {}
        ctrl_d = d.get("control") or {}
        spec = cls(
            name=str(d["name"]),
            symbol=str(d["symbol"]).upper(),
            interval=str(d["interval"]),
            venue=str(d.get("venue", "spot")),
            fee_rate=float(d.get("fee_rate", 0.0004)),
            fill_model=str(d.get("fill_model", "taker")),
            worst_case=bool(d.get("worst_case", True)),
            funding=bool(d.get("funding", False)),
            exit=ExitSpec(
                take_profit=_opt_float(exit_d.get("take_profit"), "exit.take_profit"),
                stop_loss=_opt_float(exit_d.get("stop_loss"), "exit.stop_loss"),
                timeout_bars=_opt_int(exit_d.get("timeout_bars"), "exit.timeout_bars"),
                trailing=_opt_float(exit_d.get("trailing"), "exit.trailing"),
                trailing_offset=_opt_float(exit_d.get("trailing_offset"), "exit.trailing_offset"),
                exit_signals_path=exit_d.get("exit_signals_path"),
            ),
            signals=SignalSpec(
                kind=str(sig_d.get("kind", "rule")),
                path=sig_d.get("path"),
                side=str(sig_d.get("side", "long")),
                n_random=int(sig_d.get("n_random", 200)),
                seed=int(sig_d.get("seed", 42)),
                rule=sig_d.get("rule"),
                period=int(sig_d.get("period", 14)),
                threshold=float(sig_d.get("threshold", 30.0)),
            ),
            control=ControlSpec(
                enabled=bool(ctrl_d.get("enabled", True)),
                n_random=int(ctrl_d.get("n_random", 200)),
                seed=int(ctrl_d.get("seed", 42)),
            ),
            fee_multiples=tuple(float(m) for m in d.get("fee_multiples", (1.0, 2.0, 5.0))),
            falsify_if=dict(d.get("falsify_if") or {}),
            hypothesis=str(d.get("hypothesis", "")),
            source=str(d.get("source", "")),
        )
        spec.validate()
        return spec

    @classmethod
    def load(cls, path: str | Path) -> "Spec":
        p = Path(path)
        if not p.is_file():
            raise SpecError(f"spec 文件不存在: {p}")
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SpecError(f"spec 不是合法 JSON: {exc}") from exc
        return cls.from_dict(d)


def _opt_float(v, field_name: str) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{field_name} 必须是数字或 null") from exc


def _opt_int(v, field_name: str) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{field_name} 必须是整数或 null") from exc
