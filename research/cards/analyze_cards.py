"""结果分析：跨币交叉验证、多重检验校正、信号相关性。

**这个脚本的职责是给跑出来的数字降温，不是加温。** 三个必须做的动作：

1. **跨币一致性**。一个只在 BTC 上赚钱的策略，很可能是过拟合了 BTC 的
   某段行情。真正的优势应该在三个币上方向一致（哪怕幅度不同）。

2. **多重检验校正**。132 个配置里必然有几个看起来显著。Bonferroni 阈值
   是 0.05/132，用这个尺子量，绝大多数「好结果」都会掉出去。这不是
   保守，是算术。

3. **信号相关性**。S07 与 S29、S03 与 S04、S06 与 S04 的逻辑高度重合。
   如果它们的信号相关系数 > 0.8，就不能把收益相加——那是同一个优势
   被数了两次。

输出一份能直接贴进报告的汇总表，以及一份「值得继续研究的候选」清单。
候选的判据必须是**多币一致 + 成本后为正 + 有足够样本**，三条同时满足。
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "_out"


def load(tf: str) -> dict:
    p = OUT / f"cards_{tf}.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def key(r: dict) -> str:
    """同一张卡同一组参数，跨币归并用的键。"""
    params = ",".join(f"{k}={v}" for k, v in sorted(r.get("params", {}).items()))
    return f"{r['card']}|{params}"


def by_card_param(data: dict) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in data.get("results", []):
        if r.get("status") not in ("OK",):
            continue
        g[key(r)].append(r)
    return g


def summarize(data: dict) -> list[dict]:
    """每个 (卡, 参数) 组合的跨币汇总。"""
    out = []
    for k, rows in by_card_param(data).items():
        nets = [r["net_expectancy"] for r in rows]
        anns = [r["ann_return_pct"] for r in rows]
        sharps = [r["sharpe"] for r in rows]
        pfs = [r["profit_factor"] for r in rows if r["profit_factor"] != float("inf")]
        mdd = [r["max_dd_pct"] for r in rows]
        out.append({
            "key": k,
            "card": rows[0]["card"],
            "params": rows[0]["params"],
            "n_symbols": len(rows),
            "n_trades": sum(r["n_trades"] for r in rows),
            "median_net_exp": statistics.median(nets),
            "median_ann": statistics.median(anns),
            "median_sharpe": statistics.median(sharps),
            "median_pf": statistics.median(pfs) if pfs else 0.0,
            "median_mdd": statistics.median(mdd),
            "n_pos_net": sum(1 for x in nets if x > 0),
            "n_pos_sharpe": sum(1 for x in sharps if x > 0.3),
            "consistency": sum(1 for x in nets if x > 0) / len(nets),
            "symbols": {r["symbol"]: round(r["net_expectancy"], 3) for r in rows},
        })
    out.sort(key=lambda x: (-x["consistency"], -x["median_net_exp"]))
    return out


def main():
    for tf in ("4h", "1h", "15m"):
        data = load(tf)
        if not data:
            print(f"[{tf}] 无产物，跳过")
            continue
        n_cfg = data["n_configs"]
        rows = summarize(data)
        print("=" * 110)
        print(f"周期 {tf}：{n_cfg} 个配置，{len(rows)} 个有效 (卡,参数) 组合")
        print("=" * 110)

        ok = [r for r in rows if r["n_trades"] >= 30]
        print(f"\n样本 >= 30 笔的组合：{len(ok)} 个")
        print(f"{'card':<5}{'params':<26}{'n':>6}{'netExp':>8}{'ann%':>8}"
              f"{'shrp':>7}{'PF':>6}{'一致':>7}{'正净':>6}")
        print("-" * 90)
        for r in ok:
            ps = ",".join(f"{k}={v}" for k, v in r["params"].items())[:25]
            print(f"{r['card']:<5}{ps:<26}{r['n_trades']:>6}"
                  f"{r['median_net_exp']:>8.2f}{r['median_ann']:>8.1f}"
                  f"{r['median_sharpe']:>7.2f}{r['median_pf']:>6.2f}"
                  f"{r['consistency']*100:>6.0f}%"
                  f"{r['n_pos_net']}/{r['n_symbols']:>3}")

        # ---- 候选：三币全部正期望 + 样本足够 ----
        cands = [r for r in ok
                 if r["consistency"] == 1.0 and r["median_net_exp"] > 0]
        print(f"\n跨币一致为正的组合：{len(cands)} 个")
        if cands:
            print(f"{'card':<5}{'params':<26}{'netExp':>8}{'ann%':>8}{'shrp':>7}{'n':>7}")
            for r in cands[:20]:
                ps = ",".join(f"{k}={v}" for k, v in r["params"].items())[:25]
                print(f"{r['card']:<5}{ps:<26}{r['median_net_exp']:>8.2f}"
                      f"{r['median_ann']:>8.1f}{r['median_sharpe']:>7.2f}"
                      f"{r['n_trades']:>7}")

        # ---- 多重检验 ----
        print(f"\n多重检验：{n_cfg} 个配置。纯随机下约 {0.05*n_cfg:.1f} 个 "
              f"p<0.05。Bonferroni 阈值 0.05/{n_cfg} = {0.05/n_cfg:.2e}")
        print("      ——用这个尺子量，上面任何一条都不足以宣称「已验证」。")

        # ---- 卡级聚合（同一张卡所有参数合并看方向） ----
        card_agg: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            card_agg[r["card"]].append(r)
        print(f"\n{'card':<5}{'参数组':>7}{'中位netExp':>12}{'中位ann%':>11}"
              f"{'中位shrp':>10}{'最好一致':>10}")
        print("-" * 60)
        for card in sorted(card_agg):
            rs = card_agg[card]
            print(f"{card:<5}{len(rs):>7}{statistics.median([x['median_net_exp'] for x in rs]):>12.2f}"
                  f"{statistics.median([x['median_ann'] for x in rs]):>11.1f}"
                  f"{statistics.median([x['median_sharpe'] for x in rs]):>10.2f}"
                  f"{max(x['consistency'] for x in rs)*100:>9.0f}%")

    print("\n" + "=" * 110)
    print("注意：以上全部是**机械基线**，没有 LLM 参与。Phase 4 才检验")
    print("「强模型筛选形态能否在同样信号上创造增量」。基线不赚钱不等于")
    print("加 LLM 就能赚钱——必须先证明基线有微弱但真实的正向倾斜。")


if __name__ == "__main__":
    main()