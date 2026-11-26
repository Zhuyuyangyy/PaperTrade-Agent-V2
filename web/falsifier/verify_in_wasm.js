/**
 * 在真实 Pyodide（WebAssembly）里跑引擎，验证浏览器方案成立。
 *
 * 这不是模拟：用的是与页面完全相同的 pyodide 版本、相同的 engine.py 与 bridge.py。
 * 只要这个脚本通过，页面里"浏览器内运行"这句话就是真的。
 */
const fs = require("fs");
const path = require("path");

async function main() {
  const t0 = Date.now();
  const { loadPyodide } = require("pyodide");
  const py = await loadPyodide();
  const tBoot = Date.now() - t0;
  console.log(`[1] Pyodide 启动: ${tBoot} ms`);

  // Pyodide 把 sqlite3 从标准库剥离了（unvendored）。引擎顶层就 import 它，
  // 所以必须显式装载——这不是可选项，不装连 import 都过不去。
  const tPkg = Date.now();
  await py.loadPackage("sqlite3");
  console.log(`[1b] sqlite3 扩展装载: ${Date.now() - tPkg} ms`);

  const engineSrc = fs.readFileSync(path.join(__dirname, "engine.py"), "utf8");
  const bridgeSrc = fs.readFileSync(path.join(__dirname, "bridge.py"), "utf8");
  const csv = fs.readFileSync(path.join(__dirname, "demo_candles.csv"), "utf8");

  py.runPython(bridgeSrc);

  // 引擎用 _HERE.parents[1] 推断仓库根，/tmp 上会越界；install() 把它放进
  // 足够深的虚拟目录，引擎代码一行不改。
  py.globals.set("_ENGINE_SRC", engineSrc);
  py.globals.set("_DEMO_CSV", csv);
  const info = JSON.parse(py.runPython("import json; json.dumps(install(_ENGINE_SRC, _DEMO_CSV))"));
  console.log("[2] 引擎与桥接层已载入");
  console.log(`[3] 环境: sqlite3=${info.sqlite3 ? "真可用" : "不可用"} has_main=${info.has_main} py=${info.python} 引擎 ${info.lines} 行`);

  // 与页面等价的 spec
  const spec = {
    name: "rsi-dip-btc-1h-browser",
    hypothesis: "BTC 1 小时 RSI(14) 低于 30 后做多，捕捉均值回复",
    source: "浏览器内置演示",
    symbol: "BTCUSDT", interval: "1h", venue: "spot",
    fee_rate: 0.0004, fill_model: "taker", funding: false,
    exit: { take_profit: 0.03, stop_loss: -0.08, timeout_bars: 24 },
    signals: { kind: "rule", rule: "rsi_below", period: 14, threshold: 30, side: "long" },
    control: { enabled: true, n_random: 200, seed: 42 },
    walk_forward: { enabled: true, folds: 3 },
    param_grid: { axes: { take_profit: [0.02, 0.03, 0.04], stop_loss: [-0.06, -0.08, -0.10] } },
    fee_multiples: [1, 2, 5],
    falsify_if: {
      control_lift_ci_includes_zero: true,
      net_negative_at_fee_multiple: 2,
      worst_case_net_negative: true,
      min_fills: 30,
      grid_positive_ratio_min: 0.5
    }
  };

  const t1 = Date.now();
  const tripleRaw = py.runPython(`
import json as _j
_rc, _md, _js, _err = run_verify(${JSON.stringify(JSON.stringify(spec))}, ${JSON.stringify(csv)}, "report")
_j.dumps([_rc, _md, _j.dumps(_js) if _js else "", _err or ""])
`);
  const tRun = Date.now() - t1;

  const [rc, md, jsText, errText] = JSON.parse(tripleRaw);
  console.log(`[4] 引擎运行: ${tRun} ms  (退出码 ${rc})`);

  if (errText) {
    console.error("引擎异常:\n" + errText);
    process.exit(1);
  }
  if (!jsText) {
    console.error("未返回 JSON。markdown 前 500 字:\n" + (md || "").slice(0, 500));
    process.exit(1);
  }

  const r = JSON.parse(jsText);
  const worst = (r.runs && r.runs.worst_case) || {};
  const wstats = worst.stats || {};
  const outcome = (r.verdict && r.verdict.outcome) || "?";

  console.log(`[5] 裁决: ${outcome}`);
  console.log(`    成交笔数: ${r.n_signals}`);
  console.log(`    最坏口径净收益: ${wstats.sum_pct != null ? wstats.sum_pct.toFixed(2) + "%" : "—"}`);
  console.log(`    成本敏感性档数: ${(r.cost_sensitivity || []).length}`);
  console.log(`    证伪规则触发: ${(r.verdict.checks || []).filter(c => c.triggered).length}/${(r.verdict.checks || []).length}`);
  console.log(`    markdown 长度: ${md.length} 字符`);

  // 与仓库引擎原生输出逐位对照（这是"没跑偏"的唯一证据）。
  // 注意 sum_pct 单位是百分数，不是小数率。
  const expected = { n_signals: 204, sum_pct: -54.3144, outcome: "FALSIFIED" };
  const okSignals = r.n_signals === expected.n_signals;
  const okNet = wstats.sum_pct != null && Math.abs(wstats.sum_pct - expected.sum_pct) < 1e-6;
  const okVerdict = outcome === expected.outcome;
  console.log(`[6] 与仓库引擎一致性: 笔数=${okSignals ? "一致" : "不一致 " + r.n_signals}`
    + ` 净收益=${okNet ? "一致" : "不一致 " + wstats.sum_pct}`
    + ` 裁决=${okVerdict ? "一致" : "不一致 " + outcome}`);

  if (!okSignals || !okNet || !okVerdict) {
    console.error("一致性校验失败");
    process.exit(1);
  }

  // 报告结构完整性
  const need = ["spec", "window", "data_hygiene", "n_signals", "runs", "cost_sensitivity",
                "barrier_reach", "control", "buy_and_hold", "verdict", "walk_forward",
                "param_grid", "diagnosis", "repro"];
  const missing = need.filter(k => !(k in r));
  if (missing.length) {
    console.error("报告缺少字段: " + missing.join(", "));
    process.exit(1);
  }
  console.log(`[7] 报告字段完整: ${need.length}/${need.length}`);

  // 六件仪器都必须真的产出东西，而不是空壳
  const instruments = {
    "随机对照组": (r.control && r.control.n_random === 200 && r.control.stats) ? "200 次" : "缺",
    "参数网格": (r.walk_forward && r.walk_forward.in_sample_grid) ? `${r.walk_forward.in_sample_grid.positive_cells}/${r.walk_forward.in_sample_grid.n_cells} 格为正` : "缺",
    "样本外分段": (r.walk_forward && r.walk_forward.out_sample) ? `${r.walk_forward.folds} 折` : "缺",
    "障碍可达性": (r.barrier_reach && r.barrier_reach.n) ? `TP 触及 ${r.barrier_reach.reached_tp_pct.toFixed(1)}%` : "缺",
    "买入持有对照": (r.buy_and_hold && r.buy_and_hold.matched_bh_pct != null) ? `跑输 ${(r.buy_and_hold.diff_pct).toFixed(2)}pp` : "缺",
    "成本敏感性": (r.cost_sensitivity || []).length ? `${r.cost_sensitivity.length} 档` : "缺",
  };
  const dead = Object.entries(instruments).filter(([, v]) => v === "缺");
  for (const [k, v] of Object.entries(instruments)) console.log(`    ${k}: ${v}`);
  if (dead.length) {
    console.error("仪器未产出: " + dead.map(([k]) => k).join(", "));
    process.exit(1);
  }
  console.log(`[8] 六件仪器全部产出: 6/6`);

  console.log(`\n总计: ${((Date.now() - t0) / 1000).toFixed(1)}s（启动 ${tBoot}ms + 运行 ${tRun}ms）`);
  console.log("结论: 浏览器内运行方案成立。");
}

main().catch(e => {
  console.error("验证失败:", e);
  process.exit(1);
});