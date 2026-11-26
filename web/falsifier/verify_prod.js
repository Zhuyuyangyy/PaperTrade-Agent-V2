/**
 * 生产路径验证：直接打 GitHub Pages 上的真实地址，走真实 CDN。
 *
 * 前面的 E2E 为了不依赖外网，把 Pyodide 换成了本地副本（window.PYODIDE_BASE）。
 * 但线上用户走的是 CDN 那条路——如果 CDN 地址写错、CORS 不对、或者版本号
 * 404，本地测试全绿而线上全白。所以这条必须单独验，且必须对真实域名跑。
 */
const { chromium } = require("playwright");

const URL = process.env.PROD_URL;

(async () => {
  if (!URL) { console.error("需要 PROD_URL 环境变量"); process.exit(2); }

  const browser = await chromium.launch();
  const page = await browser.newPage();

  const errors = [];
  const failed = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push("pageerror: " + e.message));
  page.on("requestfailed", r => failed.push(r.url() + " → " + (r.failure() || {}).errorText));

  let fail = 0;
  const chk = (label, cond, got) => {
    console.log(`  ${cond ? "OK  " : "FAIL"}  ${label}${cond ? "" : "  → " + JSON.stringify(got)}`);
    if (!cond) fail++;
  };

  console.log(`\n目标：${URL}`);
  await page.goto(URL, { waitUntil: "domcontentloaded" });
  chk("页面标题正确", (await page.title()).includes("策略证伪台"), await page.title());

  console.log("\n[1] 等引擎就绪（真实 CDN 取 Pyodide）");
  await page.waitForFunction(
    () => document.getElementById("status-text").textContent.includes("就绪"),
    { timeout: 300000 }
  ).catch(async () => {
    const st = await page.textContent("#status-text");
    const err = await page.textContent("#error-text").catch(() => "");
    throw new Error(`引擎未就绪（${st}）\n错误：${err}\n失败请求：\n${failed.slice(0, 8).join("\n")}`);
  });
  const meta = await page.textContent("#engine-meta");
  console.log(`  ${meta}`);
  chk("引擎已载入", meta.includes("engine ready"), meta);
  chk("sqlite3 真可用", meta.includes("真可用"), meta);

  console.log("\n[2] 真实运行一次");
  const t0 = Date.now();
  await page.click("#btn-demo");
  await page.waitForFunction(
    () => !document.getElementById("results").classList.contains("hidden"),
    { timeout: 180000 }
  );
  console.log(`  耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);

  const pill = (await page.textContent("#verdict-pill")).trim();
  chk("裁决 FALSIFIED", pill === "FALSIFIED", pill);

  const kpis = await page.$$eval("#kpis .kpi .v", els => els.map(e => e.textContent.trim()));
  chk("成交 204 笔", kpis.includes("204"), kpis);
  chk("净收益 -54.31%", kpis.includes("-54.31%"), kpis);
  chk("四个 KPI 都非空", kpis.length === 4 && kpis.every(v => v !== "—"), kpis);

  console.log("\n[3] 六个区块渲染");
  chk("5 行证伪规则", (await page.$$("#tbl-checks tbody tr")).length === 5, (await page.$$("#tbl-checks tbody tr")).length);
  chk("3 档成本", (await page.$$("#tbl-sens tbody tr")).length === 3, (await page.$$("#tbl-sens tbody tr")).length);
  chk("6 张仪器卡", (await page.$$("#instrument-out .inst")).length === 6, (await page.$$("#instrument-out .inst")).length);

  console.log("\n[4] 无报错");
  chk("无 JS 错误", errors.length === 0, errors.slice(0, 3));
  chk("无失败请求", failed.length === 0, failed.slice(0, 5));

  await page.screenshot({ path: "prod_screenshot.png", fullPage: false });
  await browser.close();

  console.log(`\n${fail === 0 ? "生产路径验证通过" : fail + " 项失败"}`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error("生产验证失败:", e.message); process.exit(1); });