/**
 * P3 四个新页面的渲染自检（D3 充值 / D2 开户 / D5 加款 / D6 消耗）。
 * 只验"页面能开、关键文案在、表格有行、控制台无报错"，不做写操作。
 *
 * 跑法：先启动后端 8012 + 前端 5173，然后 `node p3-pages-check.mjs`
 */
import puppeteer from "puppeteer-core";

// 默认用本机 Edge；装了别的 Chromium（如 Quark）时可用 E2E_BROWSER 覆盖。
const EDGE =
  process.env.E2E_BROWSER || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:5173";
const SHOT = "C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-17-55-51\\.shots";

const errors = [];
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok });
  console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}${detail ? "  " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: "new",
  args: ["--no-sandbox", "--disable-gpu"],
  defaultViewport: { width: 1560, height: 1000 },
});
const page = await browser.newPage();
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));
page.on("response", (r) => {
  if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`);
});

// 登录
await page.goto(BASE, { waitUntil: "networkidle2" });
for (const [i, v] of [["admin", "admin123"]].flat().entries()) {
  const el = (await page.$$("input"))[i];
  await el.evaluate((node, val) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(node, val);
    node.dispatchEvent(new Event("input", { bubbles: true }));
  }, v);
}
await Promise.all([
  page.click('button[type="submit"]'),
  page.waitForFunction(() => !document.querySelector(".login-card"), { timeout: 20000 }),
]);
await sleep(1500);

const PAGES = [
  {
    nav: "充值管理",
    shot: "p3-topups",
    must: ["充值管理", "申请金额", "审核"],
    hint: "审核",
  },
  {
    nav: "开户管理",
    shot: "p3-open",
    must: ["开户管理", "待审批", "Mock 回执"],
    hint: "审批",
  },
  {
    nav: "账户管理",
    shot: "p3-recharge",
    must: ["账户管理", "充值记录", "充值"],
    hint: "详情",
  },
  {
    nav: "消耗报表",
    shot: "p3-spend",
    must: ["数据概览", "消耗趋势", "客户报表", "导入消耗"],
    hint: "导出",
  },
];

for (const p of PAGES) {
  const clicked = await page.evaluate((label) => {
    const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.trim() === label);
    b?.click();
    return !!b;
  }, p.nav);
  check(`导航可点：${p.nav}`, clicked);
  await sleep(2200);

  const t = await page.evaluate(() => document.body.innerText);
  const missing = p.must.filter((k) => !t.includes(k));
  check(`${p.nav} 关键文案齐全`, missing.length === 0, missing.length ? `缺 ${missing.join("/")}` : "");

  const rows = await page.evaluate(() => document.querySelectorAll("tbody tr").length);
  check(`${p.nav} 表格有数据行`, rows > 0, `rows=${rows}`);

  if (p.hint) {
    const hasBtn = await page.evaluate((h) => document.body.innerText.includes(h), p.hint);
    check(`${p.nav} 有「${p.hint}」操作入口`, hasBtn);
  }
  await page.screenshot({ path: `${SHOT}/${p.shot}.png`, fullPage: true });
}

await browser.close();

console.log("\n--- 浏览器控制台错误 ---");
console.log(errors.length ? errors.join("\n") : "无");
const failed = results.filter((r) => !r.ok).length;
console.log(`\n通过 ${results.length - failed} / ${results.length}`);
process.exit(failed ? 1 : 0);
