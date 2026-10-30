#!/usr/bin/env python3
"""参考实现：把一个 freqtrade 网格策略导成证伪引擎能吃的信号 CSV。

这是"带自己的策略来"的样板——客户侧的活，引擎不碰你的策略代码。
FreqBot 仓库的 GridTradingStrategy（freqbot_config/strategies/GridTradingStrategy.py，
作者默认参数）逐条复刻：

  指标：SMA20 / EMA12 / EMA26 / 布林(典型价, 20, 2σ) / RSI14 / ATR14 / 量MA10
  入场：(close < bb_mid 或 close < sma20*0.998) 且 rsi<45 且 ema12>ema26
        且 bb_percent<0.6 且 0.015<bb_width<0.25 且 atr>close*0.005
        且 volume > 量MA10*0.5
  退出：(close>bb_mid 且 bb_percent>0.7 且 rsi>60 且 volume>量MA10*0.8)
        或 (rsi>70 且 close>sma20)
        或 (bb_width>0.20 且 rsi>55 且 ema12<ema26)

用法（需要 pandas；引擎本身不需要）：

    python export_grid_signals.py --candles klines.csv \
        --out-signals signals.csv --out-exit-signals exit_signals.csv

K 线 CSV 列名随意（date/time/open_time + open/high/low/close/volume 都认），
输出为引擎标准格式。把输出连同 spec 里的 kind='csv' 一起喂给引擎：

    python scripts/verify.py spec.json --candles klines.csv --signals signals.csv --out report.md
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd


def load_ohlcv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    cols = {str(c).strip().lower(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        raise SystemExit(f"K 线 CSV 缺少列 {names[0]}（接受 {names}），实际 {list(df.columns)}")

    out = pd.DataFrame({
        "t": pd.to_datetime(df[pick("t", "date", "time", "timestamp", "open_time")], utc=True),
        "open": df[pick("open")].astype(float),
        "high": df[pick("high")].astype(float),
        "low": df[pick("low")].astype(float),
        "close": df[pick("close")].astype(float),
        "volume": df[pick("volume", "vol")].astype(float) if any(
            n in cols for n in ("volume", "vol")) else 0.0,
    })
    return out.sort_values("t").reset_index(drop=True)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    mid = typical.rolling(20).mean()
    std = typical.rolling(20).std(ddof=0)          # talib/population std, 与 talib 一致
    df["bb_mid"] = mid
    df["bb_upper"] = mid + 2 * std
    df["bb_lower"] = mid - 2 * std
    df["bb_percent"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    df["sma_20"] = df["close"].rolling(20).mean()
    df["ema_12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-12))
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["vol_ma10"] = df["volume"].rolling(10).mean()
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="GridTradingStrategy -> 证伪引擎信号 CSV")
    ap.add_argument("--candles", required=True, help="K 线 CSV（列名宽容）")
    ap.add_argument("--out-signals", required=True, help="输出：入场信号 CSV（date,side）")
    ap.add_argument("--out-exit-signals", help="输出：退出信号 CSV（date,reason）")
    args = ap.parse_args()

    df = add_indicators(load_ohlcv(Path(args.candles)))

    entry = (
        ((df["close"] < df["bb_mid"]) | (df["close"] < df["sma_20"] * 0.998))
        & (df["rsi"] < 45)
        & (df["ema_12"] > df["ema_26"])
        & (df["bb_percent"] < 0.6)
        & (df["bb_width"] > 0.015) & (df["bb_width"] < 0.25)
        & (df["atr"] > df["close"] * 0.005)
        & (df["volume"] > df["vol_ma10"] * 0.5)
    ).fillna(False)

    exit_sig = (
        ((df["close"] > df["bb_mid"]) & (df["bb_percent"] > 0.7)
         & (df["rsi"] > 60) & (df["volume"] > df["vol_ma10"] * 0.8))
        | ((df["rsi"] > 70) & (df["close"] > df["sma_20"]))
        | ((df["bb_width"] > 0.20) & (df["rsi"] > 55) & (df["ema_12"] < df["ema_26"]))
    ).fillna(False)

    with open(args.out_signals, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "side"])
        for _, row in df[entry].iterrows():
            w.writerow([row["t"].strftime("%Y-%m-%d %H:%M:%S"), "long"])
    n_entry = int(entry.sum())

    n_exit = 0
    if args.out_exit_signals:
        with open(args.out_exit_signals, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "reason"])
            for _, row in df[exit_sig].iterrows():
                w.writerow([row["t"].strftime("%Y-%m-%d %H:%M:%S"), "grid_exit"])
                n_exit += 1

    print(f"入场信号 {n_entry} 个 -> {args.out_signals}")
    if args.out_exit_signals:
        print(f"退出信号 {n_exit} 个 -> {args.out_exit_signals}")
    print("注意：本导出器只搬信号层；止损(-8%)/动态止损(-15%)/仓位管理属于策略执行层，"
          "请在 spec 的 exit 里声明（引擎按 spec 执行，不读 freqtrade 配置）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
