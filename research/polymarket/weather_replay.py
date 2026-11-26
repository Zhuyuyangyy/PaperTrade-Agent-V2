#!/usr/bin/env python3
"""
M1 Step 2：气象 METAR 温度市场的横截面回测（历史回放，不依赖实时窗口）。

为什么改用历史回放
------------------
实时录制只能覆盖一个整点窗口，样本量天然稀缺。而 `aviationweather.gov`
的 `/api/data/metar` 能回溯 48 小时且**带 receiptTime**——这就把
"市场何时第一次可能看到这条报文"变成了可精确重建的量。
Polymarket 那边的 `prices-history` 对气象 token 支持 60s 粒度回放，
已关闭市场的 token 也照样能取。两条历史序列一叠加，就能在不等待的前提下
做跨城市 × 多日的统计。

要回答的唯一问题
----------------
METAR 刷新当日新高 ⇒ 低于新高的档位在物理上确定归零（YES 必为 0）。
这个信息在 `receiptTime` 之后对所有人公开。问题是：

    那一刻之后，那些档位的 NO 还能不能以明显低于 1 的价格买到？

如果能，就是真 edge（付 p 拿 1，收益 (1-p)/p）；如果价格在 receiptTime
之前就已经崩到 0.99+，那说明市场价格由更快的渠道（专业气象商、雷达外推、
航班报文化肥）驱动，我们这条公开源没有信息优势。

判定口径（照抄市场细则，不自创）
------------------------------
1. 只算 routine 观测：行首为 SPECI 的不算。AviationWeather 的 metarType
   字段只有 METAR 一类，但仍按 rawOb 复核一遍。
2. 温度取 T 组（后 4 位是露点，丢弃）；无 T 组时退回正文 xx/xx 组。
3. 日期按**站点本地时区**切分，不是 UTC。
4. °F 档位：°C × 9/5 + 32 后四舍五入，恰好 .5 进位到更大整数。
5. 首份观测只建立基准，不算"刷新新高"。

诚实边界
--------
- prices-history 是**最后成交价**，不是可执行报价。它回答不了"可吃深度"和
  "盘口残留毫秒数"。本脚本只能回答"残留是否超过 60 秒"这个二元问题。
  要测秒级残留，必须用 WS 实时录制。
- 60s 粒度意味着所有时间差都被量化到分钟。小于 60s 的结论不可信。
- 历史价格在极低概率档位上可能长期无成交，此时"价格不动"是流动性缺失，
  不是"市场反应慢"。所以必须同时输出成交量分布以区分这两种情况。
"""
import argparse
import datetime
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

# 与 weather_probe / flat5m_probe 保持一致的同级导入：
# 这些脚本是当独立程序跑的（python research/polymarket/xxx.py），
# 不以包的形式被 import，所以要把自身目录塞进 sys.path。
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arb_probe as ap  # noqa: E402
import weather_probe as W  # noqa: E402


# 站点 → 时区。市场细则明确按站点本地时间切日，UTC 会错一整天。
STATION_TZ = {
    "KLGA": "America/New_York",
    "KBOS": "America/New_York",
    "KMDW": "America/Chicago",
    "KDEN": "America/Denver",
    "KLAX": "America/Los_Angeles",
    "KSEA": "America/Los_Angeles",
    "EGLC": "Europe/London",
    "EDDM": "Europe/Berlin",
    "LEMD": "Europe/Madrid",
    "EHAM": "Europe/Amsterdam",
    "LFPB": "Europe/Paris",
    "LIRF": "Europe/Rome",
}

# 批量接口的硬上限：请求超过 5 站时单站记录会被截断（实测 6 站即触发，
# 12 站时 KLGA 从 48 条掉到 22 条）。截断会低估当日最高温，
# 把仍有竞争力的档位误判成"已归零"——这是会静默给出假 edge 的错误。
MAX_BATCH = 5


def fetch_metars(icaos, hours=48):
    """按安全批量抓 METAR，绝不一次性丢 12 个站点进去。"""
    out = {}
    for i in range(0, len(icaos), MAX_BATCH):
        chunk = icaos[i:i + MAX_BATCH]
        body, _ = W.http_text(
            f"{W.AW_BASE}?ids={','.join(chunk)}&format=json&hours={hours}",
            timeout=45,
        )
        arr = json.loads(body)
        if isinstance(arr, dict):
            arr = [arr]
        for x in arr:
            out.setdefault(x["icaoId"], []).append(x)
    for ic in out:
        out[ic].sort(key=lambda x: x["obsTime"])
    return out


