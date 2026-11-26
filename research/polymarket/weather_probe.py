#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 2：气象 METAR 最高温市场的跨整点时间差探测（纯只读）。

为什么这块值得测
----------------
- 每个城市每天只有 11 个档位，单档几百到几千美元，大机构懒得为 20+ 城市写
  定制解析器。但"懒得做"不等于"有 edge"——必须先量出信息落地到盘口改价的真实墙钟时间。
- 结算源是 **aviationweather.gov 的 METAR 原文**（描述里写死 URL，不是二手源）。
  这点很关键：如果所有人读的是同一个免费接口，那 edge 只能来自
  ①解析更快 ②订阅更快 ③对"已确定归零"的档位撤单慢。

两个子任务
----------
1. `sources`：实测公开源刷新速度与 `obsTime → 可读` 延迟，找出最快的源。
   已实测：aviationweather REST 单次 ~1.7-2.0s，`receiptTime − obsTime ≈ 487s`
   （即报文本身在观测后 8 分钟才进系统，这是**所有人的共同起点**，不是我的劣势）。
2. `watch`：跨整点双路录制。每 1–2s 轮询 METAR，同步订阅 Polymarket CLOB WS，
   重点量"突破归零"事件：当日实测最高温刷新后，**所有更低档位在物理上已确定归零**，
   量这些档位的 bid 被撤光的耗时与残留美元深度。

红线
----
- 绝不假设"归零档位的 ask 还能买到便宜货"：归零意味着它永不赔付，
  任何 > 0 的买入都是纯亏。这里量的是**反向**机会：归零档位的 **bid**
  在物理确定后仍挂着，这才是可吃的（卖出等于白拿钱）。
- 只读：不下单、不签名、不碰账户。
- 只统计"物理确定"事件（新高 >= 当前最高档下沿），模糊情景一律不计。

用法
----
    python research/polymarket/weather_probe.py sources
    python research/polymarket/weather_probe.py watch --cities KLGA,KMDW --minutes 70
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

import websocket

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arb_probe as ap  # noqa: E402

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
AW_BASE = "https://aviationweather.gov/api/data/metar"
TG_BASE = "https://tgftp.nws.noaa.gov/data/observations/metar/stations"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = {"User-Agent": "PaperTrade-WeatherProbe/0.1 (research; read-only)"}

# 结算描述里钉死的解析规则
T_GROUP = re.compile(r"\bT([01])(\d{3})(\d{4})\b")
BODY_TEMP = re.compile(r"\b(M?\d{2})/(M?\d{2})\b")
STATION_TIME = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")


def http_text(url: str, timeout: float = 20.0, retries: int = 1) -> tuple[str, float]:
    last = None
    for i in range(retries + 1):
        t = time.time()
        try:
            with _OPENER.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            return body, (time.time() - t) * 1000
        except Exception as exc:                       # noqa: BLE001
            last = exc
            if i < retries:
                time.sleep(0.5)
    raise last                                         # type: ignore[misc]


# ---------------------------------------------------------------- METAR 解析

def parse_temp_c(raw: str):
    """按市场描述的顺序取温：先 T 组（tenths °C），再正文 xx/xx（整 °C）。

    描述明确：`T01390133` = 13.9°C；`18/12` = 18°C；`M11/M17` = −11°C。
    """
    if not raw:
        return None, None
    m = T_GROUP.search(raw)
    if m:
        sign = -1 if m.group(1) == "1" else 1
        return sign * int(m.group(2)) / 10.0, "T-group"
    m = BODY_TEMP.search(raw)
    if m:
        def conv(s):
            neg = s.startswith("M")
            return (-1 if neg else 1) * int(s.lstrip("M"))
        return float(conv(m.group(1))), "body"
    return None, None


def parse_obs(raw: str):
    """取报文里的观测时刻（DDHHMMZ）。返回 (day, hour, minute)。"""
    m = STATION_TIME.search(raw or "")
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except ValueError:
        return None


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _obs_day(obs) -> int | None:
    """obs 是 parse_obs 返回的 (day, hour, minute)，取 day-of-month。"""
    if not obs:
        return None
    try:
        return int(obs[0])
    except (TypeError, IndexError, ValueError):
        return None


