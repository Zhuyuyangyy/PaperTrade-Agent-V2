"""Exploratory 15m strategy for faster feedback; excluded from primary A/B results."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import json
import os
from pathlib import Path
import tempfile

from pandas import DataFrame

_STRATEGY_DIR = str(Path(__file__).resolve().parent)
import sys
if _STRATEGY_DIR not in sys.path:
    sys.path.insert(0, _STRATEGY_DIR)
from PaperBaseline import PaperBaseline


class PaperFast(PaperBaseline):
    """A faster paper-only lane: EMA trend entries plus confirmed RSI rebound entries."""
    timeframe = "15m"
    startup_candle_count = 30
    process_only_new_candles = True
    stoploss = -0.02
    minimal_roi = {"0": 0.015}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        close = df["close"]
        df["ema9"] = close.ewm(span=9, min_periods=9, adjust=False).mean()
        df["ema21"] = close.ewm(span=21, min_periods=21, adjust=False).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.where(loss != 0, 100.0)
        rsi = rsi.where(gain != 0, 0.0)
        df["rsi14"] = rsi.fillna(50.0)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["enter_long"] = 0
        trend = (df["ema9"] > df["ema21"]) & (df["close"] > df["ema9"])
        rebound = (df["rsi14"] <= 38) & (df["close"] > df["ema9"]) & (df["close"] > df["close"].shift(1))
        valid = (trend | rebound) & (df["volume"] > 0)
        df.loc[valid, "enter_long"] = 1
        df.loc[trend & valid, "enter_tag"] = "ema9_21_trend"
        df.loc[rebound & ~trend & valid, "enter_tag"] = "rsi_rebound"
        pair = metadata.get("pair")
        if not df.empty and pair in self.APPROVED_PAIRS:
            last = df.iloc[-1]
            try:
                ema9, ema21, rsi14, close, volume = (float(last[k]) for k in ("ema9", "ema21", "rsi14", "close", "volume"))
                ready = all(math.isfinite(v) for v in (ema9, ema21, rsi14, close, volume))
                if not ready:
                    reason = "EMA/RSI 指标预热中或数据无效"
                elif volume <= 0:
                    reason = "当前 K 线成交量为 0"
                elif bool(trend.iloc[-1]):
                    reason = "EMA9 高于 EMA21 且收盘价站上 EMA9"
                elif bool(rebound.iloc[-1]):
                    reason = "RSI 反弹条件满足"
                else:
                    reason = "EMA 趋势与 RSI 反弹条件均未满足"
                self.publish_pair_status(pair, "BUY" if bool(valid.iloc[-1]) else "HOLD", reason, last.get("date"))
            except (KeyError, TypeError, ValueError, OverflowError):
                self.publish_pair_status(pair, "HOLD", "EMA/RSI 状态无法计算", None)
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["exit_long"] = 0
        exit_signal = ((df["ema9"] < df["ema21"]) | (df["close"] < df["ema21"])) & (df["volume"] > 0)
        df.loc[exit_signal, "exit_long"] = 1
        return df

    def bot_start(self, **kwargs) -> None:
        super().bot_start(**kwargs)
        mode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if mode is not None and mode != "dry_run":
            raise RuntimeError("PaperFast runs ONLY in dry-run; no live orders or historical A/B mixing")

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """Publish a tiny local heartbeat so a flat account is visibly alive."""
        super().bot_loop_start(current_time, **kwargs)
        mode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if self.config.get("dry_run") is not True or mode not in (None, "dry_run"):
            return
        userdir = Path(str(self.config.get("user_data_dir") or "/freqtrade/user_data"))
        target = userdir / "fast_heartbeat.json"
        try:
            userdir.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "source": "PaperFast",
                "mode": "dry_run",
                "status": "running",
                "timeframe": self.timeframe,
                "last_loop_at_ms": int(current_time.replace(tzinfo=timezone.utc).timestamp() * 1000)
                if current_time.tzinfo is None else int(current_time.timestamp() * 1000),
            }
            fd, temp_name = tempfile.mkstemp(prefix=".fast-heartbeat-", suffix=".tmp", dir=str(userdir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_name, 0o600)
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except OSError:
            # Telemetry must never block or break the paper strategy.
            return
