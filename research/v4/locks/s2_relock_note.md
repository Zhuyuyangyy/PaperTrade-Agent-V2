# S2 契约重锁留痕（2026-11-25）

**第一次锁**：sha256 `58ba0d4a5795fc4e1e9d37ca895babf3182fb3cd47ec1d9470ecd5bbae8b8f43`，
时间 2026-11-25T02:49:48Z。

**作废原因**：引擎 `Spec.validate()` 报 `[输入错误] signals.side 必须是 'long' 或 'short'`。
设计文档 `S2_rules.md` 写了 `side: both`，是格式错误——引擎的 CSV 模式方向由每行 `side` 列决定
（`data.load_signals_csv` 逐行读 side），spec 的 `signals.side` 只在 rule/random 模式生效，
但 Spec 构造时仍强制校验取值。同时 `signals.path` 相对 spec 目录解析会拼成
`research/v4/specs/research/v4/entries/...`，路径错误。

**修正内容（仅格式，零阈值变动）**：
- `signals.side`: `both` → `long`（占位以满足校验；真实方向仍由 CSV 每行的 LONG/SHORT 决定）
- `signals.path`: `research/v4/entries/s2_btc_4h_entries.csv` → `../entries/s2_btc_4h_entries.csv`

**未改动**：exit（TP/SL/timeout/trailing）、fee_rate、fill_model、funding、control、walk_forward、
param_grid、fee_multiples、falsify_if 全部五条、signal_definition 全部阈值。

**为什么重锁而不静默改**：契约锁按内容哈希，任何 spec 变动都必须留痕并重锁，否则无法区分
"格式修正"和"看完结果偷改阈值"。这是契约锁存在的意义。

**关于哈希未变（重要，避免误读）**：重锁后 sha256 仍是 `58ba0d4a…`，与第一次相同——
这不是锁失效，而是 `contract_lock.contract_payload()` 只保护
`name/symbol/interval/venue/falsify_if`（设计注释："the judge, not the defendant"）。
side/path 属执行细节，本就不在契约保护范围，所以格式修正不改变哈希。
两次锁的区别仅在 `locked_at` 时间戳（02:49:48Z → 02:50:11Z）。
**真正需要防的是 falsify_if 变动——那会改变哈希并被 check_lock 检出。**

**重锁后**：见同目录 `s2_squeeze_breakout.lock.json`（locked_at 02:50:11Z）。

## 第二次执行细节修正（同日，funding_path）

正式首跑 `funding_applied=False`——CLI 用 `--candles` 自定义 K 线时不自动加载资金费，
S1 已踩过这个坑，S2 又漏了。补 `funding_path='diagnostic/btc_funding.csv'` 后重跑。
`funding_path` 是执行细节，不在 `contract_payload()` 保护范围，契约哈希仍 `58ba0d4a…`。
`falsify_if` / exit / 阈值零变动。这是执行失误，如实留痕，不掩饰。
