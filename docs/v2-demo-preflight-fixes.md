# V2 引擎 + Binance Demo 联调前修复报告

日期：2026-11-22
基线提交：`a549698`（分支 `sprint-9/v2-engine`）
本轮新增提交：见文末

## 一、结论速览

| 复查项 | 状态 | 证据 |
|---|---|---|
| 1. 新闻情报进入 V2 决策 | **已修** | `on_5m_close` 现调用 `intel_fn`；`last_inputs.intel_features` 记录模型实际收到的特征 |
| 2. DeepSeek 重复调用 | **已修** | 同一快照 `memo` 缓存；测试断言 `model_calls == ["deepseek_5m"]` |
| 3. 历史刷新成功条件 | **已修** | `load_history()` 返回 bool，失败不推进 `_history_ts`；测试校验真实 K 线随时钟前进 |
| 4. 条件单接口 | **已修** | 条件单一律走 `/fapi/v1/algoOrder`；`STOP_MARKET` 不再出现于 `/fapi/v1/order` |
| 5. Demo 域名严格校验 | **已修** | 10 组恶意 URL 全被拒；每次请求前重新校验 |
| 6. 已收盘 K 线 | **已修（原实现更差）** | `as_of` 与 `bar_open_ms` 分离；`_views` 只给 `close_ms <= as_of - settle` |
| 7. Demo Execution Adapter | **已建（离线验证通过）** | 开仓/保护单/平仓/对账/幂等/兜底全部离线测试覆盖 |
| Binance Demo 真实下单 | **未验证** | 没有 Demo 凭据，一个真实订单都没发过 |

全量测试：**806 passed, 2 skipped**（基线为 800 passed）。

## 一之补充：本轮自己引入又修掉的 bug

改完 `run()` 后，`_last_bar` 一度被写成 `as_of`，而 `run()` 拿它和 `bar_open` 比。
`as_of`（now）恒大于 `bar_open`，所以**第二根 K 线之后就再也不会触发**——引擎静默停摆。

这正是第 3 条同类问题：**"看起来在跑"和"真的在推进"是两件事**。
原来的 9 小时测试直接调 `on_5m_close`，绕过了 `run()` 的比较逻辑，恰好把这个 bug 藏住。

修复：

- `_last_bar` 只存**触发 K 线的 open_ms**，注释写明不许存 as_of；
- 抽 `step_once(now_ms)` 作为唯一轮询入口，测试可以推模拟时钟走真实路径；
- 新增两条回归：
  - `test_last_bar_is_the_trigger_bar_not_as_of`
  - `test_step_once_fires_once_per_closed_bar`：同一根只触发一次、下一根必须能继续触发

## 二、第 6 条：比报告里说的更严重

原实现：

```python
bar = self.feed.latest("5m")        # 含正在形成的那根
if bar["time"] * 1000 > self._last_bar + config.decision_ms - 1000:
    self.on_5m_close(bar["time"] * 1000)     # 把 bar 的 open 当决策时刻
```

`analyze()` 默认 `settle_ms=30s`，只保留 `close_ms <= as_of - 30s` 的 K 线。
把「未收盘那根的 open」当 as_of，等于：**刚收盘的那根被排除，决策实际用的是再往前一根**。
所以不是"可能提前开仓"，而是**稳定延迟一根 5m**——每轮都在用 5 分钟前的信息。

修复后：

- `run()` 用 `latest_closed_5m(now)` 找到最新已收盘 K 线，只在它的 `open_ms` 前进时才决策；
- `on_5m_close(now, bar_open_ms=bar_open)`：`as_of` 是当前时刻，`bar_open_ms` 只用于日志；
- 决策日志新增 `trigger_bar_ms`、`closed_bar_ms`，可直接核对每次决策用的是哪根 K 线。

## 三、第 4 条：条件单迁移

2025-12-09 起 U 本位合约条件单迁移到 Algo Order API，旧接口返回 `-4120 STOP_ORDER_SWITCH_ALGO`。

| 订单类型 | 端点 | 关键参数 |
|---|---|---|
| MARKET / LIMIT | `POST /fapi/v1/order` | `quantity`、`newClientOrderId` |
| STOP / STOP_MARKET / TAKE_PROFIT / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET | `POST /fapi/v1/algoOrder` | `algoType=CONDITIONAL`、`triggerPrice`、`clientAlgoId` |
| 查条件单 | `GET /fapi/v1/algoOrder` | `algoId` 或 `clientAlgoId` |
| 撤条件单 | `DELETE /fapi/v1/algoOrder` | `algoId` 或 `clientAlgoId` |
| 撤全部条件单 | `DELETE /fapi/v1/algoOpenOrders` | — |
| 查挂单 | `GET /fapi/v1/openAlgoOrders` | — |