def f_round(x: float) -> int:
    """描述规定：小数点后恰好 .5 的进位到更大的整数。"""
    import math
    return int(math.floor(x + 0.5))


def aw_metars(icao: str, hours: int = 3):
    """取原始 METAR 列表（最老在前）。返回 [(raw, receipt_ms|None, obs_ms|None)]。"""
    body, _ms = http_text(f"{AW_BASE}?ids={icao}&format=raw&hours={hours}", timeout=20)
    return {"raw": [l.strip() for l in body.splitlines() if l.strip()]}


def aw_batch_json(icaos: list[str], hours: int = 2):
    """一次拿多站。

    实测 8 站 1.7s，单站 1.5-2.1s —— 批量几乎免费，是能否做到 1s 级轮询的关键。
    返回 {icao: latest_record}，每站只留 obsTime 最新的那条。
    """
    ids = ",".join(icaos)
    body, _ms = http_text(f"{AW_BASE}?ids={ids}&format=json&hours={hours}", timeout=25)
    try:
        arr = json.loads(body)
    except json.JSONDecodeError:
        return {}
    best = {}
    for x in arr:
        ic = x.get("icaoId")
        if not ic:
            continue
        if ic not in best or (x.get("obsTime") or 0) > (best[ic].get("obsTime") or 0):
            best[ic] = x
    return best


# ---------------------------------------------------------------- 源测速

