/**
 * 用真实 Pyodide + 真实引擎生成一份 fixture（JSON + markdown），
 * 供 verify_render.js 做离线渲染断言。
 */
const fs = require("fs");
const path = require("path");
const { loadPyodide } = require("pyodide");

const SPEC = {
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
    grid_positive_ratio_min: 0.5,
  },
};

(async () => {
  const py = await loadPyodide();
  await py.loadPackage("sqlite3");
  py.runPython(fs.readFileSync(path.join(__dirname, "../../docs/falsifier/bridge.py"), "utf8"));
  py.globals.set("_E", fs.readFileSync(path.join(__dirname, "../../docs/falsifier/engine.py"), "utf8"));
  py.globals.set("_C", fs.readFileSync(path.join(__dirname, "../../docs/falsifier/demo_candles.csv"), "utf8"));
  py.runPython("install(_E, _C)");

  py.globals.set("_S", JSON.stringify(SPEC));
  const raw = py.runPython(`
import json as _j
_rc, _md, _js, _err = run_verify(_S, _C, "report")
_j.dumps([_rc, _md, _j.dumps(_js) if _js else "", _err or ""])
`);
  const [rc, md, jsText, err] = JSON.parse(raw);
  if (err) throw new Error(err);
  if (!jsText) throw new Error("无 JSON，rc=" + rc);

  fs.writeFileSync(path.join(__dirname, "_schema.json"), JSON.stringify(JSON.parse(jsText), null, 1), "utf8");
  fs.writeFileSync(path.join(__dirname, "_report.md"), md, "utf8");
  console.log(`fixture 已生成：_schema.json (${jsText.length} B) / _report.md (${md.length} B)`);
})();