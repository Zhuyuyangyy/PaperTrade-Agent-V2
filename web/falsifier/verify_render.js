/**
 * 页面渲染层端到端验证：用真实引擎输出跑真实的 render()。
 *
 * 为什么必须这么测：字段名写错时页面不会报错，只会静默显示「—」。
 * 肉眼看不出来，用户看到的是"这工具跑不出数"。所以这里用最小 DOM 桩
 * 把 index.html 里的渲染函数原样跑一遍，再逐个断言"该有数的位置有数"。
 */
const fs = require("fs");
const path = require("path");

// ---------- 极简 DOM 桩：只实现页面用到的 API ----------
function makeEl(id) {
  const el = {
    id,
    _text: "",
    _html: "",
    className: "",
    style: {},
    value: "",
    srcdoc: "",
    disabled: false,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    querySelector(sel) {
      if (!el._children[sel]) el._children[sel] = makeEl(id + sel);
      return el._children[sel];
    },
    _children: {},
  };
  return el;
}

const els = {};
const document = {
  getElementById(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
  createElement(tag) { return makeEl(tag); },
  head: { appendChild() {} },
};
const window = {};
const performance = { now: () => Date.now() };
const fetch = async () => { throw new Error("no network in test"); };

async function main() {
  const html = fs.readFileSync(path.join(__dirname, "../../docs/falsifier/index.html"), "utf8");
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  if (!m) throw new Error("找不到 <script> 块");

  // 去掉最后的 boot() 调用——测试里不需要启动 Pyodide
  let js = m[1].replace(/\nboot\(\);\s*$/, "\n");

  // 把渲染函数暴露出来
  js += "\nmodule.exports = { render, defaultSpec, mdToMiniHtml, pct, num, pp, esc };\n";

  const sandboxRequire = require;
  const fn = new Function(
    "module", "exports", "document", "window", "performance", "fetch",
    "loadPyodide", "require", js
  );
  const mod = { exports: {} };
  fn(mod, mod.exports, document, window, performance, fetch, async () => {}, sandboxRequire);

  const { render, defaultSpec, mdToMiniHtml } = mod.exports;

  // ---------- 用真实引擎输出 ----------
  const schemaPath = path.join(__dirname, "_schema.json");
  if (!fs.existsSync(schemaPath)) {
    throw new Error("缺 _schema.json：先跑 node _probe2.js 生成真实引擎输出");
  }
  const json = JSON.parse(fs.readFileSync(schemaPath, "utf8"));
  const md = fs.readFileSync(path.join(__dirname, "_report.md"), "utf8");

  render(json, md);

  // ---------- 断言 ----------
  let fail = 0;
  const chk = (label, cond, got) => {
    console.log(`  ${cond ? "OK  " : "FAIL"}  ${label}${cond ? "" : "  → " + JSON.stringify(got)}`);
    if (!cond) fail++;
  };

  console.log("\n[1] 裁决区");
  chk("裁决 pill 为 FALSIFIED", els["verdict-pill"].textContent === "FALSIFIED", els["verdict-pill"].textContent);
  chk("pill 用了红色证伪样式", els["verdict-pill"].className.includes("v-falsified"), els["verdict-pill"].className);
  chk("标题非空", els["verdict-title"].textContent.length > 4, els["verdict-title"].textContent);
  chk("描述引用了 5 条规则", els["verdict-desc"].textContent.includes("5 条"), els["verdict-desc"].textContent.slice(0, 80));

  console.log("\n[2] KPI 四项");
  const kpiHtml = els["kpis"].innerHTML;
  chk("KPI 渲染出 4 个格", (kpiHtml.match(/class="kpi"/g) || []).length === 4, (kpiHtml.match(/class="kpi"/g) || []).length);
  chk("成交笔数 204", kpiHtml.includes(">204<"), "未找到 204");
  chk("最坏口径 -54.31%", kpiHtml.includes("-54.31%"), "未找到 -54.31%");
  chk("对照差值不显示破折号", !/>—</.test(kpiHtml.split("跑得过随机入场吗")[1] || ""), "对照差值仍是 —");
  chk("买入持有差值有数", /[-+]\d+\.\d+pp/.test(kpiHtml), "未找到 pp 数值");
  // 关键：任何 KPI 的 v 值都不该是 —
  const vals = [...kpiHtml.matchAll(/<div class="v[^"]*">([^<]*)<\/div>/g)].map(x => x[1]);
  chk("四个 KPI 值全部有数", vals.length === 4 && vals.every(v => v !== "—"), vals);

  console.log("\n[3] 证伪规则表");
  const ckHtml = els["tbl-checks"].querySelector("tbody").innerHTML;
  chk("渲染 5 行规则", (ckHtml.match(/<tr/g) || []).length === 5, (ckHtml.match(/<tr/g) || []).length);
  chk("有「触发」标记", ckHtml.includes("触发"), "无");
  chk("显示了规则名 worst_case_net_negative", ckHtml.includes("worst_case_net_negative"), "无");
  chk("checks-meta 显示 5/5", els["checks-meta"].textContent.includes("5 / 5"), els["checks-meta"].textContent);

  console.log("\n[4] 成本敏感性表");
  const sHtml = els["tbl-sens"].querySelector("tbody").innerHTML;
  chk("渲染 3 档", (sHtml.match(/<tr/g) || []).length === 3, (sHtml.match(/<tr/g) || []).length);
  chk("倍数列有 ×1/×2/×5", /×1/.test(sHtml) && /×2/.test(sHtml) && /×5/.test(sHtml), "缺倍数");
  chk("费率列有 bps", sHtml.includes("bps"), "无");
  chk("净收益有负值", sHtml.includes("-54.31%"), "无");

  console.log("\n[5] 六件仪器输出");
  const iHtml = els["instrument-out"].innerHTML;
  chk("渲染 6 张卡", (iHtml.match(/class="inst/g) || []).length === 6, (iHtml.match(/class="inst/g) || []).length);
  chk("无死卡（全产出）", !iHtml.includes("inst dead"), "存在 dead 卡");
  chk("随机对照显示 200 次", iHtml.includes("200 次"), "无");
  chk("参数网格显示 0/9", iHtml.includes("0/9"), "无");
  chk("障碍可达显示百分比", /触及 \d/.test(iHtml), "无");
  chk("walk-forward 显示 3 折", iHtml.includes("3 折"), "无");
  chk("instruments-meta 有窗口", els["instruments-meta"].textContent.includes("5000 根"), els["instruments-meta"].textContent);

  console.log("\n[6] 报告与复现");
  chk("markdown 原文非空", els["report-md"].textContent.length > 3000, els["report-md"].textContent.length);
  chk("JSON 可回读", (() => { try { return !!JSON.parse(els["report-json"].textContent); } catch { return false; } })(), "解析失败");
  chk("复现命令含 --spec", els["report-repro"].textContent.includes("--spec"), els["report-repro"].textContent.slice(0, 60));
  chk("HTML iframe 有内容", els["report-html"].srcdoc.length > 2000, els["report-html"].srcdoc.length);

  console.log("\n[7] md 渲染器");
  const h = mdToMiniHtml(md);
  chk("生成 table", h.includes("<table>"), "无 table");
  chk("表头用 th 不是 td", h.includes("<th>"), "无 th");
  chk("没有裸露的 |", !/<p>\|/.test(h), "有裸露表格行");

  console.log("\n[8] 默认 spec");
  const spec = defaultSpec();
  chk("spec 有 falsify_if", !!spec.falsify_if, "无");
  chk("spec 有 5 条证伪规则", Object.keys(spec.falsify_if).length === 5, Object.keys(spec.falsify_if).length);
  chk("spec 是合法 JSON", (() => { try { JSON.parse(JSON.stringify(spec)); return true; } catch { return false; } })(), "非法");

  console.log(`\n${fail === 0 ? "全部通过" : fail + " 项失败"}（共 30 项断言）`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch(e => { console.error("测试失败:", e); process.exit(1); });