def routine_temps(rows, tz):
    """把一份日内的 METAR 序列压成 [(本地时刻, 温度°C, receipt_ts, raw)]。

    只留 routine 观测；SPECI 是特殊观测，细则明确不计入。
    """
    seq = []
    dropped_speci = 0
    for x in rows:
        raw = x.get("rawOb") or ""
        if raw.strip().upper().startswith("SPECI"):
            dropped_speci += 1
            continue
        tc, _src = W.parse_temp_c(raw) if raw else (None, None)
        if tc is None:
            # 退回 API 已解析的 temp 字段；但要注意它可能来自正文整度组，
            # 精度比 T 组低。仅在无 T 组时使用，口径与细则一致。
            t = x.get("temp")
            tc = float(t) if t is not None else None
        if tc is None:
            continue
        rcp = W._iso_ms(x.get("receiptTime"))
        rcp_ts = rcp / 1000.0 if rcp else None
        local = datetime.datetime.fromtimestamp(x["obsTime"], tz)
        seq.append((local, tc, rcp_ts, raw))
    return seq, dropped_speci


def day_of(ts_local):
    return ts_local.strftime("%Y-%m-%d")


def scan_new_highs(seq):
    """返回 [(idx, prev_hi_c, new_hi_c, prev_hi_f, new_hi_f)] —— 盘中刷新新高。

    首个观测只建立基准。刷新新高意味着 (prev_hi, new_hi) 之间所有档位
    在该时刻物理归零。
    """
    events = []
    hi = None
    for i, (local, tc, _rcp, _raw) in enumerate(seq):
        if hi is None:
            hi = tc
            continue
        if tc > hi + 1e-9:
            events.append((i, hi, tc, W.f_round(W.c_to_f(hi)), W.f_round(W.c_to_f(tc))))
            hi = tc
    return events


def slug_for(city_slug, day_local):
    return (f"highest-temperature-in-{city_slug}-on-"
            f"{day_local.strftime('%B').lower()}-{day_local.day}-{day_local.year}")


def find_event(city_slug, day_local):
    """按 slug 直接取事件（含已关闭的）。历史回放必须允许 closed=True。"""
    for _ in range(3):
        try:
            d = ap.http_json(f"{ap.GAMMA}/events?slug={slug_for(city_slug, day_local)}",
                             timeout=25)
            return d[0] if d else None
        except ap.ProbeError:
            time.sleep(1.0)
    return None


def bracket_tokens(ev):
    """复用 weather_probe.event_brackets 的解析，避免两套口径漂移。

    它已经处理了双单位（°C / °F 档位）、单值档、-999/999 开区间，
    并自带 unit 判定。已关闭事件同样适用——clobTokenIds 不会随结算消失。
    """
    rows, unit = W.event_brackets(ev)
    for r in rows:
        r["settled"] = None
    # 补上结算结果，用于事后核对"判定归零的档位"是否真的没中
    by_q = {}
    for m in ev.get("markets") or []:
        by_q[m.get("question")] = m.get("outcomePrices")
    for r in rows:
        r["settled"] = by_q.get(r["q"])
    return rows, unit


