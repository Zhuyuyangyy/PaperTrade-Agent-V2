# BTC/ETH/SOL 多年历史回补与缓存口径（设计，未执行）

分支 `audit/v3-acceptance-fixes`。**本文件只设计，未拉取任何新数据、未创建缓存、未联网批量请求。**
日期 2026-11-25。目的：把"持续数据收集"与"S2b/S3 验证"合二为一，
解决 S2 只有 2 笔、S3 数据不可得的根因。

---

## 一、端点能力（本轮单点只读探测，4 次，无批量拉取）

| 数据 | 端点 | 能力 | 备注 |
|---|---|---|---|
| 永续 K 线 | `fapi.binance.com/fapi/v1/klines` | ✅ ETH/SOL 均通 | 1h limit≤1000/次 |
| 资金费率 | `fapi.binance.com/fapi/v1/fundingRate` | ✅ ETH/SOL 均通 | 8h，limit≤1000/次 |
| OI 历史 | `fapi.binance.com/futures/data/openInterestHist` | ⚠️ **仅约 30 天** | 31 天前报 `-1130` |
| 现货 K 线备用 | `data-api.binance.vision/api/v3/klines` | ✅ 通 | **无 funding（404）**，只可作 K 线备份 |

**硬约束**：funding 只有 fapi 有，vision 替代不了。所以 funding 必须走 fapi，
且必须遵守限频与 418 Immediate-Stop。

## 二、为什么必须先补数据（用数字说话）

| 实验 | 当前样本 | 契约门槛 | 差距 |
|---|---|---|---|
| S2 正式（BTC 4h 2年） | 2 笔 | min_fills=30 | 差 15 倍 |
| S2 诊断（去防追高） | 12 笔，样本外 0 | min_fills=30 + 5折 | 不够 |
| S3 跨币种配对 | 0（ETH/SOL 无缓存） | — | 全缺 |

BTC 单币 4h 上，压缩突破事件天然稀疏（2 年 47 个突破，五道过滤后 2 个）。
不扩资产/周期/时长，改对防追高也凑不出 30 笔。**这是数据供给问题，不是策略问题。**

## 三、回补范围与优先级

| 优先级 | 资产 | 数据 | 时长 | 频率 | 用途 |
|---|---|---|---|---|---|
| P0 | ETHUSDT | 永续 K线 + funding | 3 年 | 1h（4h 本地聚合） | S2b 增样本 + S3 |
| P0 | SOLUSDT | 永续 K线 + funding | 3 年 | 1h | S2b + S3 |
| P1 | BTCUSDT | 永续 K线 + funding | 3 年（现有 2 年扩展） | 1h | 三币统一窗口 |
| P2 | 三币 | OI 历史 | 30 天（端点上限，尽力） | 1h | 仅作 S1b 参考，不作契约依据 |

**不做**：下架币、幸存者偏差控制（那是 V4.1 第二阶段 PIT 资产池的任务，本轮范围只扩三币）。

## 四、缓存格式（与现有 `data/v3_funding/` 兼容）

```
data/v3_market/
  BTCUSDT/
    bars_1h.json     {"rows":[{"open_ms","open","high","low","close","volume","timeframe_ms"}...],
                      "meta":{"symbol","interval","fetched_at","sha256","source","gaps"}}
    funding.json     {"rows":[{"funding_ms","rate","mark_price"}...], "meta":{...}}
  ETHUSDT/ {...}
  SOLUSDT/ {...}
```

- **时间单位**：统一毫秒 UTC；K 线 `open_ms`，funding `funding_ms`（结算时刻）
- **已收盘语义**：`close_ms = open_ms + timeframe_ms`（E2 已确立）
- **meta.sha256**：数据指纹，写进实验配置，复算时校验
- **meta.gaps**：缺口清单（相邻 open_ms 差 ≠ step_ms 的位置），严禁静默补点
- **4h 不单独存**：由 1h 本地聚合（`aggregate_bars(bars, 4)`），避免两套数据漂移

## 五、拉取流程（严格限频 + 断点续传 + 418 Immediate-Stop）

1. **分页**：`klines` 每次 `limit=1000` + `startTime`，`fundingRate` 同样；
   3 年 ≈ 26,300 根 1h → K 线约 27 页/币，funding 约 10 页/币 → 三币合计约 111 次请求。
2. **限频**：每页之间 sleep ≥ 500ms；fapi 权重限制 2400/min，111 次远低于阈值，
   但历史上 418 是**累计请求**触发的，所以仍按最保守节奏走。
3. **重试策略**：仅网络超时/5xx 可重试 1 次；**418/429 立即停，读 `retry-after`，
   绝不重试、不换 IP、不切端点绕过**（用户全程硬约束）。
4. **断点续传**：每页落盘到 `data/v3_market/{sym}/{kind}.partial.jsonl`，
   记录最后 `open_ms`；中断后从该点续，不重头拉。
5. **完整性校验**：拉完校验 (a) 行数符合预期 (b) 无缺口 (c) 时间戳单调
   (d) funding 与 8h 网格对齐 (e) sha256 写入 meta。任一失败标 `INCOMPLETE`，不进实验。
6. **降级**：若 fapi 再被封，K 线可切 vision，但 funding 不可缺——
   缺 funding 的资产标 `NO_FUNDING`，只能跑 `funding=false` 的变体，且不得与有 funding 的资产混跑配对。

## 六、增量采集（回补完成后）

- 每日一次增量：从 meta 的最后时间戳拉到当前，追加 + 重算 sha256/gaps
- 与回补共用同一套限频/断点/校验逻辑，不写第二套
- 失败保留 partial，下次续；不覆盖已验证数据

## 七、与证伪引擎的对接（防呆已修）

回补后跑 S2b/S3 时：
- 每个 spec 必须带 `funding_path`（Bug2 已加 fail-fast：永续+外部K线缺 funding_path 直接报错）
- `min_fills` 不足时引擎硬短路 INCONCLUSIVE（Bug1 已修，不会再被网格覆写成假 FALSIFIED）
- 三币各自数据指纹写进 spec，复算时校验

## 八、执行边界（本轮）

**未执行**：未拉取任何新数据、未创建 `data/v3_market/`、未批量请求。
本轮只做了 4 次单点只读探测确认端点能力（klines/funding/OI/vision）。
真正回补需用户另行批准——因为它会产生网络流量、写缓存目录，且可能再次触发 418。

## 九、批准后执行顺序

1. 先拉 ETH/SOL P0 数据（P0 优先级），校验完整性
2. 扩 BTC 到 3 年，统一三币窗口
3. 数据到位后：锁 S2b-F / S2b-N × (4h,1h) 共 4 份契约
4. 解锁 S3（跨币种残差配对）—— 数据齐了无需改架构
5. OI 30 天仅作参考，不进任何契约门槛
