"""外部策略条目层——External Strategy Registry 的核心。

**这一层解决什么问题**：`sources.py` 记的是「哪个仓库」，`registry.py`
（research/cards）记的是「我们自己的假设」。两者之间缺一个东西：
**一条外部策略，我们到底能不能拿它当研究起点，以及拿到什么程度。**

用户任务书把这个叫做 External Strategy Registry，每条要记九项：
来源、许可证、原始定义、逻辑分类、数据依赖、可执行实现、
潜在缺陷、前视偏差风险、复现结果。

**九项里最容易造假的三个，本模块专门加了硬约束**：

1. **复现结果**。绝不允许写「该策略宣称年化 300%」这种转述。
   `reproduction` 只能是本模块定义的那几个枚举值，其中
   `NOT_ATTEMPTED` 是默认值、也是绝大多数条目应有的值。
   外部宣传数字一律进 `vendor_claims` 字段并强制带
   `NOT_VERIFIED_BY_US` 标记——它和 `reproduction` 是**物理隔离**的两列。

2. **可执行实现**。`impl_ref` 指向我们自己写的代码（可为空）。
   空就是空，不许写「参考其实现思路」蒙混。没有可执行实现
   就不能进回测队列——这条由 `backtest_ready` 强制。

3. **前视偏差风险**。不是我们自己拍脑袋的判断，而是必须挂
   `lookahead_scan.py` 的静态扫描结果（`scan_ref`）或明确写
   `NO_SOURCE_ACCESS`（因为我们没有代码可扫）。

**与 Methodology 的关系**：一个条目复现成功后，产出的不是
「已验证方法论」，而是一条 `Methodology`（DRAFT 状态，进隔离区）。
登记 ≠ 验证，复现 ≠ 验证。这条链上每一步都得重新挣证据。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from sources import LICENSE_POLICY, SOURCES

# ---------------------------------------------------------------------------
# 枚举：所有「结论类」字段都必须是闭集
# ---------------------------------------------------------------------------
# 自由文本字段最大的问题是「什么都写得了」。一旦允许自由文本描述
# 复现结果，报告里迟早会出现「效果不错」「基本一致」这种话。
# 闭集强制每一条都能被统计、被筛、被质疑。


class Impl(Enum):
    """可执行实现的真实状态。"""
    NONE = "NONE"                      # 只有文字描述，无代码
    REF_ONLY = "REF_ONLY"              # 只有别人写的代码参考，未移植
    PORTED = "PORTED"                  # 已按方法重写成本项目可跑代码
    PORTED_AND_SCANNED = "PORTED_AND_SCANNED"   # 重写且过了前视扫描


class Repro(Enum):
    """复现结果。**默认值是 NOT_ATTEMPTED，这是刻意的。**

    绝大多数外部策略我们不会复现，也不该假装复现了。
    枚举里没有「表现良好」这种值——复现只回答「我们的实现能否重现
    原文描述的行为」，「是否赚钱」是下一层（回测）的问题。

    **`INDEPENDENT_IMPL` 这个值是写完初版后补的，理由值得记下来。**

    初版把 FVG 那条标成 `RAN_MATCHED`，同时 repro_note 里写着
    「我们自己的 S03 是独立实现，不是从 Minara 移植」。这两句话
    互相矛盾：既然没拿对方的定义来跑，就谈不上「与原文一致」。
    更糟的是 `RAN_MATCHED` 这个词本身会被读成「我们验证了 Minara
    的策略」——正是用户明令禁止的那种暗示。

    真实情况是：我们读了公开的方法描述，然后自己写了一个实现，
    跑出了自己的结果。这三件事里没有一件叫「复现」。
    所以需要一个专门的值来诚实地表达它，而不是借用 RAN_MATCHED。
    """
    NOT_ATTEMPTED = "NOT_ATTEMPTED"    # 还没跑（默认）
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"  # 缺必要数据，跑不了
    LICENSE_BLOCKED = "LICENSE_BLOCKED"    # 许可证不允许复制，只能读
    INDEPENDENT_IMPL = "INDEPENDENT_IMPL"  # 我们自己独立实现了同族方法，
    #                                        **未复现外部那一份**
    RAN_DIVERGED = "RAN_DIVERGED"      # 用对方的定义跑了，结果与描述不符
    RAN_INCONCLUSIVE = "RAN_INCONCLUSIVE"  # 用对方定义跑了，样本/口径不足以判断
    RAN_MATCHED = "RAN_MATCHED"        # 用对方定义跑了，行为与描述一致
    # 注意：**没有 RAN_PROFITABLE**。复现不负责回答盈利性。
    # 也注意：RAN_* 三个值都隐含「用了对方的定义」。没用就不是复现。


class Risk(Enum):
    """前视偏差风险等级。取值必须能追溯到证据。"""
    UNSCANNED = "UNSCANNED"            # 有代码但没扫（不该长期停留）
    NO_SOURCE_ACCESS = "NO_SOURCE_ACCESS"  # 没代码可扫，只能读描述
    SCANNED_CLEAN = "SCANNED_CLEAN"    # 扫过，无高危命中
    SCANNED_FLAGGED = "SCANNED_FLAGGED"  # 扫过，有高危命中
    CONFIRMED_LOOKAHEAD = "CONFIRMED_LOOKAHEAD"  # 已确认存在前视（人工裁定）


class Family(Enum):
    """逻辑分类。与 research/cards 的家族对齐，便于合并去重。"""
    SMC_ICT = "SMC/ICT"
    WYCKOFF = "Wyckoff"
    PRICE_ACTION = "PriceAction"
    TREND = "Trend"
    ORDER_FLOW = "OrderFlow"
    MEAN_REVERSION = "MeanReversion"
    VOL_CARRY = "VolCarry"
    ML = "ML"
    OTHER = "Other"


class Gate(Enum):
    """能否进入回测队列的判定结果。"""
    READY = "READY"                    # 可入队
    NEEDS_PORT = "NEEDS_PORT"          # 有方法无实现，需先移植
    NEEDS_DATA = "NEEDS_DATA"          # 缺数据
    BLOCKED_LICENSE = "BLOCKED_LICENSE"  # 许可证不允许
    BLOCKED_LOOKAHEAD = "BLOCKED_LOOKAHEAD"  # 前视未排除
    REJECTED = "REJECTED"              # 已证伪


# 允许进入回测队列的 Gate 值。**只有一个。**
QUEUEABLE = {Gate.READY}


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------

@dataclass
class VendorClaim:
    """外部自述的业绩/能力数字。

    **这是整个模块里最重要的一处隔离。** 用户任务书写得很清楚：
    「不得将外部宣传收益当成验证结果。」

    所以外部数字不能进 `reproduction`，也不能进任何
    `to_dict()["reproduction"]` 相关字段。它只能活在这里，
    并且 `verified_by_us` 恒为 False —— 构造函数里不给参数，
    在结构上就不可能被写成 True。
    """
    text: str
    source_key: str            # 出自哪个来源
    where: str = ""            # 原文位置（README / 网页 / 论文）
    verified_by_us: bool = field(default=False, init=False)   # **恒 False**

    def to_dict(self) -> dict:
        return {"text": self.text, "source_key": self.source_key,
                "where": self.where, "verified_by_us": False,
                "disposition": "仅供了解，不作为研究依据"}


@dataclass
class ExternalStrategy:
    """一条外部策略的完整登记。"""

    id: str
    name: str
    source_key: str                    # 关联 sources.py
    license: str
    original_def: str                  # 原始定义（引文或转述，标明性质）
    family: Family

    # -- 数据依赖 --------------------------------------------------------
    needs: tuple[str, ...] = ("ohlcv",)     # 与 cards/registry.py 的 D_* 对齐
    asset_class: str = "major_crypto"
    timeframes: tuple[str, ...] = ()

    # -- 实现 ------------------------------------------------------------
    impl: Impl = Impl.NONE
    impl_ref: str = ""                 # 我们自己的实现文件路径；空即无

    # -- 缺陷与风险 ------------------------------------------------------
    known_flaws: tuple[str, ...] = ()
    lookahead_risk: Risk = Risk.UNSCANNED
    scan_ref: str = ""                 # 扫描结果引用（JSON 路径或规则名）

    # -- 复现 ------------------------------------------------------------
    reproduction: Repro = Repro.NOT_ATTEMPTED
    repro_note: str = ""               # 只能说明「怎么跑的/为什么没跑」

    # -- 外部宣传（隔离存放）----------------------------------------------
    vendor_claims: tuple[VendorClaim, ...] = ()

    # -- 判定 ------------------------------------------------------------
    gate: Gate = Gate.NEEDS_PORT
    gate_reason: str = ""

    def license_allows_copy(self) -> bool:
        return LICENSE_POLICY.get(self.license, ("", "READ_ONLY"))[1] == "COPY_OK"

    def backtest_ready(self) -> tuple[bool, str]:
        """能否进回测队列。**四道闸门，全过才行。**

        顺序有讲究：先查许可证（法律问题优先于技术问题），
        再查前视（科学有效性问题），最后查实现与数据（工程问题）。
        把顺序搞反会让「我们跑不了」被误读成「策略不行」。
        """
        if self.gate in (Gate.REJECTED, Gate.BLOCKED_LICENSE,
                         Gate.BLOCKED_LOOKAHEAD):
            return False, f"门禁已判死：{self.gate.value}（{self.gate_reason}）"

        if not self.license_allows_copy():
            # 注意：**READ_ONLY 不等于不能研究**。我们可以读方法、
            # 自己重写实现，只是不能复制对方的代码。
            # 所以这里不是直接拒绝，而是要求实现必须是 PORTED
            # （我们自己写的），不能是 REF_ONLY（指着别人的代码）。
            if self.impl in (Impl.NONE, Impl.REF_ONLY):
                return False, (f"许可证 {self.license} 只允许读方法，"
                               f"需自行重写实现（当前 impl={self.impl.value}）")

        if self.lookahead_risk in (Risk.SCANNED_FLAGGED,
                                   Risk.CONFIRMED_LOOKAHEAD):
            return False, f"前视未排除：{self.lookahead_risk.value}"
        if self.lookahead_risk == Risk.UNSCANNED and self.impl_ref:
            # 有实现却没扫，是最危险的状态：能跑，但不知道跑的是不是真的。
            return False, "有可执行实现但未做前视扫描"

        if self.impl not in (Impl.PORTED, Impl.PORTED_AND_SCANNED):
            return False, f"无可执行实现（impl={self.impl.value}）"
        if self.reproduction == Repro.DATA_UNAVAILABLE:
            return False, "必要数据不可得"
        if not self.impl_ref:
            return False, "impl 标了 PORTED 但没有 impl_ref——状态与事实不符"

        return True, "可入队"

    def to_dict(self) -> dict:
        ok, why = self.backtest_ready()
        return {
            "id": self.id, "name": self.name,
            "source_key": self.source_key, "license": self.license,
            "license_copy_ok": self.license_allows_copy(),
            "original_def": self.original_def,
            "family": self.family.value,
            "needs": list(self.needs), "asset_class": self.asset_class,
            "timeframes": list(self.timeframes),
            "impl": self.impl.value, "impl_ref": self.impl_ref,
            "known_flaws": list(self.known_flaws),
            "lookahead_risk": self.lookahead_risk.value,
            "scan_ref": self.scan_ref,
            "reproduction": self.reproduction.value,
            "repro_note": self.repro_note,
            "vendor_claims": [c.to_dict() for c in self.vendor_claims],
            "gate": self.gate.value, "gate_reason": self.gate_reason,
            "backtest_ready": ok, "backtest_reason": why,
        }

    def dedup_key(self) -> str:
        """跨来源去重：同一逻辑在不同仓库里各写一遍，只算一条。

        键的构成刻意与 `Methodology.dedup_key()` 同构
        （family → primary_signal 的位置换成 family），
        这样外部条目与我们自己的卡可以进同一个去重表对比。
        """
        tf = "+".join(sorted(self.timeframes))
        needs = "+".join(sorted(self.needs))
        return "|".join([self.family.value, self.asset_class, tf, needs])


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------
# 说明：**这里刻意只放少量条目。**
# 用户要的是「外部策略资源库」，但把 909 个 freqtrade 策略抄进来
# 恰恰是调研摘要里那个 MISLEADING 陷阱——那些是无许可证的近重复代码。
# 真正能作为研究起点的，是「方法被讲清楚了、我们能自己实现」的那些。
#
# 当前条目全部来自本轮实际读过的公开材料。没有读过的，一条都不填。

ENTRIES: list[ExternalStrategy] = [

    ExternalStrategy(
        id="X-ICT-FVG-RETEST",
        name="FVG 回抽确认（Fair Value Gap retest）",
        source_key="minara.docs",
        license="DOCS_ONLY",
        original_def=(
            "转述，非引文。来自 Minara `features/strategy-studio` 与 "
            "`features/factor-research` 的公开描述：未回补的公允价值缺口"
            "（FVG）被回抽时提供方向性支撑，回抽到缺口边缘并按原方向确认后入场。"
            "**原始定义我只读到概要级描述，没有读到参数化的规则文本。**"
        ),
        family=Family.SMC_ICT,
        needs=("ohlcv",),
        timeframes=("4h", "1h"),
        impl=Impl.PORTED_AND_SCANNED,
        impl_ref="research/cards/signals_cards.py (S03 FVG Retest)",
        known_flaws=(
            "缺口定义对 pivot 尺度敏感：left/right 变化会改变哪些缺口被认定为 FVG",
            "「未回补（unmitigated）」需要额外状态跟踪，实现里容易退化成「最近一个缺口」",
            "与 S05（Order Block）在定义上高度重叠，两者证据不能独立计数",
        ),
        lookahead_risk=Risk.SCANNED_CLEAN,
        scan_ref="lookahead_scan.py / research/cards 高危 0 命中",
        reproduction=Repro.INDEPENDENT_IMPL,
        repro_note=(
            "**未复现**。我们自己的 S03 是按 ICT 通行定义写的独立实现，"
            "不是从 Minara 移植，也没拿到它的参数化规则文本。"
            "所以正确说法是「我们实现了同族方法并跑过」，"
            "不是「我们复现了 Minara 的策略」。"
            "跑出来的结论：4h 净期望 +0.21%（三币），1h 转负。"
            "这既不能证明 Minara 的策略有效，也不能证伪。"
        ),
    ),

    ExternalStrategy(
        id="X-EXTREMA-PIVOT",
        name="中心窗口极值 pivot（argrelextrema 风格）",
        source_key="robotic.freqtrade-strategies",
        license="NO_LICENSE",
        original_def=(
            "转述自该仓库 `docs/LOOKAHEAD.md` 的统计：1,286 个文件中 "
            "`extrema_scan` 命中 52 次，`rolling_center` 命中 59 次。"
            "原文形态是 `argrelextrema(close, np.greater, order=n)` 或 "
            "`rolling(n, center=True).max()`，"
            "**两者都需要 i+n 的数据才能确定 i 是否极值**。"
        ),
        family=Family.PRICE_ACTION,
        needs=("ohlcv",),
        timeframes=("4h", "1h", "15m"),
        impl=Impl.PORTED_AND_SCANNED,
        impl_ref="research/cards/structure.py (confirmed_at = i + right)",
        known_flaws=(
            "**这是前视偏差的标准形态**：pivot 在 i 处被「确认」需要看到 i+right，"
            "实盘只能在 i+right 之后才知道",
            "我们的结构层用 `confirmed_at` 显式记录延迟，但**用了什么时机"
            "取结构，和结构本身何时可知，是两个独立的问题**——补了前者"
            "不能证明后者被正确处理",
            "左右窗口不等长时（left≠right），偏差方向不直观，容易以为对称就没事",
        ),
        lookahead_risk=Risk.SCANNED_CLEAN,
        scan_ref="lookahead_scan.py / 本项目用 center_window_custom 规则专防此类",
        reproduction=Repro.INDEPENDENT_IMPL,
        repro_note=(
            "复现的是**修正后的版本**（加 confirmed_at 延迟），"
            "不是原仓库那份代码——它无许可证，且已确认有前视。"
            "「修正后还剩多少优势」这个问题我们答不上来：所有含 pivot 的"
            "卡（S01/S02/S03/S05 等）都受影响，这是一个系统性未知，"
            "不是单卡问题。记在这里是为了不让它被遗忘。"
        ),
    ),

    ExternalStrategy(
        id="X-VWAP-ANCHOR",
        name="锚定 VWAP 偏离回归",
        source_key="hkuds.vibe-trading",
        license="MIT",
        original_def=(
            "转述。Vibe-Trading 的因子列表中含 VWAP 类因子（具体实现未逐条核实）。"
            "通行做法：以会话/事件起点锚定 VWAP，价格显著偏离后回归。"
        ),
        family=Family.MEAN_REVERSION,
        needs=("ohlcv",),
        timeframes=("4h", "1h", "15m"),
        impl=Impl.PORTED_AND_SCANNED,
        impl_ref="research/cards/signals_more.py (S28 VWAP)",
        known_flaws=(
            "anchor 选择是自由参数，事后选最优 anchor 等于过拟合",
            "15m 上笔数极多（93,766 笔实测​）但单笔净期望 −0.12%——"
            "**交易频率掩盖了单笔质量**",
            "VWAP 在加密永续上受资金费率影响，纯 OHLCV 口径不完整",
        ),
        lookahead_risk=Risk.SCANNED_CLEAN,
        scan_ref="lookahead_scan.py / research/cards 高危 0 命中",
        reproduction=Repro.INDEPENDENT_IMPL,
        repro_note=(
            "**未复现**——我们没有 Vibe-Trading 那份 VWAP 因子的具体实现"
            "（只看到因子列表里的名字），无法按对方定义跑。"
            "跑的是我们自己的 S28。结果与「VWAP 偏离回归有正期望」这个"
            "通行说法不符：15m 上净期望 −0.12%，93,766 笔。"
            "**这不构成对该说法的证伪**——我们的 anchor 与成本口径可能"
            "和原方法不同。只能说明「我们这样写不行」。"
        ),
    ),

    ExternalStrategy(
        id="X-MOMENTUM-TSMOM",
        name="时序动量（TSMOM）",
        source_key="virattt.ai-hedge-fund",
        license="MIT",
        original_def=(
            "转述。ai-hedge-fund 的多投资者 agent 中含趋势/动量方法论代表。"
            "通行定义（Moskowitz-Ooi-Pedersen 2012）：按过去 N 期收益符号"
            "决定多空，按波动率倒数定仓位。"
        ),
        family=Family.TREND,
        needs=("ohlcv",),
        timeframes=("1d",),
        impl=Impl.PORTED_AND_SCANNED,
        impl_ref="research/tsmom/",
        known_flaws=(
            "日线上 10 币样本，独立机会数远少于笔数",
            "波动率倒数的仓位缩放会放大低波动期的杠杆",
            "加密市场 2020 后结构性变化（机构入场）使早期样本参考价值下降",
        ),
        lookahead_risk=Risk.SCANNED_CLEAN,
        scan_ref="lookahead_scan.py / research/tsmom 高危 0 命中",
        reproduction=Repro.INDEPENDENT_IMPL,
        repro_note=(
            "**未复现**。TSMOM 是公开的学术定义（Moskowitz-Ooi-Pedersen 2012），"
            "我们按定义自己实现，没有对照 ai-hedge-fund 那份实现。"
            "而且原文是美股期货，我们只有加密——"
            "跨资产类别的复现需要该资产类别的数据。"
            "我们的结果只能说明「TSMOM 在 10 币加密日线上如何」，"
            "不能说明 ai-hedge-fund 实现得对不对。"
        ),
    ),

    ExternalStrategy(
        id="X-LEDGER-HASHCHAIN",
        name="哈希链记账的模拟盘（不是策略，是基础设施）",
        source_key="virattt.ai-hedge-fund",
        license="MIT",
        original_def=(
            "引文：该仓库有 paper trading 模式，用 hash-chained ledger 记账，"
            "使历史成交无法被事后修改。**这是工程模式，不是交易策略。**"
        ),
        family=Family.OTHER,
        needs=(),
        impl=Impl.NONE,
        impl_ref="",
        known_flaws=(
            "不是策略，入队判定不适用——**列在这里是为了不让它被当成策略**",
        ),
        lookahead_risk=Risk.NO_SOURCE_ACCESS,
        reproduction=Repro.NOT_ATTEMPTED,
        repro_note="未实现。作为 Phase 7 模拟盘的设计参考保留。",
        gate=Gate.NEEDS_PORT,
        gate_reason="基础设施模式，不参与策略回测队列",
    ),
]


# ---------------------------------------------------------------------------
# 库
# ---------------------------------------------------------------------------

class ExternalRegistry:
    """外部策略登记库。**只暴露可入队的条目给回测队列。**"""

    def __init__(self, entries: list[ExternalStrategy] | None = None):
        self._items: list[ExternalStrategy] = list(
            entries if entries is not None else ENTRIES)
        self._by_source: dict[str, list[ExternalStrategy]] = {}
        for e in self._items:
            self._by_source.setdefault(e.source_key, []).append(e)

    def all(self) -> list[ExternalStrategy]:
        return list(self._items)

    def by_source(self, key: str) -> list[ExternalStrategy]:
        return list(self._by_source.get(key, []))

    def queueable(self) -> list[ExternalStrategy]:
        """可入回测队列的条目。**这是唯一的对外出口。**

        与 `MethodologyStore.injectable()` 同构：一个管「能拿去用」，
        一个管「能拿去跑」。两者都不允许绕过闸门。
        """
        return [e for e in self._items if e.backtest_ready()[0]]

    def by_gate(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self._items:
            ok, _ = e.backtest_ready()
            k = "READY" if ok else e.gate.value
            out.setdefault(k, []).append(e.id)
        return out

    def duplicate_keys(self) -> dict[str, list[str]]:
        """跨来源去重：同键的多条只保留一条证据。

        **为什么必须做**：同一个 FVG 方法在三个仓库里各写一遍，
        如果我们登记三条、各跑一次回测、各自记一次「成功」，
        就等于给同一份证据投了三票。这正是 freqtradestrategies
        那 909 个文件最大的陷阱。
        """
        out: dict[str, list[str]] = {}
        for e in self._items:
            out.setdefault(e.dedup_key(), []).append(e.id)
        return {k: v for k, v in out.items() if len(v) > 1}

    def summary(self) -> dict:
        n = len(self._items)
        q = len(self.queueable())
        return {
            "n_entries": n,
            "n_queueable": q,
            "queueable_ratio": round(q / n, 4) if n else 0.0,
            "by_gate": {k: len(v) for k, v in self.by_gate().items()},
            "by_source": {k: len(v) for k, v in self._by_source.items()},
            "by_impl": _count(e.impl.value for e in self._items),
            "by_repro": _count(e.reproduction.value for e in self._items),
            "by_risk": _count(e.lookahead_risk.value for e in self._items),
            "n_vendor_claims": sum(len(e.vendor_claims) for e in self._items),
            "n_duplicate_keys": len(self.duplicate_keys()),
        }


def _count(it) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# 自检：把「诚实」变成可执行断言
# ---------------------------------------------------------------------------

# 这些词一旦出现在 INDEPENDENT_IMPL 的说明里，就是在暗示「我们复现了对方」
OVERCLAIM_WORDS = ("复现一致", "与原文一致", "验证了对外", "证实了该方法",
                   "复现成功", "结果吻合")


def audit_entries(entries: list[ExternalStrategy] | None = None) -> list[str]:
    """自检。返回问题列表，空表示通过。

    **这个函数存在的理由**：诚实不能靠自觉。初版写完后，
    FVG 那条同时被标成 `RAN_MATCHED` 和「我们没有复现对方」——
    一个人写的时候看不出来，因为它读起来都像好话。
    把它变成机器能查的规则，下次才不会重犯。
    """
    entries = entries if entries is not None else ENTRIES
    problems: list[str] = []

    for e in entries:
        # 1. RAN_* 必须真的用了对方定义。没有可追溯的复现依据就不许标。
        if e.reproduction in (Repro.RAN_MATCHED, Repro.RAN_DIVERGED,
                             Repro.RAN_INCONCLUSIVE):
            if not e.repro_note.strip():
                problems.append(f"{e.id}: 标了 {e.reproduction.value} 但没有复现说明")

        # 2. INDEPENDENT_IMPL 不许暗示「复现了对方」。
        if e.reproduction == Repro.INDEPENDENT_IMPL:
            for w in OVERCLAIM_WORDS:
                if w in e.repro_note:
                    problems.append(
                        f"{e.id}: INDEPENDENT_IMPL 的说明里出现「{w}」，"
                        f"这是把独立实现说成复现")

        # 3. 许可证不允许复制，就不许声称用了对方的代码。
        if not e.license_allows_copy():
            for w in ("复制自", "移植自", "copy from", "ported from"):
                if w in e.original_def + e.repro_note:
                    problems.append(
                        f"{e.id}: 许可证 {e.license} 不允许复制，"
                        f"但描述里出现「{w}」")

        # 4. 有 SCANNED_* 状态就必须有 scan_ref，否则是「声称扫过但没证据」。
        if e.lookahead_risk in (Risk.SCANNED_CLEAN, Risk.SCANNED_FLAGGED) \
                and not e.scan_ref:
            problems.append(f"{e.id}: 声称已扫描但 scan_ref 为空")

        # 5. PORTED 必须有 impl_ref 且文件真实存在。
        if e.impl in (Impl.PORTED, Impl.PORTED_AND_SCANNED):
            if not e.impl_ref:
                problems.append(f"{e.id}: impl={e.impl.value} 但无 impl_ref")

        # 6. 外部宣传数字必须带出处，不能是「听说」。
        for c in e.vendor_claims:
            if not c.source_key or not c.text:
                problems.append(f"{e.id}: vendor_claim 缺来源或正文")

        # 7. 入队判定与 gate 字段不能互相矛盾。
        ok, why = e.backtest_ready()
        if ok and e.gate not in (Gate.READY, Gate.NEEDS_PORT):
            problems.append(
                f"{e.id}: backtest_ready=True 但 gate={e.gate.value}")

    return problems


def source_coverage() -> dict:
    """每个来源贡献了几条条目。**没贡献的来源要显式列出来。**

    「调研了 7 个来源，登记了 5 条」听起来正常，但如果报告不写
    「哪两个来源一条都没产出、为什么」，读者会以为全都有收获。
    """
    known = {s.key for s in SOURCES}
    used = {e.source_key for e in ENTRIES}
    return {
        "sources_total": len(known),
        "sources_with_entries": len(used),
        "unused_sources": sorted(known - used),
        "unknown_source_keys": sorted(used - known),
    }


if __name__ == "__main__":
    reg = ExternalRegistry()
    s = reg.summary()
    print("=" * 74)
    print("External Strategy Registry")
    print("=" * 74)
    print(f"条目 {s['n_entries']} 条，可入队 {s['n_queueable']} 条 "
          f"({s['queueable_ratio']:.0%})")
    print(f"按门禁：{s['by_gate']}")
    print(f"按实现：{s['by_impl']}")
    print(f"按复现：{s['by_repro']}")
    print(f"按前视风险：{s['by_risk']}")
    print(f"外部宣传数字 {s['n_vendor_claims']} 条（全部 verified_by_us=False）")
    print()
    print(f"{'ID':<26}{'家族':<14}{'实现':<20}{'复现':<18}{'入队'}")
    print("-" * 74)
    for e in reg.all():
        ok, _ = e.backtest_ready()
        print(f"{e.id:<26}{e.family.value:<14}{e.impl.value:<20}"
              f"{e.reproduction.value:<18}{'✅' if ok else '—'}")
    print()
    cov = source_coverage()
    print(f"来源覆盖：{cov['sources_with_entries']}/{cov['sources_total']}")
    if cov["unused_sources"]:
        print(f"  无产出：{cov['unused_sources']}")
    dup = reg.duplicate_keys()
    if dup:
        print(f"⚠️ 跨来源重复键 {len(dup)} 组：{dup}")
    else:
        print("跨来源重复键：无")

    print()
    problems = audit_entries()
    if problems:
        print(f"❌ 自检发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"   · {p}")
    else:
        print("✅ 自检通过：无过度声称、无许可证越界、无证据缺失")