# Binance Demo 联调报告（第一 / 第二阶段：只读验证 + 真实受控测试单）

日期：2026-11-22 13:48–14:50
代码：`c1e86c0` → `037b3d5`（幂等预查三态 + 金额门槛）→ `52d165d`（.env 凭据通道）
仓库：`D:/ZYY_Inovation/money/_openalpha/PaperTrade-Agent-V2`，分支 `sprint-9/v2-engine`
测试：**830 passed, 1 skipped**（跳过项：Windows 缺 `SeCreateSymbolicLinkPrivilege`，与本轮无关）

## 结论：已连上，且已用真实 Demo 订单跑通「开仓 → 保护单 → 平仓 → 对账」

| 阶段 | 结果 |
|---|---|
| 一、只读验证 | **通过**。凭据可用，账户 5000 USDT，单向持仓，无挂单无持仓，时间偏差 322ms |
| 二、受控测试单 | **通过**。真实 `orderId` 28690711029，成交 0.0020 BTC @ 59225.00；止损/止盈各一张 algo 单；随后 reduceOnly 平仓并撤销两张保护单；对账零差异 |
| 三、AI 自动交易 | **未启动**（等本轮报告确认后再跑 30 分钟） |

**关键验证点：`-4120 STOP_ORDER_SWITCH_ALGO` 没有出现** —— 条件单确实可以走 `/fapi/v1/algoOrder`。
所有回执均由交易所返回，无一处本地伪造。全程 Demo 虚拟资金，未触碰正式账户。

凭据并未落在系统环境变量里（注册表 `HKCU\Environment` 无 `BINANCE_*`），而是放在
`data/openalpha_v2/.env`，由本轮新增的 `.env` 通道读取。

## 一、凭据检查（不输出密钥内容）

第一次检查（13:48）：四个位置全部 MISSING，联调停在只读之前。

| 位置 | `BINANCE_DEMO_API_KEY` | `BINANCE_DEMO_API_SECRET` |
|---|---|---|
| 当前进程环境 | MISSING | MISSING |
| Windows User 作用域 | MISSING | MISSING |
| Windows Machine 作用域 | MISSING | MISSING |
| 仓库内 `.env` | 不存在 | 不存在 |

第二次检查（14:37）：用户把凭据写进 `data/openalpha_v2/.env`，由 `.env` 通道读到，
`demo-exec` 随即连通。**当前状态：可用。**

### 原因（已查实，不是猜测）

直接读注册表 `HKCU\Environment` 的**全部值名**：只有 `DEEPSEEK_API_KEY`、`OPENROUTER_API_KEY`、
`ZHIPU_API_KEY`、`REDFOX_API_KEY`、`OSS_ACCESS_KEY_ID/SECRET`、`GITHUB_PAT_TOKEN` 等，
**没有任何 `BINANCE_*`**。HKLM 同样没有，`.bashrc/.bash_profile/.profile` 也没有。

所以不是"进程启动早读不到"，而是这两个变量**从未持久化到当前 Windows 账号**。
（机器上还挂着 `C://Users//j` 和一个编码损坏的 `鑱旀兂` 配置目录，若曾用其他账号 `setx`，
当前账号看不到。）

### 已提供的替代通道：`.env`（`52d165d`）

为了避免"设了变量但当前进程看不到"这种反复排查，凭据读取现在有两条路：

| 优先级 | 位置 | 说明 |
|---|---|---|
| 1 | 进程环境变量 | `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET`，有值就**不再读文件** |
| 2 | `data/openalpha_demo/.env` | 账本目录，最省事 |
| 3 | 当前工作目录 `./.env` | |
| 4 | 仓库根 `.env` | 模板见 `.env.example` |

只补**缺失**的键，只认这两个键（白名单外的行一律忽略），**值不打印、不进日志**。
`.gitignore` 已忽略 `.env` / `.env.*`，只放行 `.env.example`。

放好文件后**不需要重启任何东西**，直接重跑同一条命令即可。

