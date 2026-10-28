/**
 * 消耗报表页自检：概览 / TOP 客户 / 趋势悬停 / 四个报表 Tab / 明细 / 导入弹窗。
 * 只读 + 预校验，不落库。
 *
 * 跑法：后端 8012、前端 5173 起着，然后
 *   E2E_BROWSER=D:\Quark\quark.exe node spend-report-check.mjs
 */
import puppeteer from "puppeteer-core";

const BROWSER =
  process.env.E2E_BROWSER || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:5173";
const SHOT = "C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-17-55-51\\.shots";

const results = [];
const errors = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok });
  console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}${detail ? "  " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: BROWSER,
  headless: "new",
  args: ["--no-sandbox", "--disable-gpu"],
  defaultViewport: { width: 1680, height: 1050 },
});
const page = await browser.newPage();
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));
page.on("response", (r) => {
  if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`);
});

async function clickText(text) {
  return page.evaluate((t) => {
    const el = [...document.querySelectorAll("button")].find(
      (b) => b.textContent.replace(/\s+/g, "").includes(t.replace(/\s+/g, ""))
    );
    if (!el) return false;
    el.click();
    return true;
  }, text);
}

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
await sleep(1200);

// 进消耗报表
check("导航：消耗报表", await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find(
    (x) => x.textContent.trim() === "消耗报表"
  );
  b?.click();
  return !!b;
}));
await page.waitForFunction(
  () => document.querySelectorAll("table.report tbody tr").length > 0,
  { timeout: 20000 }
);
await sleep(600);

let text = await page.evaluate(() => document.body.innerText);
check("数据概览三张 KPI", ["GMV Max + BC 总消耗", "GMV Max 总消耗", "BC 总消耗"].every((k) => text.includes(k)));
check("TOP 客户区块", text.includes("消耗 TOP10 客户"));
check("趋势图渲染", await page.evaluate(() => !!document.querySelector("svg.trend-svg")));
check("客户报表默认选中", text.includes("客户报表") && text.includes("对比"));

// 趋势悬停出 tooltip
const box = await page.$eval("svg.trend-svg", (el) => {
  const r = el.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
});
await page.mouse.move(box.x + box.w * 0.55, box.y + box.h * 0.5);
await sleep(250);
check("趋势悬停 tooltip", await page.evaluate(() => {
  const t = document.querySelector(".tip-title");
  return !!t && /^\d{4}-\d{2}-\d{2}$/.test(t.textContent || "");
}));

// 账户报表
check("切换账户报表", await clickText("账户报表"));
await sleep(900);
text = await page.evaluate(() => document.body.innerText);
check("账户报表出现账户 ID", text.includes("act_1001"));
check(
  "TOP 客户不随 Tab 消失",
  (await page.$$eval(".top-item", (els) => els.length)) > 0
);

// 销售 / 代理商
check("切换销售报表", await clickText("销售报表"));
await sleep(800);
text = await page.evaluate(() => document.body.innerText);
check("销售报表出现销售名", text.includes("李四"));
check("切换代理商报表", await clickText("代理商报表"));
await sleep(800);
text = await page.evaluate(() => document.body.innerText);
check("代理商报表出现代理商", text.includes("Yatop"));

// 明细
check("切换消耗明细", await clickText("消耗明细"));
await page.waitForFunction(
  () => document.querySelectorAll("table.report.detail tbody tr").length > 0,
  { timeout: 15000 }
);
text = await page.evaluate(() => document.body.innerText);
check("明细表有逐笔数据", text.includes("账户 ID") && text.includes("来源"));

// 回到客户报表再开导入弹窗
check("切回客户报表", await clickText("客户报表"));
await sleep(800);

// 宽表横向滚动：媒体/日期/客户三列应钉在左侧
const sticky = await page.evaluate(() => {
  const wrap = document.querySelector(".report-table-wrap");
  const cell = document.querySelector("table.report:not(.compact) tbody td");
  if (!wrap || !cell) return null;
  wrap.scrollLeft = 900;
  const wr = wrap.getBoundingClientRect();
  const cr = cell.getBoundingClientRect();
  return { scroll: wrap.scrollLeft, delta: Math.abs(cr.left - wr.left) };
});
await sleep(250);
check("横向滚动后首列仍可见", !!sticky && sticky.scroll > 0 && sticky.delta < 3);
await page.screenshot({ path: `${SHOT}/spend-report-scrolled.png`, fullPage: true });
await page.evaluate(() => {
  const wrap = document.querySelector(".report-table-wrap");
  if (wrap) wrap.scrollLeft = 0;
});

check("打开导入弹窗", await clickText("导入消耗"));
await page.waitForFunction(() => !!document.querySelector(".modal"), { timeout: 10000 });
check("弹窗含预校验/导入", await page.evaluate(() => {
  const t = document.querySelector(".modal")?.innerText || "";
  return t.includes("预校验") && t.includes("导入");
}));
check("填入示例", await clickText("填入示例"));
await sleep(200);
check("示例解析出 2 行", await page.evaluate(() => document.body.innerText.includes("已解析 2 行")));
check("执行预校验", await clickText("预校验"));
await page.waitForFunction(
  () => (document.body.innerText || "").includes("预校验完成"),
  { timeout: 15000 }
);
text = await page.evaluate(() => document.querySelector(".modal")?.innerText || "");
check("预校验不落库", text.includes("未落库"));
check("预校验通过 2 行", text.includes("可导入 2 行"));

await page.screenshot({ path: `${SHOT}/spend-report.png`, fullPage: true });
await browser.close();

console.log("\n--- 浏览器控制台错误 ---");
console.log(errors.length ? errors.join("\n") : "无");
const failed = results.filter((r) => !r.ok).length;
console.log(`\n通过 ${results.length - failed} / ${results.length}`);
process.exit(failed ? 1 : 0);
