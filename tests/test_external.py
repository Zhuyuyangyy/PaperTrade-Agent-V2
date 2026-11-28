"""外部研究基础设施的回归测试。

守三件事，每件都对应一个**会静默出错**的地方：

1. **前视扫描器不能误报，也不能漏报**。误报让报告变成噪音（55 条里
   挑 3 条真的没人看），漏报让危险写法混过去。两个方向都要测。
2. **许可证判定必须保守**。判成 COPY_OK 而实际无授权，是法律问题，
   不是技术问题。宁严不宽。
3. **方法论状态机不能绕开隔离区**。任何让未毕业方法论进入下游的路径
   都是 bug——这正是 Minara 那套设计存在的唯一理由。

全部不联网。外部资源用 fixture 字符串，不依赖网络可达性。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "research" / "external"
sys.path.insert(0, str(EXT))

import lookahead_scan as LS     # noqa: E402
import methodology as M         # noqa: E402
import sources as S             # noqa: E402
import entry as E               # noqa: E402
import roles as R               # noqa: E402


# ---------------------------------------------------------------------------
# 1. 前视扫描器：真阳性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,rule", [
    # 居中窗口：一半是未来数据
    ("df['c'] = df['close'].rolling(5, center=True).mean()",
     "rolling_center"),
    # 负位移：把未来拉到现在
    ("df['t'] = df['close'].shift(-1)", "negative_shift"),
    # 负 diff
    ("df['d'] = df['close'].diff(-2)", "negative_diff"),
    # 后向填充：用后面的值回填前面
    ("s = s.bfill()", "backfill"),
    ("s = s.fillna(method='bfill')", "backfill"),
    # 后向插值
    ("s = s.interpolate(direction='backward')", "interpolate_back"),
    # 极值扫描：需要 i+n 的数据才能定 i 的极值
    ("idx = argrelextrema(df['close'].values, np.greater, order=5)",
     "extrema_scan"),
    ("p = find_peaks(df['high'].values)", "extrema_scan"),
    # 反转索引
    ("r = df['close'][::-1]", "reverse_index"),
])
def test_detects_real_lookahead(src, rule):
    rep = LS.scan_source(src)
    rules = {f.rule for f in rep.findings}
    assert rule in rules, f"漏报 {rule}：{src}"
    assert any(f.level == LS.HIGH for f in rep.findings if f.rule == rule)


def test_negative_shift_handles_unary_op():
    """`shift(-1)` 的 -1 是 UnaryOp(USub, Constant)，不是 Constant(-1)。

    外部文档专门点出这条。如果按 Constant 判断，negative_shift 命中率归零，
    而且不报错——静默失效。
    """
    rep = LS.scan_source("x = s.shift(-1)")
    assert any(f.rule == "negative_shift" for f in rep.findings)


def test_center_window_custom_detects_handwritten_pivot():
    """手写中心窗口必须被抓到。

    这是本项目最贵的 bug 形态：`structure.py` 的 pivot 判定就是
    `cs[i-left : i+right+1]`，和 rolling(center=True) 完全同一个错误，
    但匹配函数名的方法永远抓不到。
    """
    src = """
def pivots(cs, left=3, right=3):
    out = []
    for i in range(left, len(cs) - right):
        window = cs[i - left: i + right + 1]
        out.append(max(window))
    return out