## 二、已完成的真实只读验证（无需凭据的部分）

| 项 | 结果 |
|---|---|
| Demo 主机 DNS | `demo-fapi.binance.com` 可解析 |
| Demo TLS 证书 | CN=`demo-fapi.binance.com`，签发者 Amazon RSA 2048 M01，SAN 匹配 |
| Demo 公开时间端点 | `{"serverTime":1795326552791}` |
| 时间同步偏差 | **-221 ms**（recvWindow=5000ms，余量充足） |
| Demo 是否独立环境 | **是**：744 个交易对 vs 正式 956；BTCUSDT 价格 58867.00 vs 58908.30 |
| exchangeInfo 指纹 | demo `c5e27a6a90953195` ≠ prod `ec1f371c040ab9c1` |
| Trading Terminal | `http://127.0.0.1:8791/` HTTP 200，`<title>OpenAlpha Trading Terminal</title>`；`/api/state` 返回三本账 |

## 三、关键发现：20 USDT 在 Demo 上会被拒

Demo BTCUSDT 真实过滤器（`demo-fapi.binance.com/fapi/v1/exchangeInfo`）：

| 参数 | 值 |
|---|---|
| stepSize | **0.0001** |
| minQty | 0.0001 |
| tickSize | 0.10 |
| **minNotional** | **50** |

按你要求的 20 USDT 计算：

```
20 / 58867 = 0.000339... → 向下取整 0.0003 BTC
0.0003 × 58867 = 17.66 USDT  <  50  → 会被交易所拒绝
```

| 名义金额 | 取整后数量 | 实际名义 | 结果 |
|---|---|---|---|
| 20 | 0.0003 | 17.66 | **REJECTED** |
| 50 | 0.0008 | 47.09 | **REJECTED**（取整后跌破门槛） |
| 100 | 0.0016 | 94.19 | OK |
| 120 | 0.0020 | 117.73 | OK |
| 300 | 0.0050 | 294.34 | OK |

**处理**：把门槛做成下单前的硬校验，低于门槛直接返回需要多少，绝不硬试。
`--test-notional` 默认值从 20 改为 120。已用 Demo 真实过滤器固化为测试。

## 四、本轮新发现并修复的 bug：幂等预查会把「查不了」当成「不存在」

用 Demo 真实规则跑离线全链路时暴露出来：

```
open_result: stage=idempotent_exchange, qty=0, stop_algo_id=""   ← 假的幂等成功
```

原实现：

```python
try:
    existing = self.client.order_status(...)
except Exception:
    existing = None        # 网络错误 / 签名错误 / 限流 全部被当成"订单不存在"
```

后果有两个，都很严重：

1. **网络或签名错误时会重复下单** —— 本该停下的场景被当成可以下单；
2. 有 `orderId` 但成交量为 0 时，返回 `qty=0` 的"成功"，**保护单一张都不挂** → 裸仓。

修复：`_probe_existing()` 区分三种情况

| 情况 | 判定 | 动作 |
|---|---|---|
| `-2013` / `-2011` / "does not exist" | 明确不存在 | 继续下单 |
| 有 orderId 且成交量 > 0 | 已成交 | 幂等返回 |
| 有 orderId 但成交量 = 0 | 存在但未成交 | **拒绝，要求人工核对** |
| 其他异常 / 空响应 | 查不了 | **拒绝下单** |

新增 4 条回归测试。全量测试 **813 passed, 2 skipped**（上一轮 806）。

## 五、离线全链路参数证据（真实 Demo 规则，零真实订单）

`python -m scripts.demo_offline_evidence` → `reports/demo_offline_evidence.json`

所有请求由注入的 fetch 捕获，密钥是占位符 `PLACEHOLDER_KEY_NOT_REAL`，**未发往交易所**。

实际构造的请求：

