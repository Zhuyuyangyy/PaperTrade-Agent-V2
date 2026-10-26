"""Risk-first Agent overlay: only vetoes baseline entries, never issues orders."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import sys

from pandas import DataFrame

sys.path.insert(0, str(Path(__file__).resolve().parent))
from PaperBaseline import PaperBaseline
from signal_gate import approved_from_file, candle_open_ms


class PaperHybrid(PaperBaseline):
    # Research intentionally waits after candle close; re-evaluate until its signal arrives.
    process_only_new_candles = False
    SIGNAL_DIR = Path(os.environ.get("PAPER_SIGNAL_DIR", "/freqtrade/user_data/signals"))

    def bot_start(self, **kwargs) -> None:
        super().bot_start(**kwargs)
        # File-backed live research is not a historically reproducible backtest input.
        mode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if mode is not None and mode != "dry_run":
            raise RuntimeError("Hybrid runs ONLY in dry-run; no live or historical backtests")

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = super().populate_entry_trend(dataframe, metadata)
        if df.empty:
            return df
        # Never allow stale/mismatched, demo or missing external advice to create an entry.
        df["enter_long"] = 0
        pair = metadata.get("pair")
        try:
            last = df.iloc[-1]
            last_date = candle_open_ms(last["date"])
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            is_valid, gate_reason = approved_from_file(self.SIGNAL_DIR, pair, last_date, now_ms)
            baseline_ok = (last["sma20"] > last["sma50"] and last["close"] > last["sma20"]
                           and last["volume"] > 0)
            if baseline_ok and is_valid:
                df.at[df.index[-1], "enter_long"] = 1
                df.at[df.index[-1], "enter_tag"] = "agent_veto_pass"
                reason = "基线条件满足且 Agent 信号通过"
                action = "BUY"
            elif not baseline_ok:
                reason = "基线 SMA 条件未满足"
                action = "HOLD"
            else:
                reason = f"Agent 信号未通过：{gate_reason}"
                action = "HOLD"
            self.publish_pair_status(pair, action, reason, last.get("date"))
        except (KeyError, IndexError, ValueError, TypeError):
            self.publish_pair_status(pair, "HOLD", "信号或指标校验异常，按 fail-closed 处理", None)
        return df

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time: datetime,
                            entry_tag: str | None, side: str, **kwargs) -> bool:
        if not super().confirm_trade_entry(pair, order_type, amount, rate, time_in_force,
                                           current_time, entry_tag, side, **kwargs):
            return False
        try:
            # Recheck the exact last analyzed candle at the final order-authorization point.
            df, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
            if df is None or df.empty:
                self.publish_pair_status(pair, "HOLD", "最终审核缺少已分析 K 线", None)
                return False
            last = df.iloc[-1]
            candle_ms = candle_open_ms(last["date"])
            now_ms = int(current_time.replace(tzinfo=timezone.utc).timestamp() * 1000) if current_time.tzinfo is None else int(current_time.timestamp() * 1000)
            allowed, gate_reason = approved_from_file(self.SIGNAL_DIR, pair, candle_ms, now_ms)
            self.publish_pair_status(pair, "BUY" if allowed else "HOLD",
                                      "最终入场审核通过" if allowed else f"最终审核拒绝：{gate_reason}",
                                      last.get("date"))
            return allowed
        except Exception:
            self.publish_pair_status(pair, "HOLD", "最终审核异常，按 fail-closed 处理", None)
            return False
