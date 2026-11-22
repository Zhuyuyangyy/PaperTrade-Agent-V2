# Binance Demo 联调报告（第一阶段：只读验证）

日期：2026-11-22 13:48–14:05
代码：`c1e86c0` + 本轮新增修复
仓库：`D:/ZYY_Inovation/money/_openalpha/PaperTrade-Agent-V2`，分支 `sprint-9/v2-engine`

## 结论：第一阶段被凭据缺失阻塞，第二/三阶段未启动

**没有向交易所发出任何订单。没有使用真实资金。没有连接正式交易端点。**

## 一、凭据检查（不输出密钥内容）

| 位置 | `BINANCE_DEMO_API_KEY` | `BINANCE_DEMO_API_SECRET` |
|---|---|---|
| 当前进程环境 | MISSING | MISSING |
| Windows User 作用域 | MISSING | MISSING |
| Windows Machine 作用域 | MISSING | MISSING |
| 仓库内 `.env` / config | 不存在 | 不存在 |

`python -m research.runtime demo-exec --root data/openalpha_demo` 实际输出：

```json
{
  "ok": false,
  "reason": "DEMO_UNAVAILABLE: 未设置 BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET",
  "note": "没有凭据时只允许离线验证；本地虚拟盘不用凭据也能继续跑。"
}
```

程序按设计拒绝执行，**没有伪造任何回执**。

### 可能原因

Windows 环境变量在**设置之后新启动的进程**里才可见。WorkBuddy 及其子 shell 是在你设置之前启动的，
所以读不到。需要你确认设置方式（`setx` 永久 / 某个终端里 `set` 临时 / 系统属性面板），
并让程序在一个能看到该变量的新进程里运行。

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

## 六、未验证项（必须有凭据才能做）

- 真实 `orderId` / `algoId` / 成交均价 / 手续费
- `-4120` 是否真的不再出现（端点已按官方文档改对，但没真实请求过）
- Demo 账户余额、持仓、挂单的真实读取
- API 权限范围（是否只读 / 是否允许合约交易）
- 保护单在真实行情下的触发
- 重启对账的真实行为

## 七、凭据就绪后的执行顺序

```bash
# 阶段一：只读（不下单）
python -m research.runtime demo-exec --root data/openalpha_demo

# 阶段二：受控测试单（默认 120 USDT，立刻平掉）
python -m research.runtime demo-exec --root data/openalpha_demo --place-test-order --test-notional 120

# 阶段三：AI 自动交易，只镜像 deepseek_5m
python -m research.runtime v2 --root data/openalpha_v2_demo --minutes 30 \
  --demo-exec --demo-book deepseek_5m
```

失败即停：任何一步失败都会打印 `residual`（残留持仓/挂单）并返回非零码，不重试。

## 八、安全边界确认

- 客户端只允许 `https://demo-fapi.binance.com`，每次请求前重校验；10 组恶意 URL 全被拒
- 行情读取用 `fapi.binance.com` 的**公开只读**端点（klines/ticker），不涉及账户与下单
- 密钥只从环境变量读，不写入代码、配置、日志
- 本报告与证据文件中的密钥均为占位符

## 九、旧验收进程

PID 27200（13:00 启动，`--minutes 120`）在本报告撰写时仍在运行，13:48 时 7 轮、14:00 时 13 轮决策。
它跑的是**修复前**的代码（日志无 `intel` / `closed_bar_ms` 字段），全程未被干扰。