def price_hist(token, t0, t1, fidelity=1):
    """取价格历史。失败返回 []。

    fidelity 最小 1 分钟——这是接口的实际粒度下限，要更细只能实时录制。
    """
    try:
        d = ap.http_json(
            f"{ap.CLOB}/prices-history?market={token}"
            f"&startTs={int(t0)}&endTs={int(t1)}&fidelity={fidelity}",
            timeout=30,
        )
    except ap.ProbeError:
        return []
    h = (d or {}).get("history") or []
    out = []
    for x in h:
        try:
            t = int(x["t"])
            if t > 10_000_000_000:   # 防御：某天接口改成毫秒
                t //= 1000
            out.append((t, float(x["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def price_at(hist, ts_cap):
    """严格因果：只用 <= ts_cap 的最后一点，并回传该点时刻以便算陈旧度。"""
    px, pt = None, None
    for t, p in hist:
        if t <= ts_cap:
            px, pt = p, t
        else:
            break
    return px, pt


def analyze_station(icao, days_back, verbose=True):
    """单站逐日：找出归零事件，并在事件前后取 NO 价格。"""
    tz = ZoneInfo(STATION_TZ[icao])
    city = W.CITY_SLUG.get(icao)
    if not city:
        return []
    rows = fetch_metars([icao], hours=days_back * 24 + 12).get(icao, [])
    seq, dropped = routine_temps(rows, tz)
    if not seq:
        return []

    by_day = {}
    for item in seq:
        by_day.setdefault(day_of(item[0]), []).append(item)

    results = []
    for day, day_seq in sorted(by_day.items()):
        if len(day_seq) < 6:      # 半天以内的不判日高，避免"部分日"制造假新高
            continue
        ev = find_event(city, day_seq[0][0])
        if not ev:
            if verbose:
                print(f"  {icao} {day}: 事件未找到")
            continue
        brs, unit = bracket_tokens(ev)
        if not brs:
            continue

        highs = scan_new_highs(day_seq)
        for idx, prev_c, new_c, prev_f, new_f in highs:
            local, tc, rcp_ts, _raw = day_seq[idx]
            if rcp_ts is None:
                continue
            # 新高值换算成市场单位
            new_u = W.f_round(new_c) if unit == "C" else new_f
            # 物理归零：上界严格低于新高值的档位
            dead = [b for b in brs if b["hi"] < new_u and b["hi"] < 900]
            if not dead:
                continue
            # 观测时刻与可读时刻都必须落在同日才算（跨日边界不判）
            obs_ts = local.timestamp()
            if day_of(datetime.datetime.fromtimestamp(rcp_ts, tz)) != day:
                continue

            lo_t = int(obs_ts) - 3600
            hi_t = int(rcp_ts) + 7200
            for b in dead:
                h_no = price_hist(b["no"], lo_t, hi_t, fidelity=1)
                if not h_no:
                    continue
                p_pre, pre_t = price_at(h_no, obs_ts)          # 观测时刻（源方还没入库）
                p_post, post_t = price_at(h_no, rcp_ts)        # 刚可读
                p_late, late_t = price_at(h_no, rcp_ts + 1800)  # 30 分钟后

                # ---- 核心测量：跳变发生在 receiptTime 之前还是之后 ----
                # 只对"归零前确实还有分歧"的样本有意义：若跳变前 NO 已经 >=0.95，
                # 说明该档位早就被排除，无法从中读出"这一刻的信息何时被定价"。
                base = [p for t, p in h_no if obs_ts - 1800 <= t < obs_ts - 120]
                t_jump = None
                if base:
                    bmed = sorted(base)[len(base) // 2]
                    if bmed < 0.90:      # 有分歧才可测
                        for t, p in h_no:
                            if t >= obs_ts - 1800 and p >= 0.95:
                                t_jump = t
                                break
                results.append({
                    "icao": icao, "day": day, "unit": unit,
                    "bracket": f"{b['lo']}-{b['hi']}",
                    "obs_local": local.strftime("%H:%M"),
                    "obs_ts": obs_ts, "rcp_ts": rcp_ts,
                    "lag_s": round(rcp_ts - obs_ts, 0),
                    "prev_u": W.f_round(prev_c) if unit == "C" else prev_f,
                    "new_u": new_u,
                    "no_pre": p_pre, "no_pre_t": pre_t,
                    "no_post": p_post, "no_post_t": post_t,
                    "no_late": p_late, "no_late_t": late_t,
                    "no_base": (sorted(base)[len(base) // 2] if base else None),
                    "t_jump": t_jump,
                    "jump_vs_rcp_s": (round(t_jump - rcp_ts, 0) if t_jump else None),
                    "n_pts": len(h_no),
                    "settled": b["settled"],
                })
    return results


def summarize(rows):
    """核心问题：归零跳变发生在 receiptTime 之前还是之后。

    这比"可读时 NO 贵不贵"更本质。如果跳变系统性地早于 receiptTime，
    说明存在比 aviationweather 更快的渠道（专业气象商 / 雷达外推 /
    直连 NWS 的原始报文流），我们用公开源根本没有信息优势——
    晚 55 秒和晚 5 分钟，对能不能赚钱没有区别。
    """
    testable = [r for r in rows if r.get("jump_vs_rcp_s") is not None]

    print("\n" + "=" * 78)
    print("核心测量：归零跳变相对 receiptTime 的时间偏移")
    print("  负值 = 市场在源方入库之前就已定价（我们没有信息优势）")
    print("  正值 = 市场在源方入库之后才反应（存在可抢窗口）")
    print()
    if not testable:
        print("  无可测样本（所有归零档位在事件前 NO 已 >=0.90，无分歧可测）")
    else:
        off = sorted(r["jump_vs_rcp_s"] for r in testable)
        neg = [x for x in off if x < -30]
        mid = [x for x in off if -30 <= x <= 30]
        pos = [x for x in off if x > 30]
        print(f"  {'站':<6}{'日':<11}{'档位':<11}{'跳变前':>7}{'偏移':>8}"
              f"{'可读时':>8}")
        for r in testable:
            print(f"  {r['icao']:<6}{r['day']:<11}{r['bracket']:<11}"
                  f"{r['no_base']:>7.3f}{r['jump_vs_rcp_s']:>+8.0f}"
                  f"{(r['no_post'] or 0):>8.3f}")
        print()
        print(f"  n={len(off)}  偏移 p50 {off[len(off)//2]:+.0f}s  "
              f"min {off[0]:+.0f}s  max {off[-1]:+.0f}s")
        print(f"  跳变早于可读 >30s（提前定价）: {len(neg)}")
        print(f"  跳变在可读 ±30s 内（同步）    : {len(mid)}")
        print(f"  跳变晚于可读 >30s（有窗口）    : {len(pos)}")
        if neg and not pos:
            print("\n  ** 判定：市场抢在我们之前。公开源无信息优势。**")
        elif pos and not neg:
            print("\n  ** 判定：存在可抢窗口，需进一步测深度与费率。**")
        elif neg or pos:
            print("\n  混合结果，需按站/按档位细分。")

    # 事后价格同样要看：即使跳变在之前，可读时是否还能便宜买入
    usable = [r for r in rows if r["no_post"] is not None]
    print("\n" + "=" * 78)
    print("事后价格检查（可读时刻 NO 是否还有便宜挂单）")
    if not usable:
        print("  无可用 NO 历史价格")
        return
    cheap = [r for r in usable if r["no_post"] < 0.98]
    print(f"  可评估档位 {len(usable)} 个，其中可读时 NO < 0.98 的 "
          f"{len(cheap)} 个 ({len(cheap)/len(usable)*100:.0f}%)")
    if cheap:
        rets = sorted((1.0 - r["no_post"]) / r["no_post"] * 1e4 for r in cheap)
        print(f"    → 毛收益 bps: p50 {rets[len(rets)//2]:+.0f}  "
              f"最好 {rets[-1]:+.0f}（未扣费未计滑点）")
    else:
        print("  → 归零信息在 receiptTime 时刻已被完全定价")

    # 陈旧/流动性检查：历史点太少说明"价格不动"是没成交，不是反应慢
    thin = [r for r in usable if r["n_pts"] < 10]
    if thin:
        print(f"\n  警告：{len(thin)}/{len(usable)} 个档位历史点 <10 个，"
              f"价格可能只是挂单残留而非真实成交")

    # 归零判定是否被结算结果证实
    confirmed = 0
    checked = 0
    for r in rows:
        s = r.get("settled")
        if not s:
            continue
        checked += 1
        try:
            arr = json.loads(s) if isinstance(s, str) else s
            if float(arr[0]) == 0.0:      # 该档 YES 结算为 0 = 确实没中
                confirmed += 1
        except Exception:
            pass
    if checked:
        print(f"\n  归零判定核对：{confirmed}/{checked} 个档位结算为未中")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("--stations", default="KLGA,KMDW,KLAX,KSEA,EGLC,EDDM,LEMD")
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--out", default="")
    a = p.parse_args(argv)

    icaos = [s.strip().upper() for s in a.stations.split(",") if s.strip()]
    all_rows = []
    for ic in icaos:
        if ic not in STATION_TZ:
            print(f"{ic}: 无时区映射，跳过")
            continue
        try:
            rows = analyze_station(ic, a.days)
            print(f"{ic}: {len(rows)} 个归零档位样本")
            all_rows.extend(rows)
        except Exception as e:
            print(f"{ic}: 失败 {type(e).__name__} {e}")

    if all_rows:
        summarize(all_rows)
        out = (ap.DATA_DIR / f"weather_replay_{int(time.time())}.json")
        out.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        print(f"\n写入 {out}")
    else:
        print("\n没有样本")
    return 0


if __name__ == "__main__":
    sys.exit(main())