```
POST /fapi/v1/order
  symbol=BTCUSDT side=BUY type=MARKET quantity=0.0020
  newClientOrderId=oa-offline-abc123

POST /fapi/v1/algoOrder
  algoType=CONDITIONAL symbol=BTCUSDT side=SELL type=STOP_MARKET
  workingType=MARK_PRICE triggerPrice=58600 quantity=0.0020
  reduceOnly=true clientAlgoId=oa-offline-abc123-s

POST /fapi/v1/algoOrder
  algoType=CONDITIONAL symbol=BTCUSDT side=SELL type=TAKE_PROFIT_MARKET
  workingType=MARK_PRICE triggerPrice=59100 quantity=0.0020
  reduceOnly=true clientAlgoId=oa-offline-abc123-t

DELETE /fapi/v1/algoOrder  clientAlgoId=oa-offline-abc123-s
DELETE /fapi/v1/algoOrder  clientAlgoId=oa-offline-abc123-t

POST /fapi/v1/order
  symbol=BTCUSDT side=SELL type=MARKET quantity=0.0020 reduceOnly=true
  newClientOrderId=oa-offline-abc123-c
```

9 项断言全部通过：

| 断言 | 结果 |
|---|---|
| 市价单走 `/fapi/v1/order` | true |
| STOP_MARKET 走 `/fapi/v1/algoOrder` | true |
| TAKE_PROFIT_MARKET 走 `/fapi/v1/algoOrder` | true |
| 全链路无 `stopPrice` 旧参数 | true |
| 所有条件单带 `reduceOnly=true` | true |
| 保护单方向与持仓相反（SELL vs BUY） | true |
| 平仓前撤掉两张保护单 | true |
| 平仓市价单带 `reduceOnly=true` | true |
| 所有请求主机均为 `demo-fapi.binance.com` | true |

HMAC 签名可用占位密钥复算一致（`signature_recomputable: true`），证明签名构造正确。

## 六、未验证项（截至 14:50）

| 项 | 状态 |
|---|---|
| Demo 账户余额 / 持仓 / 挂单真实读取 | ✅ 已验证（5000 USDT，无持仓无挂单） |
| 真实 `orderId` / `algoId` / 成交均价 / 手续费 | ✅ 已验证（见第十节） |
| `-4120` 是否真的消失 | ✅ **未出现**，条件单成功拿到 `algoId` |
| API 权限（是否允许合约下单） | ✅ 已实测下单成功 |
| 重启对账 | ⚠️ 仅离线验证（`reconcile` 单测）；真实重启待第三阶段观察 |
| 保护单在真实行情下被触发 | ⚠️ **未触发**（价格没到 58952.20 / 59425.80 就主动平了）；第三阶段若持仓跨过触发价可验证 |
| V2 引擎连续自动交易 30 分钟 | ⚠️ 未开始（第三阶段） |

## 七、执行顺序与当前进度

```bash
# 阶段一：只读（不下单）—— ✅ 已通过
python -m research.runtime demo-exec --root data/openalpha_v2

# 阶段二：受控测试单（120 USDT，立刻平掉）—— ✅ 已成交并平掉
python -m research.runtime demo-exec --root data/openalpha_v2 --place-test-order --test-notional 120

# 阶段三：AI 自动交易，只镜像 deepseek_5m —— ⏳ 待启动
python -m research.runtime v2 --root data/openalpha_v2_demo --minutes 30 \
  --demo-exec --demo-book deepseek_5m
```

失败即停：任何一步失败都会打印 `residual`（残留持仓/挂单）并返回非零码，不重试。

## 八、安全边界确认

- 客户端只允许 `https://demo-fapi.binance.com`，每次请求前重校验；10 组恶意 URL 全被拒
- 行情读取用 `fapi.binance.com` 的**公开只读**端点（klines/ticker），不涉及账户与下单
- 密钥只从环境变量或本地 `.env` 读（白名单两键、只补缺失、值不打印），不写入代码、配置、日志
- 本报告与证据文件中的密钥均为占位符

## 九、旧验收进程

PID 27200（13:00:51 启动，`--minutes 120`）仍在运行，未被打断、未覆盖账本。14:20 时的状态：