def cmd_sources(args):
    icaos = [c.strip().upper() for c in args.cities.split(",") if c.strip()]
    print(f"源测速：{len(icaos)} 站 × {args.rounds} 轮\n")
    rows = []
    for icao in icaos:
        for i in range(args.rounds):
            rec = {"icao": icao, "round": i}
            try:
                t = time.time()
                body, ms = http_text(f"{AW_BASE}?ids={icao}&format=raw&hours=2", timeout=20)
                rec["aw_raw_ms"] = round(ms)
            except Exception as exc:                    # noqa: BLE001
                rec["aw_raw_ms"] = None
                rec["aw_raw_err"] = type(exc).__name__
            try:
                best = aw_batch_json([icao], hours=2)
                latest = best.get(icao)
                if latest:
                    obs = latest.get("obsTime")
                    rcp = latest.get("receiptTime")
                    rec["obsTime"] = obs
                    rec["receiptTime"] = rcp
                    if obs and rcp:
                        rec["obs_to_receipt_s"] = round(
                            (_iso_ms(rcp) - obs * 1000) / 1000, 1)
                    rec["temp"] = latest.get("temp")
                    rec["metarType"] = latest.get("metarType")
            except Exception as exc:                    # noqa: BLE001
                rec["aw_json_err"] = type(exc).__name__
            try:
                _b, tgms = http_text(f"{TG_BASE}/{icao}.TXT", timeout=15)
                rec["tgftp_ms"] = round(tgms)
            except Exception as exc:                    # noqa: BLE001
                rec["tgftp_ms"] = None
            rows.append(rec)
            print(f"  {icao} r{i}: aw_raw {rec.get('aw_raw_ms')}ms  "
                  f"tgftp {rec.get('tgftp_ms')}ms  "
                  f"obs→receipt {rec.get('obs_to_receipt_s')}s  "
                  f"temp {rec.get('temp')}", flush=True)
            time.sleep(0.5)

    def summarize(key):
        v = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if not v:
            return None
        v.sort()
        return {"n": len(v), "p50": v[len(v) // 2], "min": v[0], "max": v[-1]}

    print("\n汇总")
    for key in ("aw_raw_ms", "tgftp_ms", "obs_to_receipt_s"):
        s = summarize(key)
        print(f"  {key:<18} {s if s else '无数据'}")
    out = ap.DATA_DIR / f"weather_sources_{int(time.time())}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")


def _iso_ms(s: str) -> int:
    """'2026-11-25T14:54:07.530Z' → 毫秒。失败返回 0。"""
    import datetime
    try:
        dt = datetime.datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
        return int(dt.timestamp() * 1000)
    except Exception:
        try:
            dt = datetime.datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0


# ---------------------------------------------------------------- 市场发现

CITY_SLUG = {
    "KLGA": "nyc", "KMDW": "chicago", "KLAX": "los-angeles", "KSEA": "seattle",
    "KSFO": "san-francisco", "KBOS": "boston", "KDCA": "washington-dc",
    "KMIA": "miami", "KPHL": "philadelphia", "KDEN": "denver", "KATL": "atlanta",
    "KHOU": "houston", "KLAS": "las-vegas", "KPHX": "phoenix",
    "EGLL": "london", "LFPB": "paris", "EDDM": "munich", "LEMD": "madrid",
    "LIRF": "rome", "EHAM": "amsterdam", "RJTT": "tokyo", "RKSI": "seoul",
    "ZBAA": "beijing", "ZSPD": "shanghai", "ZGSZ": "shenzhen",
    "ZUUU": "chengdu", "ZHHH": "wuhan", "ZGGG": "guangzhou",
    "VHHH": "hong-kong", "WSSS": "singapore", "RCSS": "taipei",
    "RKPK": "busan", "EDDF": "frankfurt", "LOWW": "vienna", "LSZH": "zurich",
    "EFHK": "helsinki", "UUEE": "moscow", "LTFM": "istanbul",
}

# 香港天文台不是 METAR 站，官方源不同（HKO 开放数据），本脚本暂不支持 ——
# 明确排除而不是静默失败，避免把它当成"发现了 0 个机会"。
NON_METAR = {"VHHH": "香港天文台（非 METAR，需 HKO 开放数据，本脚本未实现）"}


def probe_icao(ev: dict) -> str | None:
    """从市场描述里反查该站的真实 ICAO。

    比硬编码城市→ICAO 映射可靠得多：巴黎实际用 LFPB（Le Bourget）不是 LFPG，
    香港干脆不是 METAR 站。描述里通常写成 "(KLGA)" 或 "ids=KLGA"。
    """
    ms = ev.get("markets") or []
    if not ms:
        return None
    desc = ms[0].get("description") or ""
    # 优先取 resolutionSource 里的 ids=
    src = ms[0].get("resolutionSource") or ""
    m = re.search(r"[?&]ids=([A-Z]{4})", src)
    if m:
        return m.group(1)
    # 再取描述里括号中的 4 位大写
    cands = re.findall(r"\(([A-Z]{4})\)", desc)
    for c in cands:
        if c not in ("METAR", "SPECI"):
            return c
    # 最后取 "ids=XXXX"
    m = re.search(r"ids=([A-Z]{4})", desc)
    return m.group(1) if m else None


def find_by_city(city: str, days: int = 2):
    """按 city slug 找最近一个未关闭的温度事件（今天→未来 days 天）。"""
    import datetime
    for d in range(0, days):
        day2 = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=d)
        slug = (f"highest-temperature-in-{city}-on-"
                f"{day2.strftime('%B').lower()}-{day2.day}-{day2.year}")
        try:
            r = ap.http_json(f"{ap.GAMMA}/events?slug={slug}", timeout=15)
        except ap.ProbeError:
            continue
        if not r:
            continue
        e = r[0]
        ms = e.get("markets") or []
        if e.get("closed") or not ms:
            continue
        # 至少要有可交易档位，否则这条市场今天已经停了
        if not any(m.get("acceptingOrders") for m in ms):
            continue
        return e
    return None


