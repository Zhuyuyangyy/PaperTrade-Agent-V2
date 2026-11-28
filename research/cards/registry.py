"""30 张策略卡的注册表。

设计原则：每张卡是一个**可执行的研究假设**，不是一段自然语言提示词。

编号按家族分组，与用户给的清单一一对应：
  S01–S09  SMC/ICT (9)
  S10–S13  Wyckoff (4)
  S14–S19  PriceAction (6)
  S20–S23  Trend (4)
  S24–S27  OrderFlow (4)
  S28–S30  Other (3)
  合计 30

字段分三层：
  · 元信息：id / 家族 / 来源 / 版本 / 数据依赖 / 适用市场状态
  · 可计算规则：形态 → 候选区 → 确认 → 失效 → 目标 → 有效期
  · 状态机：SCANNING → ARMED → WAITING_RETEST → WAITING_CONFIRMATION
            → TRIGGERED / INVALIDATED

**数据依赖是硬约束。** 缺 OI 就标 DATA_BLOCKED，不许用价格序列冒充。
宁可少测一张，也不能编一条。

参数空间全部显式列出且**先于回测固定**。禁止跑完看结果再扩参数——
那是给多重检验刷显著性。
"""

from __future__ import annotations

from dataclasses import dataclass, field


D_OHLCV = "ohlcv"
D_TAKER = "taker_buy_volume"
D_OI = "open_interest"
D_LIQ = "liquidations"
D_NEWS = "news_timestamp"
D_BOOK = "orderbook"

STATES = ("SCANNING", "ARMED", "WAITING_RETEST", "WAITING_CONFIRMATION",
          "TRIGGERED", "INVALIDATED")

LIFECYCLE = ("DRAFT", "IMPLEMENTED", "BACKTESTED", "VALIDATED",
             "PAPER_TRADING", "REJECTED", "DATA_BLOCKED", "NEEDS_RESEARCH")


@dataclass
class Card:
    id: str
    family: str
    name: str
    version: str
    source: str
    thesis: str
    regime_fit: list[str]
    needs: list[str]
    timeframes: dict
    params: dict
    invalidates_if: list[str]
    known_limits: list[str] = field(default_factory=list)
    overlaps_with: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "family": self.family, "name": self.name,
            "version": self.version, "source": self.source,
            "thesis": self.thesis, "regime_fit": self.regime_fit,
            "needs": self.needs, "timeframes": self.timeframes,
            "params": self.params, "invalidates_if": self.invalidates_if,
            "known_limits": self.known_limits,
            "overlaps_with": self.overlaps_with,
            "states": list(STATES),
            "lifecycle_default": "DRAFT",
        }


TF_STD = {"context": "4h", "structure": "15m", "trigger": "5m"}
TF_FAST = {"context": "1h", "structure": "15m", "trigger": "5m"}