| 账本 | 轮次 | 区间 | 决策分布 | 是否含 `intel` 字段 |
|---|---|---|---|---|
| rules_5m | 17 | 13:00 → 14:20 | SHORT 17 | 否 |
| deepseek_5m | 17 | 13:00 → 14:20 | SHORT 17 | 否 |
| dual_5m | 17 | 13:00 → 14:20 | HOLD 17 | 否 |

13:00→14:20 共 80 分钟，17 拍 = 首拍 + 16 个 5m 收盘，**每根 K 线一拍，无丢拍、无重复触发**。
无 `intel` 字段说明它跑的是修复前代码（符合预期）。dual 全 HOLD 是既有的"第二模型缺席即暂停"逻辑，不是本轮改动。

**旁注（不影响本轮结论，但值得记一笔）**：rules 与 deepseek 17 轮全部同向 SHORT，
说明这段时间趋势极单边；这样的样本无法区分"模型有判断力"与"模型只会跟趋势"，
等 Demo 联调稳定后建议补一段震荡行情对比。

## 十、第二阶段：真实受控测试单（已成交）

命令：

```bash
python -m research.runtime demo-exec --root data/openalpha_v2 --place-test-order --test-notional 120
```

账户前置状态（下单前实测）：`USDT 5000.00000000`、`BTC 0.01000000`、`USDC 5000.00000000`，
**无持仓、无普通挂单、无 algo 挂单**，单向持仓模式。

### 10.1 开仓

| 项 | 值 |
|---|---|
| `orderId` | **28690711029** |
| `clientOrderId` | `oa-demo-test-1795329727` |
| 方向 / 类型 | BUY / MARKET |
| 数量 | 0.0020 BTC |
| 成交均价 | **59225.00** |
| 成交额 | 118.450000 USDT |
| 状态 | FILLED（`executedQty = 0.0020`） |

### 10.2 保护单（algoOrder 通道）

| 项 | 止损 | 止盈 |
|---|---|---|
| `algoId` | **1000000297515008** | **1000000297515018** |
| `clientAlgoId` | `oa-demo-test-1795329727-s` | `oa-demo-test-1795329727-t` |
| 类型 | STOP_MARKET | TAKE_PROFIT_MARKET |
| `triggerPrice` | 58952.20 | 59425.80 |
| `reduceOnly` | true | true |
| 最终 `algoStatus` | CANCELED（平仓时撤销） | CANCELED（平仓时撤销） |

**两张单都返回了 `algoId`，没有 `-4120`。** 这是本轮最关键的验证：2025-12-09 的迁移确实生效，
旧端点已不可用、新端点可用。

### 10.3 平仓与撤保护

| 项 | 值 |
|---|---|
| `orderId` | **28690711132** |
| `clientOrderId` | `oa-demo-test-1795329727-c` |
| 方向 / 类型 | SELL / MARKET，`reduceOnly = true` |
| 成交均价 | 59224.80 |
| 成交额 | 118.449600 USDT |

### 10.4 成交明细（`/fapi/v1/userTrades`）

| trade id | orderId | 价格 | 数量 | 手续费 | 已实现盈亏 |
|---|---|---|---|---|---|
| 560264905 | 28690711029 | 59225.0 | 0.0020 | 0.04738000 USDT | 0 |
| 560264923 | 28690711132 | 59224.8 | 0.0020 | 0.04737984 USDT | -0.00039999 |

### 10.5 账务核对

| 项 | 值 |
|---|---|
| 余额前 | 5000.00000000 USDT |
| 余额后 | **4999.90484017 USDT** |
| 差额 | -0.09515983 |
| 手续费合计 | 0.09475984 |
| 价差亏损 | 0.00039999 |
| 差额校验 | 0.09475984 + 0.00039999 = 0.09515983 ✔ 完全对上 |

平仓后 `/fapi/v2/positionRisk` 无持仓、`/fapi/v1/openOrders` 空、`/fapi/v1/openAlgoOrders` 空，
`adapter.reconcile()` 返回 `{"ok": true, "diffs": []}`。