def find_city_events(icao: str, days: int = 3):
    """找某站对应的当日温度事件。

    两条路并行：先按 `highest-temperature-in-{city}-on-{month}-{day}-{year}`
    直接构造 slug（最可靠，不受"活跃度前 50"限制），失败再退回 tag 扫描 + 描述匹配 ICAO。
    """
    import datetime
    out, seen = [], set()
    city = CITY_SLUG.get(icao.upper())
    if city:
        for d in range(-1, days):
            day2 = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=d)
            slug = (f"highest-temperature-in-{city}-on-"
                    f"{day2.strftime('%B').lower()}-{day2.day}-{day2.year}")
            try:
                d_ = ap.http_json(f"{ap.GAMMA}/events?slug={slug}", timeout=15)
            except ap.ProbeError:
                continue
            if not d_:
                continue
            e = d_[0]
            if e.get("closed"):
                continue
            ms = e.get("markets") or []
            if not ms:
                continue
            if icao.upper() not in (ms[0].get("description") or "").upper():
                continue
            if slug in seen:
                continue
            seen.add(slug)
            out.append(e)
        if out:
            return out
    # 退化路径：tag 扫描
    for off in range(0, 300, 50):
        try:
            evs = ap.http_json(f"{ap.GAMMA}/events?limit=50&offset={off}"
                               f"&closed=false&active=true&order=volume24hr&ascending=false"
                               f"&tag_slug=highest-temperature", timeout=25)
        except ap.ProbeError:
            break
        if not evs:
            break
        for e in evs:
            ms = e.get("markets") or []
            if ms and icao.upper() in (ms[0].get("description") or "").upper():
                if e.get("slug") not in seen:
                    seen.add(e.get("slug"))
                    out.append(e)
    return out


def event_brackets(ev: dict):
    """把事件的温度档位整理成 (lo, hi, market)，统一归一到 **摄氏度整数** 区间。

    两种市场并存（实测）：
    - 美国站用 °F 档位，描述里写明「states its range in whole degrees Fahrenheit」
      并把按 °C 的实测值换算、四舍五入（恰好 .5 进位）。
    - 日/欧/亚站用 °C 档位，描述写「in whole degrees Celsius」。

    所以必须按市场自己声明的单位解析，再用同一单位比较，否则跨城市会错档。
    返回里额外带 `unit`，后续归零判定用它把温度换成同一单位。
    """
    ms = ev.get("markets") or []
    if not ms:
        return [], "F"
    desc = ms[0].get("description") or ""
    # 必须匹配"声明句"，不能只查 '°C' 是否出现——°F 市场的描述里也会提到 °C
    # 换算规则（如 "18.3°C is 64.94°F"），只查符号必然误判。
    if re.search(r"states its range in whole degrees Celsius", desc):
        unit = "C"
    elif re.search(r"states its range in whole degrees Fahrenheit", desc):
        unit = "F"
    else:
        # 兜底：看档位文案里出现的符号（去重后唯一的那种）
        units = set(re.findall(r"°([CF])", " ".join(
            (m.get("question") or "") for m in ms)))
        unit = "C" if units == {"C"} else "F"
    sym = "°C" if unit == "C" else "°F"
    rows = []
    for m in ms:
        q = (m.get("question") or "")
        lo = hi = None
        mm = re.search(r"be\s+(-?\d+)" + sym + r"\s+or\s+below", q)
        if mm:
            lo, hi = -999, int(mm.group(1))
        else:
            mm = re.search(r"be\s+(-?\d+)" + sym + r"\s+or\s+higher", q)
            if mm:
                lo, hi = int(mm.group(1)), 999
            else:
                mm = re.search(r"between\s+(-?\d+)-(-?\d+)" + sym, q)
                if mm:
                    lo, hi = int(mm.group(1)), int(mm.group(2))
                else:
                    mm = re.search(r"be\s+(-?\d+)" + sym + r"\s+on\b", q)
                    if mm:                      # 单值档（如 "be 10°C on Nov 26"）
                        v = int(mm.group(1))
                        lo, hi = v, v
        if lo is None:
            continue
        toks = ap._tokens(m)
        if not toks:
            continue
        rows.append({"lo": lo, "hi": hi, "q": q, "yes": toks[0],
                     "no": toks[1] if len(toks) > 1 else None,
                     "cond": m.get("conditionId"),
                     "bid": ap._f(m.get("bestBid")), "ask": ap._f(m.get("bestAsk")),
                     "vol": ap._f(m.get("volume")) or 0.0,
                     "closed": bool(m.get("closed")),
                     "accepting": bool(m.get("acceptingOrders"))})
    rows.sort(key=lambda r: r["lo"])
    return rows, unit


# ---------------------------------------------------------------- 跨整点录制