"""
    rep = LS.scan_source(src)
    assert any(f.rule == "center_window_custom" for f in rep.findings), \
        "手写中心窗口漏报——这是本项目历史上最贵的 bug 形态"


def test_future_column_in_features_is_high():
    """预测列混进特征矩阵 = 硬证据，判高危。

    这是 `future_name` 唯一有判别力的形态：`df[['close','label']]` 里
    标签被当成输入喂给模型了。而单纯 `row['label']`（评估时读标签）
    无法判定，只能是中危。
    """
    rep = LS.scan_source("X = df[['close', 'volume', 'label']]")
    high = [f for f in rep.findings if f.level == LS.HIGH]
    assert any(f.rule == "future_name_in_features" for f in high), \
        f"特征矩阵含 label 却没判高危：{[f.rule for f in rep.findings]}"


def test_safe_target_columns_not_flagged():
    """`target_annual` / `target_weight` / `walk_forward` 不是预测视野。

    第一版正则会把这四个全部报成高危，在真实语料上 22 条命中里
    20 条是误报。误报率 90% 的规则等于没有规则——人会直接忽略整列。
    """
    for name in ("target_annual", "target_weight", "walk_forward",
                 "tag_labels", "label_name", "target_position"):
        rep = LS.scan_source(f"v = d['{name}']")
        assert not rep.findings, f"{name} 被误报成 {[f.rule for f in rep.findings]}"


def test_plain_label_read_is_only_medium():
    """单独读标签列是中危，不是高危。

    你要算一笔交易的收益，就必须读 `fwd_return`。把它判高危会让
    `research/verify/` 和 `research/replay/` 整片变红，然后没人再看。
    """
    rep = LS.scan_source("y = row['label']")
    assert not rep.flagged, "单独读 label 被判成高危"
    assert any(f.rule == "future_name" for f in rep.findings)
# ---------------------------------------------------------------------------
# 2. 前视扫描器：真阴性（误报会毁掉可用性）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,why", [
    # 右闭合滚动窗口 [i-n+1, i]：只看过去，完全正确
    ("w = vals[i - n + 1: i + 1]", "右闭合滚动窗口上界是 i+1"),
    # 右开滚动窗口 [i-n, i]：同样只看过去
    ("w = cs[i - window: i]", "右开滚动窗口上界就是 i"),
    # 标量 max：和全样本统计无关
    ("x = max(self.bars_held, 0)", "标量比较"),
    ("p = max(bar['o'], limit)", "标量比较"),
    ("m = min(bar['l'], e.level)", "标量比较"),
    # 有显式窗口的容器统计
    ("hi = max(b['h'] for b in cs[i - channel:i])", "有界窗口内的极值"),
    ("lo = min((b['l'] for b in w))", "w 来自有界切片"),
    # rolling 内的 max 是合法的
    ("m = df['close'].rolling(20).max()", "rolling 内的统计"),
    # 正向 shift
    ("prev = df['close'].shift(1)", "看过去"),
    # 正向 diff
    ("chg = df['close'].diff(1)", "和过去做差"),
    # ffill
    ("s = s.ffill()", "前向填充用过去的值"),
])
def test_no_false_positive(src, why):
    rep = LS.scan_source(src)
    high = [f for f in rep.findings if f.level == LS.HIGH]
    assert not high, f"误报（{why}）：{src} → {[f.rule for f in high]}"


def test_center_window_does_not_flag_right_closed_rolling():
    """`[i-n+1 : i+1]` 是最容易被误判成中心窗口的写法。

    Python 切片右开，`i+1` 恰恰是「包含 i」的标准写法。
    把它当中窗口报，会让 `rolling_extreme()` 和 `signals_more` 里
    所有正确的窗口统计全部变成噪音——第一版就是这么错的（命中 2 处，
    全是误报）。
    """
    rep = LS.scan_source("w = vals[i - n + 1: i + 1]\nhw = max(w)")
    assert not [f for f in rep.findings if f.rule == "center_window_custom"]


def test_scanner_never_executes_code():
    """扫描器必须是纯静态的——不 import、不 eval、不跑回测。

    用一段「一执行就会炸」的代码验证：如果扫描器真去执行它，
    测试就会失败。
    """
    src = """
