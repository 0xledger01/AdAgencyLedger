/**
 * 客户端「BC 管理」自检：
 * 入口在门户侧栏 / 只看自己的 BCID / 分页 / 默认绑定单选 / 手工添加 /
 * 删除后刷新不回弹 / 客户之间数据隔离。
 *
 * 会临时给 client1 加 6 条 BCP 前缀的 BCID，跑完按门户 API 删除；
 * 中途失败也会在 finally 里清理。
 *
 * 跑法：后端 8012、前端 5173 起着，然后
 *   E2E_BROWSER=D:\Quark\quark.exe node portal-bc-check.mjs
 */
import puppeteer from "puppeteer-core";

const BROWSER =
  process.env.E2E_BROWSER || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:5173";
const API = "http://127.0.0.1:8012";
const SHOT = "C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-17-55-51\\.shots";

const results = [];
const errors = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}${detail ? "  " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, token, method = "GET", body) {
  const resp = await fetch(API + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(`${method} ${path} -> ${resp.status} ${JSON.stringify(payload)}`);
  return payload;
}

/* ---------------- 夹具 ---------------- */

const c1 = await api("/api/auth/portal/login", null, "POST", {
  username: "client1",
  password: "123456",
});
const c1Token = c1.token;
const before = await api("/api/portal/bcids?limit=200", c1Token);
const originalDefault = before.summary.default_bcid;
console.log(`[setup] client1 原有 ${before.total} 条，默认 ${originalDefault ?? "无"}`);

const stamp = Date.now();
const SEED = Array.from({ length: 6 }, (_, i) => `BCP${stamp}${String(i).padStart(2, "0")}`);
const EXTRA = `BCPUI${stamp}`;
for (const bcid of SEED) {
  await api("/api/portal/bcids", c1Token, "POST", { bcid });
}
const totalAfterSeed = before.total + SEED.length;

async function cleanup(browser) {
  if (browser) await browser.close().catch(() => {});
  const list = await api("/api/portal/bcids?limit=200", c1Token);
  for (const item of list.items) {
    if (item.bcid.startsWith("BCP")) {
      await api(`/api/portal/bcids/${item.id}`, c1Token, "DELETE").catch(() => {});
    }
  }
  if (originalDefault) {
    const after = await api("/api/portal/bcids?limit=200", c1Token);
    const row = after.items.find((i) => i.bcid === originalDefault);
    if (row && !after.summary.default_bcid) {
      await api(`/api/portal/bcids/${row.id}/default`, c1Token, "POST", {
        is_default: true,
      }).catch(() => {});
    }
  }
  const left = await api("/api/portal/bcids?limit=200", c1Token);
  console.log(`[cleanup] client1 可见 ${left.total} 条（应为 ${before.total}）`);
}

/* ---------------- 浏览器 ---------------- */

let browser;
try {
  browser = await puppeteer.launch({
    executablePath: BROWSER,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu"],
    defaultViewport: { width: 1680, height: 1050 },
  });

  async function loginPortal(page, username, password) {
    await page.goto(`${BASE}/portal`, { waitUntil: "networkidle2" });
    await page.waitForSelector(".public-login-btn", { timeout: 20000 });
    await page.click(".public-login-btn");
    await page.waitForFunction(
      () => document.querySelector(".login-title")?.textContent === "海外客户端系统",
      { timeout: 20000 }
    );
    await page.waitForSelector(".login-card input", { timeout: 20000 });
    await sleep(300);
    const inputs = await page.$$(".login-card input");
    for (const [i, v] of [username, password].entries()) {
      await inputs[i].evaluate((node, val) => {
        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(
          node,
          val
        );
        node.dispatchEvent(new Event("input", { bubbles: true }));
      }, v);
    }
    await Promise.all([
      page.click('.login-card button[type="submit"]'),
      page.waitForFunction(() => !document.querySelector(".login-card"), { timeout: 20000 }),
    ]);
    await page.waitForSelector(".nav-item", { timeout: 20000 });
  }

  function watch(page) {
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("response", (r) => r.status() >= 400 && errors.push(`HTTP ${r.status()} ${r.url()}`));
  }

  const navTo = (page, label) =>
    page.evaluate((text) => {
      const b = [...document.querySelectorAll(".nav-item")].find(
        (x) => x.textContent.trim() === text
      );
      b?.click();
      return !!b;
    }, label);

  const hasRow = (page, bcid) =>
    page.evaluate((value) => {
      return [...document.querySelectorAll(".bc-table tbody tr")].some(
        (r) => r.querySelector(".bc-bcid")?.textContent.trim() === value
      );
    }, bcid);

  const clickRowButton = (page, bcid, selector) =>
    page.evaluate(
      ({ value, sel }) => {
        const tr = [...document.querySelectorAll(".bc-table tbody tr")].find(
          (r) => r.querySelector(".bc-bcid")?.textContent.trim() === value
        );
        const el = tr?.querySelector(sel);
        el?.click();
        return !!el;
      },
      { value: bcid, sel: selector }
    );

  /* ---- client1：功能 ---- */
  const page = await browser.newPage();
  watch(page);
  await loginPortal(page, "client1", "123456");
  const navText = await page.evaluate(() =>
    [...document.querySelectorAll(".nav-item")].map((x) => x.textContent.trim())
  );
  check("门户侧栏出现 BC 管理", navText.includes("BC 管理"), navText.join("/"));
  check(
    "门户侧栏没有内部菜单",
    !navText.some((t) => ["客户管理", "资金流水", "消耗报表", "充值管理"].includes(t))
  );

  check("点击 BC 管理", await navTo(page, "BC 管理"));
  await page.waitForSelector(".bc-table", { timeout: 20000 });
  await sleep(1200);
  check("URL 进入 /portal/bc", page.url().includes("/portal/bc"), page.url());
  check(
    "页面标题 BC 管理",
    (await page.evaluate(() => document.querySelector(".page-head h1")?.textContent.trim())) ===
      "BC 管理"
  );
  check(
    "说明文案",
    (await page.evaluate(() => document.body.innerText)).includes(
      "管理你绑定过或手工添加的 BCID"
    )
  );
  check(
    "客户端没有客户选择器",
    (await page.$$(".bc-scope-select")).length === 0
  );
  check(
    `列表共 ${totalAfterSeed} 条`,
    (await page.evaluate(() => document.body.innerText)).includes(`共 ${totalAfterSeed} 条`)
  );

  // 分页
  const totalPages = Math.ceil(totalAfterSeed / 10);
  check(
    `分页出现第 ${totalPages} 页`,
    await page.evaluate(
      (n) =>
        [...document.querySelectorAll(".pager-btns .btn")].some(
          (b) => b.textContent.trim() === String(n)
        ),
      totalPages
    )
  );
  await page.evaluate(() =>
    [...document.querySelectorAll(".pager-btns .btn")]
      .find((b) => b.textContent.trim() === "2")
      ?.click()
  );
  await sleep(1200);
  const rowsP2 = await page.evaluate(
    () => document.querySelectorAll(".bc-table tbody tr .bc-bcid").length
  );
  check("翻到第 2 页行数正确", rowsP2 === totalAfterSeed - 10, `rows=${rowsP2}`);
  await page.evaluate(() =>
    [...document.querySelectorAll(".pager-btns .btn")]
      .find((b) => b.textContent.trim() === "1")
      ?.click()
  );
  await sleep(1200);

  // 默认绑定单选
  check("点击临时行的默认勾选", await clickRowButton(page, SEED[1], 'input[type="checkbox"]'));
  await sleep(1500);
  check(
    "默认项全表唯一",
    await page.evaluate((bcid) => {
      const rows = [...document.querySelectorAll(".bc-table tbody tr")];
      const checked = rows.filter((r) => r.querySelector('input[type="checkbox"]')?.checked);
      return (
        checked.length === 1 &&
        checked[0].querySelector(".bc-bcid")?.textContent.trim() === bcid
      );
    }, SEED[1])
  );
  check(
    "默认提示同步",
    (await page.evaluate(() => document.querySelector(".client-pager")?.innerText || "")).includes(
      SEED[1]
    )
  );

  // 手工添加 + 删除
  check(
    "打开添加弹窗",
    await page.evaluate(() => {
      const b = [...document.querySelectorAll("button")].find((x) =>
        x.textContent.replace(/\s+/g, "").includes("添加BCID")
      );
      b?.click();
      return !!b;
    })
  );
  await page.waitForSelector(".modal-mask .modal", { timeout: 10000 });
  await page.evaluate((value) => {
    const input = document.querySelector(".modal-mask .modal input");
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(
      input,
      value
    );
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, EXTRA);
  await page.evaluate(() => {
    [...document.querySelectorAll(".modal-foot .btn")]
      .find((b) => b.textContent.trim() === "添加")
      ?.click();
  });
  await page.waitForFunction(() => !document.querySelector(".modal-mask"), { timeout: 10000 });
  await sleep(1600);
  check("添加后出现在第 1 页", await hasRow(page, EXTRA));

  check("点击删除", await clickRowButton(page, EXTRA, ".bc-delete"));
  await page.waitForSelector(".modal-mask .modal", { timeout: 10000 });
  check(
    "删除弹窗说明不会被补回",
    (await page.evaluate(() => document.querySelector(".modal-mask .modal")?.innerText || "")).includes(
      "不会随刷新自动补回"
    )
  );
  await page.evaluate(() => {
    [...document.querySelectorAll(".modal-foot .btn")]
      .find((b) => b.textContent.replace(/\s+/g, "").includes("确认删除"))
      ?.click();
  });
  await page.waitForFunction(() => !document.querySelector(".modal-mask"), { timeout: 10000 });
  await sleep(1500);
  check("删除后行消失", !(await hasRow(page, EXTRA)));
  await page.evaluate(() => {
    [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "刷新")?.click();
  });
  await sleep(1800);
  check("刷新后已删除行不会回来", !(await hasRow(page, EXTRA)));

  await page.screenshot({ path: `${SHOT}\\portal-bc.png`, fullPage: true });

  /* ---- client2：隔离 ---- */
  const ctx2 = await browser.createBrowserContext();
  const page2 = await ctx2.newPage();
  watch(page2);
  await loginPortal(page2, "client2", "123456");
  check("客户二也能进 BC 管理", await navTo(page2, "BC 管理"));
  await page2.waitForSelector(".bc-table", { timeout: 20000 });
  await sleep(1200);
  const page2Text = await page2.evaluate(() => document.body.innerText);
  check(
    "客户二看不到客户一的 BCID",
    !page2Text.includes("765557932675017934") && !page2Text.includes(SEED[1])
  );
  check("客户二只看到自己的条数", !page2Text.includes(`共 ${totalAfterSeed} 条`));
  await ctx2.close();
} finally {
  await cleanup(browser);
}

/* ---------------- 汇总 ---------------- */

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
if (errors.length) {
  console.log("控制台/HTTP 错误：");
  for (const e of errors) console.log("  -", e);
}
if (failed.length || errors.length) process.exit(1);