def _depth_usd(levels) -> float:
    return sum(p * s for p, s in (levels or []) if p is not None and s)


class WeatherWatch:
    def __init__(self, cities, poll_s: float = 1.5):
        self.cities = cities            # [(icao, event, brackets, unit)]
        self.poll_s = poll_s
        self.lock = threading.Lock()
        self.quotes = {}                # token -> {bid, ask, bid_levels, ts}
        self.updates = collections.defaultdict(list)
        self.metar = collections.defaultdict(list)   # icao -> [(ts_ms, raw, temp_c)]
        self.events = []                # 归零事件
        self.msgs = 0
        self.stop = False
        self.day_high = {}              # icao -> (temp_c, ts_ms, obs_day)
        self.obs_lag = collections.defaultdict(list)   # icao -> [obs→receipt 秒]
        self.req_ms = []                # 每次批量请求的耗时

    def tokens(self):
        """YES 和 NO 都要订阅。

        归零档位要"白拿钱"，现实路径是**买入 NO**（付 1−bid，结算拿 1），
        因为 Polymarket 的卖单需要先持有份额。NO 的真实 ask 未必等于 1−YES bid，
        不订阅就量不出真实可吃价。
        """
        out = []
        for _icao, _ev, brs, _u in self.cities:
            for b in brs:
                out.append(b["yes"])
                if b.get("no"):
                    out.append(b["no"])
        return out

    def on_poly(self, msg, now_ms):
        et = msg.get("event_type")
        with self.lock:
            self.msgs += 1
            if et == "book":
                tok = msg.get("asset_id")
                lb = {}
                asks = sorted((ap._f(l.get("price")), ap._f(l.get("size")))
                              for l in (msg.get("asks") or []))
                bids = sorted(((ap._f(l.get("price")), ap._f(l.get("size")))
                               for l in (msg.get("bids") or [])), reverse=True)
                asks = [(p, s) for p, s in asks if p is not None and s]
                bids = [(p, s) for p, s in bids if p is not None and s]
                q = self.quotes.setdefault(tok, {})
                q["ask"] = asks[0][0] if asks else None
                q["bid"] = bids[0][0] if bids else None
                q["ask_levels"] = asks[:10]
                q["bid_levels"] = bids[:10]
                q["ts"] = now_ms
                self._rec(tok, now_ms, q)
                return
            if et != "price_change":
                return
            for ch in (msg.get("price_changes") or []):
                tok = ch.get("asset_id")
                if not tok:
                    continue
                q = self.quotes.setdefault(tok, {})
                bb, ba = ap._f(ch.get("best_bid")), ap._f(ch.get("best_ask"))
                if bb is not None:
                    q["bid"] = bb
                if ba is not None:
                    q["ask"] = ba
                q["ts"] = now_ms
                self._rec(tok, now_ms, q)

    def _rec(self, tok, now_ms, q):
        h = self.updates[tok]
        if h and h[-1][1] == q.get("bid") and h[-1][2] == q.get("ask"):
            return                      # 同一价位的重复推送不计入
        h.append((now_ms, q.get("bid"), q.get("ask")))

    # ---------- METAR 轮询 ----------
    def poll_loop(self):
        """批量轮询所有站，一次请求拿到全部。

        延迟记两段，缺一不可：
        - `obs → 可读`：obsTime 到本地首次看到的时刻。这是**无法靠工程缩短**的，
          但它决定了「别人也能看到」的绝对时刻，是真正的起跑线。
        - `收包耗时`：请求本身的 RTT，这部分才是我能优化的。
        """
        icaos = [c[0] for c in self.cities]
        while not self.stop:
            t_req = time.time()
            try:
                best = aw_batch_json(icaos, hours=2)
            except Exception:
                time.sleep(self.poll_s)
                continue
            now = int(t_req * 1000)
            with self.lock:
                self.req_ms.append((time.time() - t_req) * 1000)
                for icao, rec in best.items():
                    obs = rec.get("obsTime")
                    if obs is None:
                        continue
                    hist = self.metar[icao]
                    if hist and hist[-1][1] == obs:
                        continue
                    raw = rec.get("rawOb") or ""
                    temp_c, src = parse_temp_c(raw)
                    # 用 receiptTime（数据源自己的入库时间）比用本地收包更客观
                    rcp = rec.get("receiptTime")
                    lag_s = None
                    if rcp:
                        rms = _iso_ms(rcp)
                        if rms and obs:
                            lag_s = (rms - obs * 1000) / 1000
                    hist.append((now, obs, temp_c, raw, src))
                    self.obs_lag[icao].append(lag_s)
                    if temp_c is not None:
                        self._check_breach(icao, temp_c, now, parse_obs(raw))
            time.sleep(self.poll_s)

    def _check_breach(self, icao, temp_c, now_ms, obs):
        """物理确定归零判定。

        关键：市场问的是**当日最高温**，即当日所有 routine 观测里温度的最大值，
        不是最新一报的值。所以必须累积当日 max，且只能拿同一观测日的报文比。

        `obs` 是 DDHHMMZ 里的 (day, hour, minute)。换日要清空当日 max，
        否则昨天的高温会把今天全部档位误判成归零。

        单位：METAR 永远是 °C；档位可能是 °C 也可能是 °F，按该市场的 unit 换算。
        """
        day = _obs_day(obs) if obs is not None else None
        # 先按各市场单位算出"当日最高温"——不同市场单位不同，需要分别累积
        cur = self.day_high.get(icao)
        cur_c = cur[0] if cur else None          # 统一用 °C 累积，避免换算误差累积
        if cur is not None and day is not None and cur[2] is not None and cur[2] != day:
            self.day_high[icao] = (temp_c, now_ms, day)
            return                      # 刚换日，拿单报去判归零不安全，跳过
        if cur is None:
            self.day_high[icao] = (temp_c, now_ms, day)
            return                      # 首报不构成"刷新新高"，不触发
        if temp_c <= cur_c + 1e-9:
            return                      # 没刷新新高，没有新的物理确定
        self.day_high[icao] = (temp_c, now_ms, day)

        for _icao, ev, brs, unit in self.cities:
            if _icao != icao:
                continue
            # 把当日最高温换成该市场的单位
            hi = f_round(c_to_f(temp_c)) if unit == "F" else f_round(temp_c)
            dead = [b for b in brs if b["hi"] < hi and b["hi"] < 900]
            if not dead:
                continue
            snap = {}
            for b in dead:
                qy = self.quotes.get(b["yes"]) or {}
                qn = self.quotes.get(b["no"]) or {} if b.get("no") else {}
                snap[b["yes"]] = {
                    "range": f"{b['lo']}-{b['hi']}",
                    "yes_bid": qy.get("bid"), "yes_ask": qy.get("ask"),
                    "no_ask": qn.get("ask"), "no_ask_usd": _depth_usd(qn.get("ask_levels")),
                    "yes_bid_usd": _depth_usd(qy.get("bid_levels")),
                }
            self.events.append({"t_metar": now_ms, "icao": icao, "obs": obs,
                                "temp_c": temp_c, "unit": unit, "day_high": hi,
                                "prev_high_c": cur_c, "day": day,
                                "title": ev.get("title"), "dead": snap})