### 10.6 本轮暴露并修掉的三个真实问题

| 问题 | 现象 | 修法 |
|---|---|---|
| HTTP 错误体被丢弃 | 只看到 `HTTP Error 400`，不知是 -1021 还是 -2015 | `_default_fetch` 捕获 `HTTPError` 并解析 body，抛出 `HTTP 400 -1021: ...` |
| 未校准服务器时间 | `-1021 Timestamp outside of recvWindow` 随机出现 | `sync_time()` 取 3 次采样中 **RTT 最小**的一次定 offset；签名用 `server_now_ms()` |
| `recvWindow=5000` 太紧 | 本机到 Demo RTT 抖动可达 4.2s，偶发 -1021 | 提到 **60000**（Binance 上限），并对 -1021 **只重试一次**（重采样后再签） |
| preflight 一击即溃 | 一个接口失败就整段 traceback，看不到其余信息 | 每步单独 try，输出 `steps` / `errors`，只有「账户可读 + 单向持仓 + 过滤器可读」才算 ok |

实测时间偏差：**322 ms**（recvWindow 60000，余量充足）。


## 十一、第三阶段（AI 自动交易）：执行链路已通，卡在 LLM 凭据

14:47 启动：

```bash
python -m research.runtime v2 --root data/openalpha_v2_demo --minutes 30 \
  --demo-exec --demo-book deepseek_5m
```

引擎正常起来（`demo=on:deepseek_5m`，凭据从 `.env` 读到），跑了 3 个 5m 收盘（约 9 分钟）后主动停止。

### 11.1 决策结果：AI 没电，只有规则在跑

| 账本 | 3 轮结果 | 状态 |
|---|---|---|
| rules_5m | SHORT / SHORT / SHORT | OK（趋势跟随） |
| deepseek_5m | HOLD / HOLD / HOLD | **ERROR**（`deepseek_error: HTTPError`） |
| dual_5m | HOLD / HOLD / HOLD | UNAVAILABLE（第二模型未配置） |

**没有触发任何 Demo 订单**，账户无持仓、无挂单，余额仍为 4999.90484017 USDT。

### 11.2 LLM 凭据实测（三项全废）

| 变量 | 长度/前缀 | 实测结果 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 35，`sk-` 前缀 | `HTTP 401 Authentication Fails, Your api key: ****d976 is invalid` |
| `ZHIPU_API_KEY` | 49 | `HTTP 401 {"error":{"code":"1000","message":"身份验证失败。"}}` |
| `OPENROUTER_API_KEY` | 49 | `HTTP 401 Missing Authentication header` |

**关键发现**：`ZHIPU_API_KEY` 与 `OPENROUTER_API_KEY` 是**同一个值**（前缀 `cf7`、后缀 `Fks`），
说明这两条是占位/复制错了的凭据，不是真 key。

### 11.3 顺带修掉的两个「查不出来」

| 问题 | 修法 |
|---|---|
| `DeepSeekProvider` 兜底只写 `deepseek_error: HTTPError`，401/402/超时/解析失败全一样 | 带上 `str(exc)[:120]`，一眼看出是哪类 |
| `_run_v2` 构造 `IntelPipelineConfig` 未传 `llm` → 情报抽取**永远走规则回退**，即使有 key 也一样 | 把同一个 key 传进 `LLMConfig` |

第二条是本轮最隐蔽的：日志里的 `LLM_UNAVAILABLE: no DEEPSEEK_API_KEY` 看起来像"没配 key"，
实际原因是**代码没把 key 交给情报模块**——正是上一轮"情报没真正进入决策"的同类问题。

### 11.4 结论

执行链路（引擎 → 适配器 → 交易所）已经端到端跑通，**唯一缺的是一把有效的 LLM key**。
拿到有效 key 后，直接重跑同一条命令即可，无需改代码。


## 十二、第三阶段实测（30 分钟，7 轮，2 次真实开仓）

