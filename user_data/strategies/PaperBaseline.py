"""Freqtrade 1h spot-only dry-run baseline, never authorizes live orders."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from pandas import DataFrame
from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade


class PaperBaseline(IStrategy):
    INTERFACE_VERSION = 3
    can_short = False
    timeframe = "1h"
    startup_candle_count = 60
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    position_adjustment_enable = False
    stoploss = -0.03
    # Effectively disable ROI profit-taking; trend exit and hard stop control exits.
    minimal_roi = {"0": 10.0}
    # Freqtrade only uses the returned custom price as the ORDER's limit price. With a
    # market entry the order still fills at the market average, so the 5 bps adverse
    # entry price never reaches the ledger (verified: ft_price 1.0005x market, average
    # == market). Both legs therefore use marketable limits: buy 5 bps above, sell 5 bps
    # below the proposed rate, which fill immediately but book the adverse price.
    # This is a paper fill model, not a live-order setup.
    order_types = {"entry": "limit", "exit": "limit", "stoploss": "limit", "stoploss_on_exchange": False}
    order_time_in_force = {"entry": "gtc", "exit": "gtc"}
    SLIPPAGE_BPS = 5
    SLIPPAGE_RATE = SLIPPAGE_BPS / 10_000.0
    APPROVED_PAIRS = ("BTC/USDT", "ETH/USDT")

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {"method": "StoplossGuard", "lookback_period_candles": 24,
             "trade_limit": 2, "stop_duration_candles": 24, "only_per_pair": False},
            {"method": "MaxDrawdown", "lookback_period_candles": 72,
             "trade_limit": 4, "stop_duration_candles": 24,
             "calculation_mode": "equity", "max_allowed_drawdown": 0.05},
        ]

    def require_paper_mode(self) -> None:
        cfg = self.config
        exchange = cfg.get("exchange", {})
        if (cfg.get("dry_run") is not True or cfg.get("trading_mode", "spot") != "spot"
                or cfg.get("margin_mode") not in (None, "")
                or any(exchange.get(k) for k in ("key", "secret", "password"))
                or cfg.get("api_server", {}).get("enabled", False)
                or cfg.get("force_entry_enable", False)):
            raise RuntimeError("PAPER-ONLY safety violation: live mode, keys or remote order API")
        if (exchange.get("name") != "kraken"
                or exchange.get("pair_whitelist") != ["BTC/USDT", "ETH/USDT"]
                or cfg.get("dry_run_wallet") != 1000):
            raise RuntimeError("PAPER-ONLY exchange, pairlist or dry wallet was changed")
        if cfg.get("max_open_trades") != 2 or cfg.get("stake_amount") != 100:
            raise RuntimeError("PAPER-ONLY sizing configuration changed")

    def bot_start(self, **kwargs) -> None:
        self.require_paper_mode()

    @staticmethod
    def _utc_ms(value: datetime | None = None) -> int | None:
        if value is None:
            value = datetime.now(timezone.utc)
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.astimezone(timezone.utc).timestamp() * 1000)

    def _write_local_status(self, filename: str, payload: dict) -> None:
        """Atomically publish non-authoritative local telemetry; failures never affect trading."""
        try:
            userdir_value = self.config.get("user_data_dir")
            if not userdir_value:
                return
            userdir = Path(str(userdir_value))
            userdir.mkdir(parents=True, exist_ok=True)
            target = userdir / filename
            if target.is_symlink():
                return
            fd, temp_name = tempfile.mkstemp(prefix=".paper-status-", suffix=".tmp", dir=str(userdir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_name, 0o600)
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except (OSError, TypeError, ValueError):
            return

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """Write a per-account heartbeat so no-trade can be distinguished from a dead loop."""
        mode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if self.config.get("dry_run") is not True or mode not in (None, "dry_run"):
            return
        stamp = self._utc_ms(current_time)
        if stamp is None:
            return
        self._write_local_status("strategy_heartbeat.json", {
            "schema_version": 1,
            "source": self.__class__.__name__,
            "mode": "dry_run",
            "status": "running",
            "timeframe": self.timeframe,
            "last_loop_at_ms": stamp,
        })

    def publish_pair_status(self, pair: str, action: str, reason: str, candle_date=None) -> None:
        """Publish the last real strategy evaluation for one whitelisted pair."""
        mode = getattr(self.config.get("runmode"), "value", self.config.get("runmode"))
        if (self.config.get("dry_run") is not True or mode not in (None, "dry_run")
                or pair not in self.APPROVED_PAIRS or action not in ("BUY", "HOLD")):
            return
        candle_ms = self._utc_ms(candle_date) if candle_date is not None else None
        self._write_local_status(f"strategy_status_{pair.replace('/', '_')}.json", {
            "schema_version": 1,
            "strategy": self.__class__.__name__,
            "mode": "dry_run",
            "pair": pair,
            "timeframe": self.timeframe,
            "action": action,
            "reason": str(reason)[:240],
            "candle_open_ms": candle_ms,
            "observed_at_ms": self._utc_ms(),
        })

    def custom_entry_price(
        self, pair: str, trade: Trade | None, current_time: datetime,
        proposed_rate: float, entry_tag: str | None, side: str, **kwargs,
    ) -> float:
        """Model 5 bps adverse entry slippage on approved long spot pairs."""
        try:
            rate = float(proposed_rate)
            if pair not in self.APPROVED_PAIRS or side != "long" or not math.isfinite(rate) or rate <= 0:
                return proposed_rate
            return rate * (1.0 + self.SLIPPAGE_RATE)
        except (TypeError, ValueError, OverflowError):
            return proposed_rate

    def custom_exit_price(
        self, pair: str, trade: Trade, current_time: datetime,
        proposed_rate: float, current_profit: float, exit_tag: str | None, **kwargs,
    ) -> float:
        """Model 5 bps adverse exit slippage; Freqtrade invokes this for limit exits."""
        try:
            rate = float(proposed_rate)
            if pair not in self.APPROVED_PAIRS or not math.isfinite(rate) or rate <= 0:
                return proposed_rate
            if bool(getattr(trade, "is_short", False)):
                return proposed_rate
            return rate * (1.0 - self.SLIPPAGE_RATE)
        except (TypeError, ValueError, OverflowError):
            return proposed_rate

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["sma20"] = df["close"].rolling(window=20, min_periods=20).mean()
        df["sma50"] = df["close"].rolling(window=50, min_periods=50).mean()
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["enter_long"] = 0
        condition = (df["sma20"] > df["sma50"]) & (df["close"] > df["sma20"]) & (df["volume"] > 0)
        df.loc[condition, "enter_long"] = 1
        pair = metadata.get("pair")
        if not df.empty and pair in self.APPROVED_PAIRS:
            last = df.iloc[-1]
            try:
                sma20, sma50, close, volume = (float(last[k]) for k in ("sma20", "sma50", "close", "volume"))
                ready = all(math.isfinite(v) for v in (sma20, sma50, close, volume))
                if not ready:
                    reason = "指标预热中或数据无效"
                elif volume <= 0:
                    reason = "当前 K 线成交量为 0"
                elif sma20 <= sma50:
                    reason = "SMA20 尚未高于 SMA50"
                elif close <= sma20:
                    reason = "收盘价尚未站上 SMA20"
                else:
                    reason = "基线趋势条件满足"
                action = "BUY" if bool(condition.iloc[-1]) else "HOLD"
                self.publish_pair_status(pair, action, reason, last.get("date"))
            except (KeyError, TypeError, ValueError, OverflowError):
                self.publish_pair_status(pair, "HOLD", "指标状态无法计算", None)
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe.copy()
        df["exit_long"] = 0
        df.loc[((df["sma20"] < df["sma50"]) | (df["close"] < df["sma20"]))
               & (df["volume"] > 0), "exit_long"] = 1
        return df

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        """Max 10% (with extra absolute 100 USDT ceiling) per first entry."""
        try:
            self.require_paper_mode()
            if side != "long" or not self.wallets:
                return 0.0
            equity_proxy = float(self.wallets.get_total_stake_amount())
            if not math.isfinite(equity_proxy) or equity_proxy <= 0:
                return 0.0
            amount = min(100.0, 0.10 * equity_proxy, proposed_stake, max_stake)
            if min_stake is not None and amount < min_stake:
                return 0.0
            return max(0.0, amount)
        except Exception:
            # Never fall back to proposed_stake after any sizing uncertainty.
            return 0.0

    def daily_realized_loss_ok(self, current_time: datetime) -> bool:
        """Daily realized-loss cutoff, NOT full unrealized account-equity drawdown."""
        try:
            now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            day = now.astimezone(timezone.utc).date()
            realized = 0.0
            for trade in Trade.get_trades_proxy(is_open=False):
                dt = getattr(trade, "close_date_utc", None)
                if dt is None:
                    dt = getattr(trade, "close_date", None)
                if not isinstance(dt, datetime):
                    return False
                dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                if dt.astimezone(timezone.utc).date() == day:
                    pnl = float(getattr(trade, "close_profit_abs", float("nan")))
                    if not math.isfinite(pnl):
                        return False
                    realized += pnl
            return realized > -20.0  # 2% of nominal 1000 USDT initial dry wallet.
        except Exception:
            return False

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time: datetime,
                            entry_tag: str | None, side: str, **kwargs) -> bool:
        try:
            self.require_paper_mode()
            allowed = (side == "long" and pair in self.APPROVED_PAIRS
                       and self.daily_realized_loss_ok(current_time))
            if not allowed:
                self.publish_pair_status(pair, "HOLD", "最终入场风控未放行", None)
            return allowed
        except Exception:
            self.publish_pair_status(pair, "HOLD", "最终入场检查异常，按 fail-closed 处理", None)
            return False