CARDS: list[Card] = [
    # ================= SMC / ICT (9) =================
    Card("S01", "SMC/ICT", "BOS Continuation", "0.1.0",
         "ICT / 结构交易通行做法",
         "已确认的 BOS 之后，回抽至结构区并再次同向确认，净期望高于直接追突破。",
         ["trend"], [D_OHLCV], TF_STD,
         {"pivot_left": [3, 5], "pivot_right": [3, 5], "confirm_bars": [2, 3],
          "retest_max_bars": [10, 20]},
         ["收盘重新跌回 BOS 前结构内", "retest 超过 max_bars 未触发"],
         ["pivot 确认延迟 right 根，会吃掉一部分行情", "BOS 与 CHoCH 在小周期互相覆盖"]),

    Card("S02", "SMC/ICT", "CHoCH Reversal", "0.1.0",
         "ICT 结构性质改变",
         "下降结构中出现向上 CHoCH（或反之）后，趋势反转概率显著高于随机。",
         ["reversal"], [D_OHLCV], TF_STD,
         {"pivot_left": [3, 5], "pivot_right": [3, 5], "retest_max_bars": [10, 20]},
         ["反转后重新创出原趋势新极值", "CHoCH 被后续 BOS 否定"],
         ["CHoCH 与 BOS 的判定只差『之前的结构方向』，边界样本易误判"]),

    Card("S03", "SMC/ICT", "FVG Retest", "0.1.0",
         "ICT 2022 / SMC",
         "未回补的 FVG 提供非随机支撑/阻力；回抽至缺口边缘并按方向确认，存在正期望。",
         ["trend", "range"], [D_OHLCV], TF_STD,
         {"min_gap_atr": [0.3, 0.6, 1.0], "entry_at": ["edge", "mid"],
          "retest_max_bars": [20, 40]},
         ["价格收盘穿越缺口远端", "缺口已完全回补"],
         ["缺口定义对 pivot 尺度敏感", "『unmitigated』需额外状态跟踪"]),

    Card("S04", "SMC/ICT", "Inversion FVG", "0.1.0",
         "ICT IFVG",
         "原方向 FVG 被反向有效穿越后，该区间转为反向参考，回测时提供反向入场。",
         ["reversal"], [D_OHLCV], TF_STD,
         {"min_gap_atr": [0.3, 0.6], "entry_at": ["edge", "mid"]},
         ["价格重新回到原缺口方向并站稳"],
         ["IFVG 与普通突破回踩的区别需消融验证"]),

    Card("S05", "SMC/ICT", "Order Block Retest", "0.1.0",
         "SMC / 机构订单块",
         "位移行情之前的最后一根反向 K 线区域，回测时提供支撑/阻力。",
         ["trend"], [D_OHLCV], TF_STD,
         {"impulse_atr_min": [1.0, 1.2, 1.5], "retest_max_bars": [20, 40]},
         ["价格收盘穿过订单块整个区域"],
         ["OB 定义流派差异大", "『未缓解』状态未实现"]),

    Card("S06", "SMC/ICT", "Breaker Block", "0.1.0",
         "ICT Breaker",
         "被反向突破的订单块翻转为 opposite 方向的入场区，比普通 OB 更可靠。",
         ["reversal", "trend"], [D_OHLCV], TF_STD,
         {"impulse_atr_min": [1.0, 1.5], "retest_max_bars": [20, 40]},
         ["价格再次穿越该块并站稳"],
         ["Breaker 与 IFVG 高度重合，需相关性检查"]),

    Card("S07", "SMC/ICT", "Liquidity Sweep + MSS", "0.1.0",
         "ICT / Turtle Soup 同源",
         "扫掉前高/前低后收回，再出现反向 MSS，属于流动性诱捕后的结构反转。",
         ["range", "reversal"], [D_OHLCV], TF_STD,
         {"sweep_within": [3, 5], "min_wick_atr": [0.1, 0.25],
          "require_mss": [True, False]},
         ["再次突破清扫极值", "MSS 之后未出现确认"],
         ["与 S29 Turtle Soup 逻辑重合，必须做相关性检查"]),

    Card("S08", "SMC/ICT", "Optimal Trade Entry (OTE)", "0.1.0",
         "ICT OTE（0.62–0.79 回撤）",
         "在 BOS 之后的 0.62–0.79 回撤区入场，风险回报优于浅回撤入场。",
         ["trend"], [D_OHLCV], TF_STD,
         {"fib_lo": [0.62, 0.705], "fib_hi": [0.79], "retest_max_bars": [20, 40]},
         ["回撤超过 1.0（即完全吞回位移）"],
         ["Fibonacci 比例本身无理论依据，属经验参数"]),

    Card("S09", "SMC/ICT", "Silver Bullet", "0.1.0",
         "ICT Silver Bullet（特定时段窗口）",
         "特定小时窗口内出现的 FVG 回测，命中率高于全天平均。",
         ["trend"], [D_OHLCV], TF_FAST,
         {"hours_utc": [[10, 11], [14, 15], [18, 19]], "min_gap_atr": [0.3]},
         ["窗口内未出现有效 FVG", "价格穿越缺口远端"],
         ["样本高度依赖时段切分，易过拟合", "需与其他 FVG 卡做消融"]),

    # ================= Wyckoff (4) =================
    Card("S10", "Wyckoff", "Spring + Test", "0.1.0",
         "Wyckoff 吸筹",
         "区间下沿假跌破后收回，二次测试不破前低，则吸筹完成、上行概率提升。",
         ["range"], [D_OHLCV], TF_STD,
         {"range_window": [30, 40, 60], "max_width_atr": [3.0, 4.0],
          "require_second_test": [True, False]},
         ["二次测试跌破 Spring 低点", "区间被向下有效突破"],
         ["区间识别对窗口长度敏感", "与 S11/S29 逻辑重合"]),

    Card("S11", "Wyckoff", "UTAD + Test", "0.1.0",
         "Wyckoff 派发",
         "区间上沿假突破后回落，反抽受阻，则派发完成、下行概率提升。",
         ["range"], [D_OHLCV], TF_STD,
         {"range_window": [30, 40, 60], "max_width_atr": [3.0, 4.0],
          "require_second_test": [True, False]},
         ["反抽站稳区间上沿", "区间被向上有效突破"],
         ["与 S10 镜像，不能当两个独立收益来源"]),

    Card("S12", "Wyckoff", "SOS + LPS", "0.1.0",
         "Wyckoff 吸筹第二阶段",
         "Sign of Strength 之后的 Last Point of Support 是低风险多头入场点。",
         ["range", "trend"], [D_OHLCV], TF_STD,
         {"range_window": [30, 40, 60], "sos_vol_z": [1.0, 1.5]},
         ["LPS 被跌破", "回到区间中部以下"],
         ["SOS 需要成交量确认，量能口径敏感"]),

    Card("S13", "Wyckoff", "SOW + LPSY", "0.1.0",
         "Wyckoff 派发第二阶段",
         "Sign of Weakness 之后的 Last Point of Supply 是低风险空头入场点。",
         ["range", "trend"], [D_OHLCV], TF_STD,
         {"range_window": [30, 40, 60], "sow_vol_z": [1.0, 1.5]},
         ["LPSY 被突破", "回到区间中部以上"],
         ["与 S12 镜像"]),

    # ================= PriceAction (6) =================
    Card("S14", "PriceAction", "Breakout Retest", "0.1.0",
         "突破回踩",
         "突破后独立回踩并收盘确认，优于直接追突破。",
         ["trend"], [D_OHLCV], TF_STD,
         {"breakout_atr": [0.3, 0.5], "retest_bars": [3, 10],
          "require_closed_confirm": [True]},
         ["回踩重新跌回原区间"],
         ["『独立』的判定需要最小间隔"]),

    Card("S15", "PriceAction", "False Breakout Reversal", "0.1.0",
         "假突破反转",
         "突破后快速收回，反向动能强于普通反转。",
         ["reversal"], [D_OHLCV], TF_STD,
         {"reclaim_bars": [1, 2, 3], "min_wick_atr": [0.2]},
         ["再次突破并站稳"],
         ["与 S29 重合"]),

    Card("S16", "PriceAction", "Support Resistance Rejection", "0.1.0",
         "裸K",
         "关键水平位出现拒绝形态后，短期反向移动概率高于随机。",
         ["range"], [D_OHLCV], TF_STD,
         {"touch_tolerance_atr": [0.1, 0.2], "min_wick_atr": [0.3, 0.5]},
         ["收盘有效穿越该水平"],
         ["水平位本身是事后挑选的高风险点"]),

    Card("S17", "PriceAction", "Pin Bar Rejection", "0.1.0",
         "裸K / Pin Bar",
         "长影线拒绝关键价位后，反转概率高于随机。",
         ["range"], [D_OHLCV], TF_STD,
         {"wick_body_ratio": [2.0, 3.0], "min_wick_atr": [0.5]},
         ["下一根吞没影线"],
         []),

    Card("S18", "PriceAction", "Engulfing", "0.1.0",
         "裸K / 吞没",
         "吞没形态在关键位出现时，短期反转概率提升。",
         ["reversal"], [D_OHLCV], TF_STD,
         {"min_body_ratio": [1.0, 1.5]},
         ["下一根反向吞没"],
         []),

    Card("S19", "PriceAction", "Inside Bar Breakout", "0.1.0",
         "Inside Bar",
         "母子线收敛后突破母体区间，方向延续概率高于随机。",
         ["trend"], [D_OHLCV], TF_STD,
         {"max_wait": [5, 10]},
         ["突破后收回母体"],
         []),

    # ================= Trend (4) =================
    Card("S20", "Trend", "Donchian Breakout", "0.1.0",
         "Turtle Traders",
         "N 周期新高/新低突破后趋势延续，扣除成本后仍有正期望。",
         ["trend"], [D_OHLCV],
         {"context": "1d", "structure": "4h", "trigger": "1h"},
         {"channel": [20, 55], "atr_stop": [2.0, 3.0]},
         ["跌破反向通道"],
         ["日线级已由 TSMOM 研究间接覆盖"]),

    Card("S21", "Trend", "Volatility Compression Breakout", "0.1.0",
         "Squeeze / NR7",
         "波动率压缩后往往伴随扩张；压缩末端的突破比普通突破更有持续性。",
         ["trend"], [D_OHLCV], TF_STD,
         {"squeeze_pct": [0.2, 0.3], "atr_n": [14, 20]},
         ["突破后立刻收回压缩区间"],
         ["『压缩』定义多样"]),

    Card("S22", "Trend", "Trend Pullback", "0.1.0",
         "趋势跟随",
         "趋势中等回抽至均线/结构位后继续，比追高有更好的风险回报。",
         ["trend"], [D_OHLCV], TF_STD,
         {"ma": [20, 50], "pullback_atr": [0.5, 1.0]},
         ["收盘跌破均线且结构破坏"],
         []),

    Card("S23", "Trend", "Momentum Continuation", "0.1.0",
         "动量",
         "强动量 K 线之后短期延续概率高于随机。",
         ["trend"], [D_OHLCV], TF_STD,
         {"body_atr": [1.0, 1.5], "max_bars": [3, 5]},
         ["下一根反向吞没"],
         ["与 TSMOM 结论可能有冲突，需对照"]),

    # ================= OrderFlow (4) =================
    Card("S24", "OrderFlow", "CVD Divergence", "0.1.0",
         "订单流",
         "主动成交方向与价格背离时，价格倾向于修正。",
         ["reversal"], [D_OHLCV, D_TAKER], TF_STD,
         {"div_window": [20, 40], "min_div_atr": [0.5]},
         ["背离消失，价格与 CVD 同向"],
         ["需要 taker buy volume；本仓库已采集"]),

    Card("S25", "OrderFlow", "Open Interest + Structure", "0.1.0",
         "持仓量分析",
         "OI 与价格同向增长时趋势更可信；OI 下降伴随突破多为假突破。",
         ["trend"], [D_OHLCV, D_OI], TF_STD,
         {"oi_change_pct": [0.05, 0.1]},
         ["OI 与价格背离"],
         ["需要历史 OI，币安 REST 只提供近期"]),

    Card("S26", "OrderFlow", "Liquidation Reversal", "0.1.0",
         "清算数据",
         "异常清算后价格被吸收，短期出现均值回归。",
         ["reversal"], [D_OHLCV, D_LIQ], TF_STD,
         {"liq_zscore": [2.0, 3.0]},
         ["清算继续放大且价格不收回"],
         ["历史清算数据需第三方归档"]),

    Card("S27", "OrderFlow", "Volume Spread Analysis", "0.1.0",
         "VSA / Wyckoff",
         "成交量与推进幅度背离（放量滞涨/缩量下跌）预示趋势衰竭。",
         ["range", "reversal"], [D_OHLCV], TF_STD,
         {"vol_zscore": [1.5, 2.0], "spread_atr": [0.5, 1.0]},
         ["放量同向推进"],
         []),

    # ================= Other (3) =================
    Card("S28", "Other", "VWAP Mean Reversion", "0.1.0",
         "VWAP",
         "价格过度偏离日内 VWAP 后有回归倾向。",
         ["range"], [D_OHLCV], TF_FAST,
         {"dev_atr": [1.5, 2.5], "vwap_window": ["session", 96]},
         ["趋势日单边远离 VWAP"],
         ["需要明确 VWAP 锚点；日内与滚动口径不同"]),

    Card("S29", "Other", "Turtle Soup", "0.1.0",
         "Turtle Soup (Connors/Crabel)",
         "前高/前低被刺穿但收盘收回，属于失败突破，反向交易有正期望。",
         ["range", "reversal"], [D_OHLCV], TF_STD,
         {"lookback_pivot": [10, 20, 55], "min_wick_atr": [0.1, 0.25]},
         ["收盘站稳突破方向", "连续两次清扫同向"],
         ["与 S07/S10 高度重合"]),

    Card("S30", "Other", "News Event Reaction", "0.1.0",
         "事件驱动",
         "事件冲击后波动扩张，随后按事件方向延续的概率高于随机。",
         ["event"], [D_OHLCV, D_NEWS], TF_STD,
         {"window_min": [15, 60]},
         ["事件后波动迅速收敛且不延续"],
         ["需要带可靠发布时间的新闻历史；否则 DATA_BLOCKED"]),
]


BY_ID = {c.id: c for c in CARDS}


def data_blocked(available: set[str]) -> list[Card]:
    return [c for c in CARDS if not set(c.needs).issubset(available)]


def registry() -> dict:
    fams: dict[str, int] = {}
    for c in CARDS:
        fams[c.family] = fams.get(c.family, 0) + 1
    return {"n_cards": len(CARDS), "families": fams,
            "cards": [c.to_dict() for c in CARDS]}


if __name__ == "__main__":
    r = registry()
    print(f"共 {r['n_cards']} 张卡")
    for k, v in r["families"].items():
        print(f"  {k:<14}{v}")
    assert r["n_cards"] == 30, "必须正好 30 张"
    assert len({c.id for c in CARDS}) == 30, "ID 必须唯一"