def cmd_watch(args):
    # 用户给的是 ICAO 列表；先按 slug 表找到 event，再用 probe_icao 校正真实站点。
    # 允许直接给 city（如 paris），用于映射表没收录的城市。
    keys = [c.strip().lower() for c in args.cities.split(",") if c.strip()]
    cities = []
    icao_to_city = {v: k for k, v in CITY_SLUG.items()}
    for key in keys:
        if len(key) == 4 and key.upper() in CITY_SLUG:
            city, hinted = CITY_SLUG[key.upper()], key.upper()
        elif key in icao_to_city:
            city, hinted = key, icao_to_city[key]
        else:
            city, hinted = key, None
        if hinted in NON_METAR:
            print(f"  {hinted}: 跳过 — {NON_METAR[hinted]}")
            continue
        ev = find_by_city(city)
        if not ev:
            print(f"  {key}: 未找到当日未关闭事件（city slug '{city}'）")
            continue
        brs, unit = event_brackets(ev)
        if not brs:
            print(f"  {key}: 档位解析失败")
            continue
        icao = probe_icao(ev) or hinted
        if not icao:
            print(f"  {key}: 无法确定 ICAO，跳过")
            continue
        if icao in NON_METAR:
            print(f"  {icao}: 跳过 — {NON_METAR[icao]}")
            continue
        cities.append((icao, ev, brs, unit))
        print(f"  {icao}: {ev.get('title')[:50]}  {len(brs)} 档 [{unit}]  "
              f"YES vol {sum(b['vol'] for b in brs):,.0f}")
    if not cities:
        print("没有可用城市")
        return 1

    w = WeatherWatch(cities, poll_s=args.poll_s)
    toks = w.tokens()
    print(f"订阅 {len(toks)} 个 token，METAR 轮询 {args.poll_s}s\n")

    def poly_run():
        while not w.stop:
            try:
                ws = websocket.create_connection(POLY_WS, timeout=20,
                                                 http_proxy_host=None,
                                                 http_no_proxy=["*"],
                                                 enable_multithread=True)

                def ping():
                    while not w.stop:
                        time.sleep(8)
                        try:
                            ws.send("PING")
                        except Exception:
                            return
                threading.Thread(target=ping, daemon=True).start()
                ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                while not w.stop:
                    ws.settimeout(5)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw or raw.strip().upper() == "PONG":
                        continue
                    now = int(time.time() * 1000)
                    d = json.loads(raw)
                    for it in (d if isinstance(d, list) else [d]):
                        w.on_poly(it, now)
            except Exception:
                if w.stop:
                    break
                time.sleep(1.5)

    threading.Thread(target=poly_run, daemon=True).start()
    threading.Thread(target=w.poll_loop, daemon=True).start()

    t0 = time.time()
    while time.time() - t0 < args.minutes * 60:
        time.sleep(10)
        with w.lock:
            print(f"  {time.time()-t0:5.0f}s poly {w.msgs:>6d} "
                  f"metar {sum(len(v) for v in w.metar.values()):>3d} "
                  f"归零事件 {len(w.events)}", flush=True)
    w.stop = True
    time.sleep(1)

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("数据源延迟（决定起跑线的唯一数字）")
    all_lag = [x for v in w.obs_lag.values() for x in v if x is not None]
    if all_lag:
        all_lag.sort()
        print(f"  obs → 源方入库（receiptTime − obsTime）："
              f"p50 {all_lag[len(all_lag)//2]:.0f}s  "
              f"min {all_lag[0]:.0f}s  max {all_lag[-1]:.0f}s  n={len(all_lag)}")
        print(f"  → 这段时间内**所有人都看不到**这条报文，不是我慢，是源方慢")
    for icao, v in sorted(w.obs_lag.items()):
        vv = [x for x in v if x is not None]
        if vv:
            print(f"    {icao}: n={len(vv)} p50 {sorted(vv)[len(vv)//2]:.0f}s  "
                  f"min {min(vv):.0f}s")
    if w.req_ms:
        rq = sorted(w.req_ms)
        print(f"  批量请求 RTT：p50 {rq[len(rq)//2]:.0f}ms  p90 {rq[int(len(rq)*0.9)]:.0f}ms"
              f"  n={len(rq)}（这才是能优化的部分）")

    for icao, ev, brs, unit in cities:
        sym = "°C" if unit == "C" else "°F"
        print(f"\n{icao}  {ev.get('title')}  [{sym} 档位]")
        hist = w.metar.get(icao) or []
        print(f"  METAR 更新 {len(hist)} 条")
        for ts, obs, tc, raw, src in hist[-4:]:
            mark = f"{tc:.1f}°C ({src})" if tc is not None else "无温度"
            print(f"    +{ts-t0:6.0f}s obs {obs}  {mark}")
        hi = w.day_high.get(icao)
        if not hi:
            continue
        hi_u = f_round(c_to_f(hi[0])) if unit == "F" else f_round(hi[0])
        print(f"  当日实测最高 {hi[0]:.1f}°C = {hi_u}{sym}（观测日 {hi[2]}）")
        dead = [b for b in brs if b["hi"] < hi_u and b["hi"] < 900]
        live = [b for b in brs if b["hi"] >= hi_u or b["hi"] >= 900]
        print(f"  物理确定归零 {len(dead)} 档 / 仍在竞争 {len(live)} 档")
        if dead:
            print(f"  {'档位':<12}{'YES bid':>8}{'NO ask':>8}{'NO 可吃$':>10}"
                  f"{'买NO成本':>9}{'净收益bps':>10}")
            for b in dead:
                qy = w.quotes.get(b["yes"]) or {}
                qn = w.quotes.get(b["no"]) or {} if b.get("no") else {}
                no_ask = qn.get("ask")
                if no_ask is None:
                    print(f"  {b['lo']}-{b['hi']}{sym:<8}{str(qy.get('bid')):>8}"
                          f"{'—':>8}{'无卖单':>10}{'—':>9}{'—':>10}")
                    continue
                # 买 NO：付 no_ask，必然结算拿 1（该档已确定不中）
                # 费率：weather_fees，taker feeRate 0.01
                fee_ps = 0.01 * no_ask * (1 - no_ask)
                net = (1.0 - no_ask - fee_ps) / no_ask * 1e4
                usd = _depth_usd(qn.get("ask_levels"))
                print(f"  {b['lo']}-{b['hi']}{sym:<8}{str(qy.get('bid')):>8}"
                      f"{no_ask:>8.3f}{usd:>10,.0f}{no_ask:>9.3f}{net:>+10.0f}")

    print(f"\n归零事件 {len(w.events)} 个")
    for e in w.events:
        tot_no = sum(v.get("no_ask_usd") or 0 for v in e["dead"].values())
        cheap = [v for v in e["dead"].values()
                 if v.get("no_ask") is not None and v["no_ask"] < 0.98]
        print(f"  {e['icao']} t=+{e['t_metar']-t0:.0f}s 实测 {e['temp_c']:.1f}°C "
              f"当日高 {e['day_high']}{e['unit']}  归零 {len(e['dead'])} 档  "
              f"低价 NO 卖单 {len(cheap)} 个  可吃 ${tot_no:,.0f}")

    # 直接给出可执行结论：归零瞬间还买得到便宜 NO 吗
    all_bins = {}
    for e in w.events:
        for k, v in e["dead"].items():
            all_bins.setdefault(v["range"], v)
    print(f"\n{'='*78}\n可执行性结论")
    if not all_bins:
        print("  本次录制未捕捉到归零事件（METAR 未刷新当日新高）")
    else:
        cheap = [v for v in all_bins.values()
                 if v.get("no_ask") is not None and v["no_ask"] < 0.98]
        print(f"  归零档位 {len(all_bins)} 个，其中 NO 卖一 <0.98 的 {len(cheap)} 个")
        if cheap:
            print("  ** 存在可吃窗口 **——归零后仍有低于 0.98 的 NO 卖单")
        else:
            print("  归零档位的 NO 卖单已全部 >=0.98 或无挂单 → 物理确定信息已被完全定价")

    out = ap.DATA_DIR / f"weather_watch_{int(time.time())}.json"
    out.write_text(json.dumps({
        "generated_utc": int(time.time() * 1000),
        "cities": [{"icao": i, "title": ev.get("title"), "unit": u} for i, ev, _b, u in cities],
        "metar": {k: v for k, v in w.metar.items()},
        "events": w.events,
        "day_high_f": {k: v[0] for k, v in w.day_high.items()},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="气象 METAR 温度市场跨整点时间差探测")
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("sources", help="公开气象源测速")
    s1.add_argument("--cities", default="KLGA,KMDW,KLAX,KSEA,EGLL")
    s1.add_argument("--rounds", type=int, default=2)
    s1.set_defaults(func=cmd_sources)

    s2 = sub.add_parser("watch", help="跨整点双路录制")
    s2.add_argument("--cities", default="KLGA,KMDW")
    s2.add_argument("--minutes", type=float, default=70)
    s2.add_argument("--poll-s", type=float, default=1.5)
    s2.set_defaults(func=cmd_watch)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())