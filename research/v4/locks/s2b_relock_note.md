
## S2b F/N 执行细节（2026-11-25 12:50，哈希不变）

S2b F/N 契约锁定后，发现两个数据路径指向旧 2 年缓存（S1/S2 诊断用），
而信号与 K 线已换成新 3 年 `data/v3_market/`：
- 新增 `btc_1h_3y.csv`（26298 根）与 `btc_funding_3y.csv`（3287 条）
- spec.funding_path 改为 `../diagnostic/btc_funding_3y.csv`

funding_path / candles 路径均不在 `contract_payload()` 保护范围，
契约哈希 F=`464759f2…` / N=`b4861d21…` 不变，`check_lock` 仍通过。
`falsify_if` 零变动。这是执行数据对齐修正，留痕不掩饰。
