/**
 * 离线验证：页面加载完引擎后，真的断网还能不能再跑一次。
 *
 * 页面上写着"首次打开需联网，此后每次运行都不再发请求"。这句话必须能被
 * 验证，否则又是一句漂亮话。做法：等引擎就绪 → 用 route 拦掉所有外部请求
 * → 再点一次运行 → 仍然要出正确数字。
 */
const { chromium } = require("playwright");

const URL = process.env.FALSIFIER_URL || "http://127.0.0.1:8899/";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();

  let fail = 0;
  const chk = (label, cond, got) => {
    console.log(`  ${cond ? "OK  " : "FAIL"}  ${label}${cond ? "" : "  → " + JSON.stringify(got)}`);
    if (!cond) fail++;
  };

  console.log("\n[1] 联网状态下等引擎就绪");
  await page.goto(URL, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.getElementById("status-text").textContent.includes("就绪"),
    { timeout: 180000 }
  );
  console.log("  引擎已就绪");

  console.log("\n[2] 第一次运行（联网）");
  await page.click("#btn-demo");
  await page.waitForFunction(
    () => !document.getElementById("results").classList.contains("hidden"),
    { timeout: 120000 }
  );
  const v1 = await page.textContent("#verdict-pill");
  chk("第一次裁决 FALSIFIED", v1.trim() === "FALSIFIED", v1);

  console.log("\n[3] 掐断所有网络（除同源静态文件）");
  let blocked = 0;
  await page.route("**/*", route => {
    const u = route.request().url();
    if (u.startsWith(URL)) return route.continue();   // 同源文件照常
    blocked++;
    return route.abort();
  });
  console.log("  已拦截外部请求");

  console.log("\n[4] 断网后再运行一次");
  // 先收起结果，这样才能检测到"重新跑出来了"
  await page.evaluate(() => document.getElementById("results").classList.add("hidden"));
  await page.click("#btn-demo");
  await page.waitForFunction(
    () => !document.getElementById("results").classList.contains("hidden"),
    { timeout: 120000 }
  );
  const v2 = (await page.textContent("#verdict-pill")).trim();
  const kpi2 = await page.$$eval("#kpis .kpi .v", els => els.map(e => e.textContent.trim()));

  chk("断网后仍出裁决", v2 === "FALSIFIED", v2);
  chk("断网后 KPI 仍是 204 笔", kpi2.includes("204"), kpi2);
  chk("断网后最坏口径仍是 -54.31%", kpi2.includes("-54.31%"), kpi2);
  chk("断网后没有新的外部请求被发出", blocked === 0, blocked + " 个外部请求");

  console.log(`\n${fail === 0 ? "离线运行验证通过" : fail + " 项失败"}`);
  await browser.close();
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error("离线验证失败:", e.message); process.exit(1); });