/**
 * 浏览器端到端验证：真开一个 Chromium，真点「跑内置演示」，真断言页面上的数字。
 *
 * 前面 verify_render.js 用 DOM 桩测的是渲染逻辑；这里测的是整条链路——
 * Pyodide 从 CDN 下载、sqlite3 扩展装载、引擎在 WASM 里执行、结果回到页面。
 * 任何一环断了，这里都会红。
 *
 * 这是唯一能证明"它真的能在用户浏览器里跑"的证据。
 */
const { chromium } = require("playwright");

const URL = process.env.FALSIFIER_URL || "http://127.0.0.1:8899/";

// 与仓库引擎原生输出逐位一致（多一处断言就多一道保险）
const EXPECT = {
  verdict: "FALSIFIED",
  nSignals: "204",
  netPct: "-54.31%",
};

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();

  const consoleErrors = [];
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push("pageerror: " + e.message));

  let fail = 0;
  const chk = (label, cond, got) => {
    console.log(`  ${cond ? "OK  " : "FAIL"}  ${label}${cond ? "" : "  → " + JSON.stringify(got)}`);
    if (!cond) fail++;
  };

  console.log("\n[1] 打开页面");
  await page.goto(URL, { waitUntil: "domcontentloaded" });
  chk("标题正确", (await page.title()).includes("策略证伪台"), await page.title());

  console.log("\n[2] 等引擎就绪（Pyodide + sqlite3 + 引擎载入）");
  await page.waitForFunction(
    () => document.getElementById("status-text").textContent.includes("就绪"),
    { timeout: 180000 }
  ).catch(async () => {
    const st = await page.textContent("#status-text");
    const err = await page.textContent("#error-text").catch(() => "");
    throw new Error(`引擎未就绪，状态="${st}"\n错误：${err}`);
  });
  const meta = await page.textContent("#engine-meta");
  console.log(`  引擎元信息：${meta}`);
  chk("状态显示就绪", true);
  chk("sqlite3 真可用", meta.includes("真可用"), meta);
  chk("引擎行数 4724", meta.includes("4724"), meta);

  console.log("\n[3] 点「跑内置演示」");
  const t0 = Date.now();
  await page.click("#btn-demo");
  await page.waitForFunction(
    () => {
      const r = document.getElementById("results");
      return r && !r.classList.contains("hidden");
    },
    { timeout: 180000 }
  );
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`  运行耗时：${elapsed}s`);

  console.log("\n[4] 裁决与 KPI");
  const pill = (await page.textContent("#verdict-pill")).trim();
  chk(`裁决为 ${EXPECT.verdict}`, pill === EXPECT.verdict, pill);
  chk("pill 是红色证伪样式", (await page.getAttribute("#verdict-pill", "class")).includes("v-falsified"),
    await page.getAttribute("#verdict-pill", "class"));

  const kpis = await page.$$eval("#kpis .kpi", els => els.map(e => ({
    k: e.querySelector(".k").textContent.trim(),
    v: e.querySelector(".v").textContent.trim(),
    n: e.querySelector(".n").textContent.trim(),
  })));
  chk("4 张 KPI 卡", kpis.length === 4, kpis.length);
  for (const k of kpis) console.log(`    ${k.k}: ${k.v}   (${k.n})`);

  const byKey = Object.fromEntries(kpis.map(x => [x.k, x.v]));
  chk(`成交笔数 = ${EXPECT.nSignals}`, byKey["成交笔数"] === EXPECT.nSignals, byKey["成交笔数"]);
  chk(`最坏口径 = ${EXPECT.netPct}`, byKey["最坏口径净收益"] === EXPECT.netPct, byKey["最坏口径净收益"]);
  chk("四个 KPI 都不是 —", kpis.every(x => x.v !== "—"), kpis.map(x => x.v));

  console.log("\n[5] 证伪规则表");
  const rows = await page.$$eval("#tbl-checks tbody tr", els => els.map(e => ({
    rule: e.children[0].textContent.trim(),
    hit: e.children[3].textContent.trim(),
  })));
  chk("5 条规则", rows.length === 5, rows.length);
  chk("5 条全部触发", rows.filter(r => r.hit === "触发").length === 5, rows.map(r => r.hit));
  chk("含 worst_case_net_negative", rows.some(r => r.rule.includes("worst_case_net_negative")), rows.map(r => r.rule));

  console.log("\n[6] 成本敏感性");
  const sens = await page.$$eval("#tbl-sens tbody tr", els => els.map(e =>
    [...e.children].map(c => c.textContent.trim())));
  chk("3 档", sens.length === 3, sens.length);
  chk("×1 档净收益 -54.31%", sens[0] && sens[0][2] === "-54.31%", sens[0] && sens[0][2]);
  chk("三档全部为负", sens.every(r => r[5].includes("为负")), sens.map(r => r[5]));

  console.log("\n[7] 六件仪器");
  const inst = await page.$$eval("#instrument-out .inst", els => els.map(e => ({
    t: e.querySelector(".t").textContent.trim(),
    m: e.querySelector(".m").textContent.trim(),
    dead: e.classList.contains("dead"),
  })));
  chk("6 张卡", inst.length === 6, inst.length);
  chk("无死卡", inst.every(i => !i.dead), inst.filter(i => i.dead).map(i => i.t));
  for (const i of inst) console.log(`    ${i.t}: ${i.m}`);

  console.log("\n[8] 报告三件套");
  const md = await page.textContent("#report-md");
  chk("markdown 非空", md.length > 3000, md.length);
  chk("markdown 含 FALSIFIED", md.includes("FALSIFIED"), md.slice(0, 200));
  const repro = await page.textContent("#report-repro");
  chk("复现命令含 --candles", repro.includes("--candles"), repro.slice(0, 100));

  console.log("\n[9] 控制台无报错");
  chk("无 JS 错误", consoleErrors.length === 0, consoleErrors.slice(0, 3));

  await page.screenshot({ path: "e2e_screenshot.png", fullPage: true });
  console.log("\n  截图已保存 e2e_screenshot.png");

  await browser.close();

  console.log(`\n${fail === 0 ? "全部通过" : fail + " 项失败"}`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error("E2E 失败:", e.message); process.exit(1); });