一个容易漏的细节：撤销条件单成功时返回 `{"code":"200","msg":"success"}`。
旧 `_request` 只要 `payload["code"]` 为真就抛异常，**成功会被当成失败**。现在白名单 `{None, 0, "0", 200, "200"}`。

## 四、第 7 条：Execution Adapter

`research/execution/adapter.py`，是引擎与交易所之间唯一的写路径。

- **数量合法化**：读 `exchangeInfo` 的 `stepSize` / `tickSize` / `minQty` / `minNotional`，一律向下取整；
  止损价按方向取保守侧（LONG 向下、SHORT 向上），避免比意图更近而提前触发。
- **幂等**：`clientOrderId = "oa-" + intent_id`，本地 `demo_orders` 表 + 交易所 `order_status` 双查。
- **裸仓兜底**：止损挂不上立刻市价平掉；平不掉就抛 `ExecutionRefused` 让引擎停，不允许静默留裸仓。
- **对账**：`reconcile()` 输出 `unprotected_position` / `missing_position` / `position_ok` 三类差异。
- **单向持仓**：`preflight()` 检测到双向持仓直接拒绝（引擎按单向建模，双向会算错方向）。

引擎侧默认 `executor=None, demo_book=None`：不显式开 `--demo-exec` 时，**一个交易所订单都不会发**。

## 五、当前能做什么 / 不能做什么

**已验证（离线，零网络）**
- 引擎三账本、5m 决策、tick 止损、早退
- 情报 digest 确实进入 snapshot 并留下特征快照
- DeepSeek 每快照一次，Dual 复用
- 历史刷新失败不推进时间戳；9 小时模拟时钟经 `step_once` 走真实入口，每轮 K 线递增且间隔恰为 5 分钟
- 同一根收盘 K 线只决策一次；第二根起必须继续触发（防静默停摆）
- 条件单走 algo 通道、带 `reduceOnly`、方向与持仓相反
- 保护单失败触发紧急平仓；平不掉会抛错阻断
- 无凭据时零下单

**已验证（真实网络，只读）**
- `load_history()` 从 `fapi.binance.com/fapi/v1/klines` 拉到 5m/15m/1h/4h 各 499 根，全部已收盘
- `_views()` 的 settle 裁剪正确：5m 末根 `close_ms` ≤ `as_of - 30s`
- 本例实测：as_of=1795325529607，5m 末根 close_ms=1795325400000（差 129.6s，已收盘）

**未验证（需要 Demo 凭据）**
- 币安真实返回的订单回执、`algoId`、成交均价
- `MARKET` 单的滑点与 `MIN_NOTIONAL` 的真实口径
- 保护单在真实行情下的触发时点
- 对账在真实重启后的行为
- `-4120` 是否真的不再出现（目前只是按文档改对了端点，没有真实请求过 algoOrder）

## 六、拿到凭据后的执行顺序

```bash
# 1. 只读体检（不下单）
python -m research.runtime demo-exec --root data/openalpha_demo

# 2. 一笔受控小额测试单（默认立刻平掉，默认 20 USDT）
python -m research.runtime demo-exec --root data/openalpha_demo \
  --place-test-order --test-notional 20

# 3. 只镜像 DeepSeek 账本，本地虚拟盘继续跑
python -m research.runtime v2 --root data/openalpha_v2_demo --minutes 30 \
  --demo-exec --demo-book deepseek_5m
```

凭据只从环境变量读：`BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET`。
不要写进任何配置文件、不要贴进任何聊天窗口。

## 七、范围与限制

- 本轮**没有**触碰正在跑的两小时验收（PID 27200，root=`data/openalpha_v2_accept`），也没有改动其账本。
- 本轮**没有**合并 PR #26，仍在 `sprint-9/v2-engine` 分支。
- 引擎目前仍按「本地虚拟盘为准、交易所为镜像」运行：交易所下单失败只记 `errors`，不回滚本地成交。
  这是有意的——回滚会让本地账本与已发生的成交脱节。真正上线前需要补「交易所失败 → 本地标记未对齐」的闭环。