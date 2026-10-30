"""The strategy spec: everything the engine needs, declared up front.

A spec is a small JSON document. It exists so that the falsification criteria
are written BEFORE anyone sees a single number -- the same discipline the
research arms used (`_arm` fields pinned in config before the run). A spec
without `falsify_if` is accepted and measured in full, but the verdict comes
back NOT_FALSIFIED with the criteria table saying "no rules were declared" --
an audit without pre-declared kill criteria is a measurement, not a test.

Example:

    {
      "name": "rsi-dip-btc-1h",
      "hypothesis": "BTC 1h RSI<30 后的均值回复",
      "symbol": "BTCUSDT", "interval": "1h", "venue": "futures",
      "fee_rate": 0.0004, "fill_model": "taker", "funding": true,
      "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
      "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30},
      "control": {"enabled": true, "n_random": 200, "seed": 42},
      "walk_forward": {"enabled": true, "folds": 3},
      "param_grid": {"axes": {"take_profit": [0.02, 0.03, 0.04],
                              "stop_loss": [-0.06, -0.08, -0.10]}},
      "fee_multiples": [1, 2, 5],
      "falsify_if": {
        "control_lift_ci_includes_zero": true,
        "net_negative_at_fee_multiple": 2,
        "worst_case_net_negative": false,
        "grid_positive_ratio_min": 0.5,
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
# Exit parameters the sensitivity grid may move. Signal parameters are NOT
# gridable here: changing them changes the entry set itself, which is a
# different experiment (and a different control group), not a sensitivity.
GRIDABLE_PARAMS = ("take_profit", "stop_loss", "timeout_bars", "trailing")


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
class WalkForwardSpec:
    """Time-split the window into N folds; the last fold is out-of-sample.

    Declaring the block is the intent (same convention as `control`). The
    industry's number-one cause of backtest->live decay is in-sample overfit
    (median -14.3pp); a strategy that only works on the bars its parameters
    were chosen on is a story, not an edge. The engine cannot know how the
    parameters were chosen -- that provenance is disclosed as a limit, not
    assumed away.
    """

    enabled: bool = False
    folds: int = 3

    def validate(self) -> None:
        if self.folds < 2:
            raise SpecError("walk_forward.folds 至少为 2（1 折没有样本外段，walk-forward 失去意义）")


@dataclass
class ParamGridSpec:
    """Grid the exit parameters and replay EVERY cell: an edge that exists
    only at one exact take-profit/stop-loss combination is a curve fit, not
    an edge.

    The grid values are declared in the spec BEFORE anyone sees a number --
    the same discipline as `control` and `walk_forward`; a grid chosen after
    reading the report is marketing. Each cell replays the SAME entries
    under the SAME headline convention (the spec's worst/best-case flag,
    base fee); only the declared axes move. At most two axes: the report
    renders the grid as a two-dimensional heat table, and a three-axis
    product is a reading problem, not a measurement problem.
    """

    enabled: bool = False
    axes: dict[str, list] = field(default_factory=dict)
    max_cells: int = 64

    def validate(self) -> None:
        if not self.axes:
            raise SpecError(
                "param_grid 至少要声明一个参数轴（如 \"take_profit\": [0.02, 0.03]）"
            )
        if len(self.axes) > 2:
            raise SpecError(
                f"param_grid 最多支持 2 个参数轴（热力表是二维的），得到 {len(self.axes)} 个："
                f"{list(self.axes)}——想扫更多维度就分多次运行，每次的规则都事前声明"
            )
        total = 1
        for key, values in self.axes.items():
            if key not in GRIDABLE_PARAMS:
                raise SpecError(f"param_grid 参数名必须是 {GRIDABLE_PARAMS} 之一，得到 {key!r}")
            if len(values) < 2:
                raise SpecError(f"param_grid 轴 {key!r} 至少 2 个值（1 个值不成网格）")
            if len(set(values)) != len(values):
                raise SpecError(f"param_grid 轴 {key!r} 的值有重复：{values}")
            if key in ("take_profit", "trailing") and any(v <= 0 for v in values):
                raise SpecError(f"param_grid.{key} 的值必须是正数，得到 {values}")
            if key == "stop_loss" and any(v >= 0 for v in values):
                raise SpecError(f"param_grid.stop_loss 的值必须是负数，得到 {values}")
            if key == "timeout_bars" and any(int(v) != v or v < 1 for v in values):
                raise SpecError(f"param_grid.timeout_bars 的值必须是 ≥1 的整数，得到 {values}")
            total *= len(values)
        if total > self.max_cells:
            raise SpecError(
                f"param_grid 共 {total} 格，超过 max_cells={self.max_cells}——"
                f"网格是组合爆炸的，先缩小轴范围，或想清楚再看再调大 max_cells"
            )


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
    walk_forward: WalkForwardSpec = field(default_factory=WalkForwardSpec)
    param_grid: ParamGridSpec = field(default_factory=ParamGridSpec)
    fee_multiples: tuple[float, ...] = (1.0, 2.0, 5.0)
    falsify_if: dict = field(default_factory=dict)
    hypothesis: str = ""
    source: str = ""
    # Free-text port notes for strategies ported from somewhere else: what was
    # replicated verbatim, what was approximated, what was NOT ported. Rendered
    # as section 0 so a regenerated report never loses them (hand-editing the
    # markdown after a run is how artifacts drift from the engine).
    port_notes: str = ""

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
        min_ratio = self.falsify_if.get("grid_positive_ratio_min")
        if min_ratio is not None:
            try:
                min_ratio = float(min_ratio)
            except (TypeError, ValueError):
                raise SpecError("falsify_if.grid_positive_ratio_min 必须是 (0, 1] 内的数字")
            if not 0 < min_ratio <= 1:
                raise SpecError(
                    f"falsify_if.grid_positive_ratio_min 必须在 (0, 1] 内，得到 {min_ratio}"
                )
        self.exit.validate()
        self.signals.validate()
        self.control.validate()
        self.walk_forward.validate()
        # an absent block is "not declared"; a declared block must be valid
        if self.param_grid.enabled:
            self.param_grid.validate()

    # -- (de)serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "source": self.source,
            "port_notes": self.port_notes,
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
            "walk_forward": {
                "enabled": self.walk_forward.enabled,
                "folds": self.walk_forward.folds,
            },
            "param_grid": {
                "enabled": self.param_grid.enabled,
                "axes": {k: list(v) for k, v in self.param_grid.axes.items()},
                "max_cells": self.param_grid.max_cells,
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
        wf_d = d.get("walk_forward") or {}
        pg_d = d.get("param_grid") or {}
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
            walk_forward=WalkForwardSpec(
                enabled=bool(wf_d.get("enabled", True)) if "walk_forward" in d else False,
                folds=int(wf_d.get("folds", 3)),
            ),
            param_grid=ParamGridSpec(
                enabled=bool(pg_d.get("enabled", True)) if "param_grid" in d else False,
                axes=_grid_axes(pg_d.get("axes") or {}),
                max_cells=int(pg_d.get("max_cells", 64)),
            ),
            fee_multiples=tuple(float(m) for m in d.get("fee_multiples", (1.0, 2.0, 5.0))),
            falsify_if=dict(d.get("falsify_if") or {}),
            hypothesis=str(d.get("hypothesis", "")),
            source=str(d.get("source", "")),
            port_notes=str(d.get("port_notes", "")),
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


def _grid_axes(raw: dict) -> dict:
    """Coerce declared grid axis values to their parameter's type (bar counts
    are ints; the rest are fractions). A bad value is a SpecError naming the
    axis, never a silent coercion."""
    axes: dict = {}
    for key, values in raw.items():
        if not isinstance(values, (list, tuple)) or not values:
            raise SpecError(f"param_grid 轴 {key!r} 必须是非空数组，得到 {values!r}")
        if key == "timeout_bars":
            out = []
            for v in values:
                f = _opt_float(v, f"param_grid.{key}")
                if f is None or int(f) != f or f < 1:
                    raise SpecError(
                        f"param_grid.timeout_bars 的值必须是 ≥1 的整数，得到 {v!r}"
                    )
                out.append(int(f))
            axes[key] = out
        else:
            axes[key] = [_opt_float(v, f"param_grid.{key}") for v in values]
            if any(v is None for v in axes[key]):
                raise SpecError(f"param_grid 轴 {key!r} 含有 null 值：{values}")
    return axes
