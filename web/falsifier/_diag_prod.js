const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("console", m => console.log("[" + m.type() + "]", m.text().slice(0, 200)));
  p.on("pageerror", e => console.log("PAGEERROR:", e.message));
  p.on("requestfailed", r => console.log("FAILED:", r.url().slice(0, 110), "→", (r.failure() || {}).errorText));
  p.on("response", r => {
    const u = r.url();
    if (/pyodide|sqlite/.test(u)) console.log("RESP", r.status(), u.replace("https://cdn.jsdelivr.net", ""));
  });
  await p.goto("https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/falsifier/", { waitUntil: "domcontentloaded" });
  for (let i = 0; i < 12; i++) {
    await p.waitForTimeout(10000);
    const st = await p.textContent("#status-text");
    console.log(`t+${(i + 1) * 10}s  status="${st}"`);
    if (st.includes("就绪") || st.includes("失败")) break;
  }
  console.log("META:", await p.textContent("#engine-meta"));
  console.log("ERR:", (await p.textContent("#error-text").catch(() => "")).slice(0, 300));
  await b.close();
})();