raise SystemExit("这段代码被执行了，说明扫描器不是纯静态的")
"""
    rep = LS.scan_source(src)      # 不应抛异常
    assert isinstance(rep, LS.FileReport)


def test_syntax_error_is_reported_not_crashed():
    rep = LS.scan_source("def f(:\n  pass")
    assert rep.parse_error
    assert not rep.flagged


def test_own_cards_code_has_no_high_risk_findings():
    """我们自己的策略卡代码不能有高危前视命中。

    这条与 `tests/test_cards.py` 的截断因果性测试形成互补：
    截断测试证明「跑出来的结果没变」，静态扫描证明「写法里没有危险形态」。
    两者都过，才敢说结构层是干净的。
    """
    reports = LS.scan_dir(ROOT / "research" / "cards")
    flagged = [(r.path, [f.rule for f in r.findings if f.level == LS.HIGH])
               for r in reports if r.flagged]
    assert not flagged, f"策略卡代码有高危前视命中：{flagged}"


# ---------------------------------------------------------------------------
# 3. 许可证判定必须保守
# ---------------------------------------------------------------------------

def test_no_license_source_is_read_only():
    """无统一许可证 = 默认保留所有权利 = 只能读。

    freqtradestrategies 是典型：它自己写「collected from public sources;
    individual authors retain their rights」。这种不能整包复制。
    """
    src = next(s for s in S.SOURCES if s.key == "robotic.freqtrade-strategies")
    assert src.license == "NO_LICENSE"
    assert not src.can_copy_code, "无许可证的来源被判成可复制，这是法律风险"


def test_unknown_license_is_read_only():
    """许可证没确认就按最严处理。"""
    s = S.Source(key="x", name="x", url="x", kind="repo", license="UNKNOWN",
                 verified=S.PARTIAL, what_it_actually_is="x")
    assert not s.can_copy_code


def test_permissive_licenses_allow_copy():
    for lic in ("MIT", "Apache-2.0", "BSD-3-Clause"):
        s = S.Source(key="x", name="x", url="x", kind="repo", license=lic,
                     verified=S.VERIFIED, what_it_actually_is="x")
        assert s.can_copy_code, f"{lic} 应允许复制"


def test_every_source_has_verification_status():
    """每条来源必须标明我是否实际访问过。

    没有这一列，报告里就会出现「看起来权威但实际没验证过」的条目。
    """
    valid = {S.VERIFIED, S.PARTIAL, S.UNVERIFIED, S.NOT_FOUND}
    for s in S.SOURCES:
        assert s.verified in valid, f"{s.key} 的 verified={s.verified} 非法"
        assert s.what_it_actually_is, f"{s.key} 缺少「实际是什么」的描述"


def test_no_source_claims_unverified_scale_numbers():
    """自述规模数字（92 skills / 478 因子 / 909 策略）必须带 caveat。

    这些数字来自 README 自述，未经我方核验。直接写进报告会变成
    「我们调研发现它有 478 个因子」——那是替对方转述宣传。
    """
    for s in S.SOURCES:
        text = s.what_it_actually_is
        has_big_number = any(t in text for t in ("92", "478", "909", "137"))
        if has_big_number:
            assert s.caveats, f"{s.key} 引用了规模数字但没有 caveat"
            joined = " ".join(s.caveats)
            assert any(k in joined for k in ("未", "不得", "不是", "高估")), \
                f"{s.key} 的 caveat 没有点明数字的局限"


def test_corrections_exist_and_have_verdict():
    """用户摘要里的偏差必须被记录下来，且每条有裁决。"""
    assert len(S.CORRECTIONS) >= 3
    valid = {"CORRECT", "CORRECT_AND_UNDERSTATED", "PARTIALLY_WRONG",
             "OUTDATED", "MISLEADING", "NOT_FOUND"}
    for c in S.CORRECTIONS:
        assert c["verdict"] in valid
        assert c["detail"] and c["note"]


# ---------------------------------------------------------------------------
# 4. 方法论状态机
# ---------------------------------------------------------------------------

def _mk(**kw) -> M.Methodology:
    base = dict(id="M1", name="t", card_id="S20", asset_class="major_crypto",
                direction="bullish", primary_signal="momentum",
                timeframe="medium", indicators=("atr",))
    base.update(kw)
    return M.Methodology(**base)


def test_wilson_lower_punishes_small_samples():
    """1 次全中的下界必须远低于 30 次中 15 次。

    这是整套门槛存在的理由。如果这条不成立，我们就是在用点估计做决策，
    等于把「跑了 3 笔全赢」和「944 笔稳定」当成同一回事。
    """
    lucky = M.wilson_lower(1, 1)
    steady = M.wilson_lower(15, 30)
    assert lucky < steady, f"1/1={lucky:.3f} 应低于 15/30={steady:.3f}"


def test_wilson_lower_zero_evidence_is_zero_not_half():
    """n=0 返回 0，不是 0.5。

    返回 0.5 等于给一个全新的方法论「一半可信度」，那正好绕开隔离区。
    """
    assert M.wilson_lower(0, 0) == 0.0


def test_new_methodology_starts_quarantined():
    m = _mk()
    assert m.quarantine is True
    assert m.confidence == 0.1
    assert m.state == M.State.DRAFT


def test_quarantined_methodology_never_injected():
    """未毕业的方法论不能进入下游。这是隔离区的唯一职责。"""
    store = M.MethodologyStore()
    m = _mk()
    store.add(m)
    m.to_implemented()
    m.to_backtested(n_trades=100)
    # 即使记录了一堆成功，没走 try_graduate 就不能注入
    for _ in range(50):
        m.record_outcome(True)
    assert m.quarantine is True
    assert store.injectable() == []


def test_graduation_needs_both_thresholds():
    """双门槛：置信度够但次数不够，或反之，都不能毕业。"""
    m = _mk()
    m.to_implemented()
    m.to_backtested(n_trades=100)

    # 次数不够（只 5 次，全对）
    for _ in range(5):
        m.record_outcome(True)
    ok, why = m.try_graduate()
    assert not ok and "独立验证" in why

    # 凑够次数但成功率低
    m2 = _mk(id="M2")
    m2.to_implemented()
    m2.to_backtested(n_trades=100)
    for k in range(40):
        m2.record_outcome(k % 3 == 0)      # 约 33% 成功
    ok2, why2 = m2.try_graduate()
    assert not ok2 and "置信度" in why2


def test_graduation_succeeds_with_enough_good_evidence():
    m = _mk()
    m.to_implemented()
    m.to_backtested(n_trades=500)
    for _ in range(M.MIN_USES):
        m.record_outcome(True)             # 30 次全对 → conf = 0.886
    ok, why = m.try_graduate()
    assert ok, why
    assert m.state == M.State.VALIDATED
    assert m.quarantine is False


def test_graduated_methodology_becomes_injectable():
    store = M.MethodologyStore()
    m = _mk()
    store.add(m)
    m.to_implemented()
    m.to_backtested(n_trades=500)
    for _ in range(M.MIN_USES):
        m.record_outcome(True)
    m.try_graduate()
    assert [x.id for x in store.injectable()] == ["M1"]


def test_rejected_cannot_graduate_even_with_perfect_record():
    """已证伪的方法论不能靠攒成功洗白。"""
    m = _mk()
    m.mark_rejected("样本外衰减 98%")
    ok, why = m.try_graduate()
    assert not ok and "REJECTED" in why


def test_data_blocked_cannot_graduate():
    """缺数据的方法论永远不能毕业——它是冻着的，不是攒着。"""
    m = _mk()
    m.mark_data_blocked("缺历史 open_interest")
    ok, why = m.try_graduate()
    assert not ok and "DATA_BLOCKED" in why


def test_zero_trades_cannot_be_marked_backtested():
    """零成交不是「回测完成」。

    这是 S03/S04 静默零信号那个 bug 的教训：如果允许零成交晋级，
    一个因为下标bug恒为零信号的卡会顺利走完流程，然后我们会以为
    「它跑过了，只是没机会」。
    """
    m = _mk()
    m.to_implemented()
    with pytest.raises(ValueError, match="零成交"):
        m.to_backtested(n_trades=0)


def test_dedup_key_ignores_free_text():
    """去重键只用结构化字段，自由文本不参与。"""
    a = _mk(id="A", name="突破入场", indicators=("ATR", "rsi"))
    b = _mk(id="B", name="买入当价格突破时", indicators=("rsi", "atr"))
    assert a.dedup_key() == b.dedup_key(), "指标顺序不应影响去重"
    assert a.name != b.name


def test_store_rejects_duplicate_and_returns_existing():
    """同键第二次 add 必须被拒，否则同一方法写 10 次就有 10 份证据。"""
    store = M.MethodologyStore()
    ok1, _ = store.add(_mk(id="A"))
    ok2, why = store.add(_mk(id="B"))
    assert ok1 and not ok2
    assert "重复" in why
    assert store.summary()["n_methodologies"] == 1


def test_demotion_requires_reason():
    """降级必须留原因，否则半年后没人知道为什么。"""
    m = _mk()
    m.to_implemented()
    m.to_backtested(n_trades=500)
    for _ in range(M.MIN_USES):
        m.record_outcome(True)
    m.try_graduate()
    assert m.state == M.State.VALIDATED

    for _ in range(M.DEMOTE_AFTER_FAILURES):
        m.record_outcome(False)
    demoted, reason = m.check_demote()
    assert demoted
    assert reason and m.demote_reason == reason
    assert m.state == M.State.DEMOTED
    assert m.quarantine is True


def test_demotion_uses_consecutive_not_cumulative_failures():
    """降级看**连续**失败，不是累计失败。

    这条测试针对一个「有代码、有测试、没有行为」的 bug：
    30 次全对后连续 5 次失败，累计失败只有 5/35，Wilson 下界从
    0.886 掉到 0.706，仍高于 0.62——按累计+置信度的写法，
    降级永远不触发，机制沦为装饰。
    """
    m = _mk()
    m.to_implemented()
    m.to_backtested(n_trades=500)
    for _ in range(M.MIN_USES):
        m.record_outcome(True)
    m.try_graduate()

    # 中间成功过几次，累计失败仍是 5 次，但不连续
    for _ in range(3):
        m.record_outcome(False)
        m.record_outcome(True)
    for _ in range(M.DEMOTE_AFTER_FAILURES - 1):
        m.record_outcome(False)
    ok, _ = m.check_demote()
    assert not ok, "累计失败达到阈值但中间有成功，不该降级"

    # 补到真正连续
    m.record_outcome(False)
    ok2, why2 = m.check_demote()
    assert ok2, f"连续 {M.DEMOTE_AFTER_FAILURES} 次失败却没降级：{why2}"


def test_demoted_cannot_re_graduate_on_old_evidence():
    """降级后必须**重新挣证据**，不能吃掉降级前的老本。

    不加 `evidence_floor` 的写法会立刻重新毕业：
    降级那一刻 times_used 已经 >= 30、confidence 也还在门槛上，
    下一次 try_graduate() 直接返回 True。降级等于没做。
    """
    m = _mk()
    m.to_implemented()
    m.to_backtested(n_trades=500)
    for _ in range(M.MIN_USES):
        m.record_outcome(True)
    m.try_graduate()

    for _ in range(M.DEMOTE_AFTER_FAILURES):
        m.record_outcome(False)
    demoted, _ = m.check_demote()
    assert demoted

    ok, why = m.try_graduate()
    assert not ok, "降级后靠老证据原地复职——evidence_floor 失效"
    assert "本期新增" in why


def test_lifecycle_is_append_only_audit_trail():
    m = _mk()
    m.to_implemented("代码就绪")
    m.to_backtested(n_trades=100, note="100 笔")
    assert len(m.lifecycle) == 2
    assert m.lifecycle[0][:2] == ("DRAFT", "IMPLEMENTED")
    assert m.lifecycle[1][:2] == ("IMPLEMENTED", "BACKTESTED")


def test_our_three_candidates_would_all_fail_graduation():
    """把本轮三个 4h 候选的真实证据喂进去，**两个都该被卡住**。

    这不是悲观，是这套门槛存在的意义。三个候选在三币上的正负：
      S20: +0.53 / +0.15 / +0.97  → 3/3 为正
      S08: +0.25 / +0.32 / +0.77  → 3/3 为正，但合并 CI 跨 0
      S03: +0.09 / +0.21 / +1.42  → 3/3 为正，但样本外衰减 98%

    即便给 S20 记 3 次独立成功，n=3 的 Wilson 下界也只有
    wilson_lower(3,3)=0.439 < 0.62，**毕业不了**。
    这正是我们要的：3 个币不足以宣称验证通过，必须扩到 10–15 币。
    """
    m = _mk(id="S20-ch55", card_id="S20")
    m.to_implemented()
    m.to_backtested(n_trades=944)
    for _ in range(3):                     # 三个币各算一次独立验证
        m.record_outcome(True)
    ok, why = m.try_graduate()
    assert not ok, "3 币全为正竟然毕业了——门槛失效"
    assert m.confidence < M.CONF_THRESHOLD
    assert m.confidence == pytest.approx(M.wilson_lower(3, 3), abs=1e-9)


def test_min_uses_is_at_least_year_times_symbol():
    """MIN_USES 不能低于「币数 × 年数」的独立机会数下限。

    3 币 × 7 年 = 21 个独立机会。门槛定得比这还低，等于允许
    「三个币同向」就晋级——而我们已经知道三币一致为正可能是三个
    都不精确的正数（S20 的教训）。
    """
    assert M.MIN_USES >= 21
    assert M.CONF_THRESHOLD > 0.55, "门槛不应低于 Minara 的单资产口径"


# ---------------------------------------------------------------------------
# 5. 外部策略条目层（External Strategy Registry）
# ---------------------------------------------------------------------------

def _entry(**kw) -> E.ExternalStrategy:
    base = dict(
        id="X1", name="t", source_key="minara.docs", license="MIT",
        original_def="转述", family=E.Family.TREND,
        impl=E.Impl.PORTED, impl_ref="research/cards/x.py",
        lookahead_risk=E.Risk.SCANNED_CLEAN, scan_ref="scan",
        reproduction=E.Repro.INDEPENDENT_IMPL,
        gate=E.Gate.READY,
    )
    base.update(kw)
    return E.ExternalStrategy(**base)


def test_vendor_claim_can_never_be_marked_verified():
    """外部宣传数字在结构上不可能被标成「我方已核实」。

    用户任务书原话：「不得将外部宣传收益当成验证结果。」
    光靠人记住这条不够——`verified_by_us` 是 `init=False` 的字段，
    构造函数里根本传不进去，改它只能显式赋值。而 `to_dict()` 里
    又硬编码返回 False，所以连 dump 都漏不出去。
    """
    c = E.VendorClaim(text="年化 300%", source_key="minara.docs")
    assert c.verified_by_us is False
    assert c.to_dict()["verified_by_us"] is False
    # 传参也不该成功
    with pytest.raises(TypeError):
        E.VendorClaim(text="x", source_key="y", verified_by_us=True)


def test_reproduction_default_is_not_attempted():
    """默认复现值必须是 NOT_ATTEMPTED，不能是「已复现」。

    默认值选错会造成最坏的一类错误：新加一条条目，忘了填复现状态，
    系统就自动宣称「复现一致」。默认必须是最保守的那个。

    这里**不传 reproduction**，让 dataclass 自己填默认值——
    上一版手动赋 `NOT_ATTEMPTED` 再断言，等于什么都没测。
    """
    e = E.ExternalStrategy(
        id="X1", name="t", source_key="minara.docs", license="MIT",
        original_def="转述", family=E.Family.TREND)
    assert e.reproduction is E.Repro.NOT_ATTEMPTED
    assert e.impl is E.Impl.NONE
    assert e.lookahead_risk is E.Risk.UNSCANNED


def test_independent_impl_cannot_claim_consistency():
    """标 INDEPENDENT_IMPL 却又说「与原文一致」→ 自检必须报。

    这是初版真实犯过的错：FVG 那条同时写着 `RAN_MATCHED` 和
    「我们没有复现对方」。两句话都是好话，拼在一起就是过度声称。
    """
    bad = _entry(repro_note="我们的独立实现与原文一致，复现成功")
    problems = E.audit_entries([bad])
    assert problems, "过度声称没被检出"
    assert any("复现" in p for p in problems)


def test_no_license_entry_cannot_claim_ported():
    """无许可证来源不许说「移植自」。"""
    bad = _entry(license="NO_LICENSE",
                 original_def="代码移植自该仓库")
    problems = E.audit_entries([bad])
    assert any("不允许复制" in p for p in problems)


def test_scanned_status_requires_scan_ref():
    """声称扫过就必须有扫描证据。"""
    bad = _entry(lookahead_risk=E.Risk.SCANNED_CLEAN, scan_ref="")
    problems = E.audit_entries([bad])
    assert any("scan_ref" in p for p in problems)


def test_ported_without_impl_ref_is_blocked():
    """impl 标了 PORTED 但 impl_ref 为空 = 状态与事实不符，不得入队。"""
    e = _entry(impl=E.Impl.PORTED, impl_ref="")
    ok, why = e.backtest_ready()
    assert not ok and "impl_ref" in why


def test_flagged_lookahead_blocks_queue():
    """扫出高危前视的条目不进入回测队列。"""
    e = _entry(lookahead_risk=E.Risk.SCANNED_FLAGGED)
    ok, why = e.backtest_ready()
    assert not ok and "前视" in why


def test_unscanned_but_implemented_is_blocked():
    """有可执行实现却没扫描——最危险的状态：能跑，但不知道跑的是不是真的。"""
    e = _entry(lookahead_risk=E.Risk.UNSCANNED)
    ok, why = e.backtest_ready()
    assert not ok and "未做前视扫描" in why


def test_read_only_license_requires_own_implementation():
    """READ_ONLY ≠ 不能研究，但必须自己重写。

    `REF_ONLY` 意味着指着别人的代码——无许可证时这是侵权，
    不是技术选择。
    """
    e = _entry(license="NO_LICENSE", impl=E.Impl.REF_ONLY,
               impl_ref="vendor/x.py")
    ok, why = e.backtest_ready()
    assert not ok and "自行重写" in why

    # 自己重写就可以跑
    e2 = _entry(license="NO_LICENSE", impl=E.Impl.PORTED,
                impl_ref="research/cards/ours.py")
    ok2, _ = e2.backtest_ready()
    assert ok2, "自己重写的实现应当允许入队"


def test_queueable_is_the_only_export():
    """只有过闸门的条目能从 queueable 出去。"""
    good = _entry(id="GOOD")
    bad = _entry(id="BAD", lookahead_risk=E.Risk.CONFIRMED_LOOKAHEAD)
    reg = E.ExternalRegistry([good, bad])
    assert [e.id for e in reg.queueable()] == ["GOOD"]


def test_cross_source_duplicates_are_detected():
    """同一个方法在三个仓库各写一遍，只能算一条证据。

    这正是 freqtradestrategies 那 909 个文件最大的陷阱：
    家族聚集造成有效样本量被严重高估。
    """
    a = _entry(id="A")
    b = _entry(id="B")          # 同 family / asset_class / tf / needs
    reg = E.ExternalRegistry([a, b])
    dup = reg.duplicate_keys()
    assert len(dup) == 1
    assert set(next(iter(dup.values()))) == {"A", "B"}
    assert reg.summary()["n_duplicate_keys"] == 1


def test_every_entry_points_to_known_source():
    """条目引用的 source_key 必须真实存在，不能是编的。"""
    cov = E.source_coverage()
    assert not cov["unknown_source_keys"], \
        f"条目引用了不存在的来源：{cov['unknown_source_keys']}"


def test_production_entries_pass_audit():
    """生产登记表本身必须过自检。"""
    problems = E.audit_entries()
    assert not problems, f"登记表自检失败：{problems}"


def test_no_production_entry_claims_reproduction():
    """当前登记表里没有任何一条声称复现了外部策略——这是事实，也是约束。

    我们确实一条都没复现：五条里四条是独立实现，一条是基础设施模式。
    如果将来有人把它改成 RAN_MATCHED，必须同时提供复现依据并
    在这里显式放行，而不是悄悄改掉。
    """
    for e in E.ENTRIES:
        assert e.reproduction != E.Repro.RAN_MATCHED, \
            f"{e.id} 声称复现一致，但本轮没有任何一条做过对照复现"

# ---------------------------------------------------------------------------
# 6. 角色卡库
# ---------------------------------------------------------------------------

def test_every_role_allows_abstain_or_blocked():
    """每个角色都必须允许「我判断不了」。

    LLM 角色的头号退化形态是硬编答案：如果你只给它 PASS/FAIL 两个选项，
    它总会挑一个，哪怕输入根本不够。这不是模型能力问题，是接口设计问题。
    """
    for r in R.ROLES:
        assert {R.Verdict.ABSTAIN, R.Verdict.BLOCKED} & set(r.verdict_options), \
            f"{r.id} 不允许弃权，它会在信息不足时硬编一个结论"


def test_role_rejects_verdict_without_evidence():
    """判 PASS/FAIL 但证据字段为空 → 必须被拒。

    「结论是 FAIL 但说不出哪个数字支持它」不是裁决，是表态。
    """
    role = next(r for r in R.ROLES if r.id == "R-COST")
    ok, why = role.check_output(R.Verdict.PASS, {})
    assert not ok and "证据" in why

    ok2, _ = role.check_output(R.Verdict.PASS, {
        "gross_expectancy": 0.68, "net_expectancy": 0.53,
        "cost_per_trade": 0.15, "n_trades": 311})
    assert ok2


def test_role_rejects_verdict_not_in_options():
    role = next(r for r in R.ROLES if r.id == "R-COST")
    ok, why = role.check_output(R.Verdict.BLOCKED, {"missing_slot": "x"})
    assert not ok and "不接受" in why


def test_abstain_and_blocked_need_reasons():
    role = next(r for r in R.ROLES if r.id == "R-REGIME")
    ok, why = role.check_output(R.Verdict.ABSTAIN, {})
    assert not ok and "reason" in why
    ok2, why2 = role.check_output(R.Verdict.BLOCKED, {})
    assert not ok2 and "槽" in why2

    # 补上缺槽声明后应当通过
    ok3, _ = role.check_output(R.Verdict.BLOCKED,
                               {"missing_slot": "news"})
    assert ok3


def test_route_by_slots_not_by_plan():
    """路由必须按**手里有什么输入**决定，不按计划里写了几个角色。

    如果写死「每次跑五个角色」，缺 news 的角色会拿到空输入然后
    硬编一个结论——**能跑什么取决于手里有什么**。
    """
    lib = R.RoleLibrary()
    can, blocked = lib.route({R.Slot.BACKTEST_RUN})
    assert "R-COST" in [r.id for r in blocked], "缺 costs 槽却没被拦"
    assert "R-MULTIPLICITY" in [r.id for r in can], "只需要 backtest_run 的应能跑"
    assert all(set(r.slots_in) <= {R.Slot.BACKTEST_RUN} for r in can)


def test_blocked_reason_names_missing_slot():
    lib = R.RoleLibrary()
    role = lib.get("R-EXTERNAL")
    why = lib.blocked_reason(role, {R.Slot.OHLCV})
    assert "external_method" in why


def test_roles_pass_audit():
    problems = R.audit_roles()
    assert not problems, f"角色卡自检失败：{problems}"


def test_general_practice_role_must_explain_itself():
    """来源不明的角色必须显式说明，不能让它看起来像有出处。"""
    bad = R.RoleCard(
        id="X", name="x", question="这样对吗？",
        slots_in=(R.Slot.OHLCV,),
        verdict_options=(R.Verdict.PASS, R.Verdict.FAIL, R.Verdict.ABSTAIN),
        must_quote=("a",), hard_bans=("b",), anti_pattern="c",
        basis=R.Basis.GENERAL_PRACTICE, basis_note="")
    problems = R.audit_roles([bad])
    assert any("GENERAL_PRACTICE" in p for p in problems)


def test_role_without_hard_bans_is_rejected():
    bad = R.RoleCard(
        id="X", name="x", question="这样对吗？",
        slots_in=(R.Slot.OHLCV,),
        verdict_options=(R.Verdict.ABSTAIN,),
        anti_pattern="c", basis=R.Basis.PROJECT_HISTORY, basis_note="d")
    problems = R.audit_roles([bad])
    assert any("hard_bans" in p for p in problems)


def test_no_role_claims_primary_source_reading():
    """本轮没有任何一条角色读过外部原文，不许标 READ_PRIMARY。

    这是防自欺的最后一道：角色设计看起来越专业，越容易让人以为
    「我们研究了他们的提示词」。事实是本轮只读到架构描述。
    """
    for r in R.ROLES:
        assert r.basis != R.Basis.READ_PRIMARY, \
            f"{r.id} 声称读过原文，但本轮没有逐字读过任何外部提示词"
