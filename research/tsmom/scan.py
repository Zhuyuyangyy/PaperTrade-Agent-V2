"""三个基线 × 多回看期 × 多币种的完整扫描。

参数在跑之前一次性声明（LOOKBACKS 等常量），跑完不回头改。看完成绩再挑
回看期，就是在给自己造证据——这是本研究最想避免的事，因为它能把任何
策略「做」成正期望。

三个基线对应上一轮讨论里说的三件事：
  A 传统动量      positions_tsmom      —— 只看涨没涨
  B 风险调整动量  positions_riskadj    —— 看涨得够不够确定（t 值）
  C 动态动量      positions_regime     —— 动量给方向，长均线给许可

外加 D 纯多头 / E 纯空头两条腿，用来回答「多头的钱到底是 alpha 还是 beta」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import signals as sg
from run import build_spec, digest, run_engine, OUT

# ---- 事前声明，跑完不改 -------------------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
           "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT"]
LOOKBACKS = [10, 20, 30, 60, 90]
RISKADJ_THR = [1.0, 2.0]
REGIME_MA = [200]
FOLDS = 4

# 共同样本区间。EOSUSDT 止于 2025-05-26（早 18 个月），不纳入；
# 其余币起点不一，跨币比较必须截到共同窗口，否则「年化」不可比。
START = "2020-09-01"
END = "2026-11-26"


def main() -> None:
    rows: list[dict] = []
    OUT.mkdir(parents=True, exist_ok=True)

    for sym in SYMBOLS:
        candles_path = HERE / "data" / f"{sym}.csv"
        if not candles_path.is_file():
            print(f"[跳过] 无数据 {sym}")
            continue

        for lb in LOOKBACKS:
            for variant, extra in (
                ("tsmom", {}),
                ("tsmom_long", {}),
                ("tsmom_short", {}),
            ):
                meta = sg.build(sym, variant, lb, offset_start=START, offset_end=END)
                rows.append(_one(sym, variant, lb, meta, candles_path, extra))

            for thr in RISKADJ_THR:
                meta = sg.build(sym, "riskadj", lb, thr=thr, offset_start=START, offset_end=END)
                rows.append(_one(sym, "riskadj", lb, meta, candles_path, {"thr": thr}))

        for ma in REGIME_MA:
            for lb in LOOKBACKS:
                meta = sg.build(sym, "regime", lb, ma=ma, offset_start=START, offset_end=END)
                rows.append(_one(sym, "regime", lb, meta, candles_path, {"ma": ma}))

        print(f"[完成] {sym}  累计 {len(rows)} 条", flush=True)

    # 行尾固定 LF、字段顺序固定：这份文件会被 sha256 记账
    out = HERE / "_out" / "scan_results.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"\n写出 {out}  ({len(rows)} 条)")


def _one(sym: str, variant: str, lb: int, meta: dict, candles_path: Path, extra: dict) -> dict:
    name = f"{sym}__{variant}__{lb}" + "".join(f"__{k}{v}" for k, v in extra.items())
    build_spec(
        name, sym,
        "../" + meta["entries_path"].replace("\\", "/"),
        "../" + meta["exits_path"].replace("\\", "/"),
        hypothesis=f"{sym} 日线 {variant} 回看{lb}", folds=FOLDS, grid=False,
    )
    try:
        d = run_engine(f"{name}.json", candles_path)
    except Exception as exc:                                  # noqa: BLE001
        return {"name": name, "symbol": sym, "variant": variant, "lookback": lb,
                "error": str(exc)[:400], **extra}
    g = digest(d, variant)
    g.update(extra)
    g["variant"] = variant
    g["lookback"] = lb
    g["signal_meta"] = {k: meta[k] for k in
                        ("n_bars", "n_entries", "long_entries", "short_entries", "in_market_pct")}
    return g


if __name__ == "__main__":
    main()