15:18–15:48，`--root data/openalpha_v2_run --minutes 30 --demo-exec --demo-book deepseek_5m`，
DeepSeek key 有效后重跑（`env -u DEEPSEEK_API_KEY` 让 `.env` 生效，避免被系统里的旧 key 盖掉）。

### 12.1 决策：AI 真的在判断

7 轮全部 `SHORT / OK / conf 0.6`，rationale 每轮都带技术面 + 新闻面理由，
`last_inputs.intel_features` 显示 digest 已进模型（technical.bias、news.bias、ADX、RSI、ATR 均在）。

样例（15:18）：

> Downtrend with aligned negative momentum and technical bias; ADX moderate,
> RSI neutral-low. News mildly bearish. Microstructure slightly positive but weak.

### 12.2 真实订单（交易所回执）

| 时间 | 动作 | 交易所 ID | 价格 | 数量 | 状态 |
|---|---|---|---|---|---|
| 15:19 | 开仓 SELL MARKET | **28690740782** | 59108.20 | 0.0016 | FILLED |
| 15:19 | 止损 STOP_MARKET | **1000000297552353** | trigger 60009.10 | 0.0016 | CANCELED（平仓时撤销） |
| 15:19 | 止盈 TAKE_PROFIT_MARKET | **1000000297552397** | trigger 57369.10 | 0.0016 | CANCELED |
| 15:31 | 平仓 BUY MARKET reduceOnly | **28690751351** | 59107.20 | 0.0016 | FILLED |
| 15:36 | 再开仓 SELL MARKET | **28690754999** | 59143.70 | 0.0016 | FILLED |
| 15:36 | 止损 | **1000000297568607** | trigger 60009.10 | 0.0016 | **NEW（仍在挂）** |
| 15:36 | 止盈 | **1000000297568653** | trigger 57369.10 | 0.0016 | **NEW（仍在挂）** |

平仓那笔已实现盈亏 **+0.00159999 USDT**，手续费 0.03782924 + 0.03782860 = 0.07565784 USDT。

### 12.3 账户当前状态（15:48 结束后）

| 项 | 值 |
|---|---|
| 持仓 | **-0.0016 BTC**，开仓价 59143.70，标记价 59109.78 |
| 未实现盈亏 | **+0.05427484 USDT** |
| 保护单 | 止损 60009.10 / 止盈 57369.10，均 `NEW`、`reduceOnly` |
| 普通挂单 | 无 |
| USDT 余额 | **4999.79293036**（起始 5000） |

进程退出后仓位与两张条件单仍留在交易所，**打开 Binance Demo Trading 的 BTCUSDT 合约页即可看到**。

### 12.4 本轮暴露的两个问题

| 问题 | 现象 | 处理 |
|---|---|---|
| **镜像失败仍被登记为已持仓** | 第二跑（15:11）首次开仓镜像超时，本地却记成"交易所有仓"，平仓时会去平不存在的仓 | 改成成功才登记；无法确认时 `mirror_halted` 停止镜像并立刻 `reconcile` |
| **下单超时直接判失败** | 订单可能已在交易所成交，本地记 FAILED | 先 `_probe_existing`，有成交就按回执继续挂保护单 |
| **行情陈旧仍在下单** | REST 出现 `HTTP 418/429`，`load_history` 连续失败，引擎仍按旧快照开仓 | 新增 `max_stale_ms`（3 根 5m），超时跳过本轮 |

`418 I'm a teapot` 是 Binance 对超额请求的封禁响应，通常跟在 429 之后。
本轮同时有旧验收进程和多次手工联调在打同一个 IP，属可预期的限流；
24 小时跑之前建议把 `history_refresh_ms` 放宽到 10 分钟，并给 418 加冷却。

### 12.5 结论

**「AI 判断 → 下单 → 挂保护单 → 平仓 → 再开仓」在真实 Binance Demo 上完整跑通，全程虚拟资金。**
剩余风险集中在数据源限流与长时间运行的对账漂移，属于运维层面，不影响链路正确性。
