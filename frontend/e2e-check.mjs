import puppeteer from "puppeteer-core";
import fs from "node:fs";

// 默认用本机 Edge；装了别的 Chromium（如 Quark）时可用 E2E_BROWSER 覆盖。
// 这台机器上 Edge 被策略拦住无法由 puppeteer 启动，所以跑之前先设 E2E_BROWSER。
const EDGE =
  process.env.E2E_BROWSER || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:5173";
const SHOT = "C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-17-55-51\\.shots";

const errors = [];
const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "[OK]  " : "[FAIL]"} ${name}${detail ? "  " + detail : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function texts(page) {
  return page.evaluate(() => document.body.innerText);
}

async function fill(page, index, value) {
  const el = (await page.$$("input"))[index];
  await el.evaluate((node, v) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(node, v);
    node.dispatchEvent(new Event("input", { bubbles: true }));
  }, value);
}

async function loginAs(page, username, password, portal) {
  await page.goto(portal ? `${BASE}/portal` : BASE, { waitUntil: "networkidle2" });
  // 门户登录页默认是「开户申请」落地页，登录入口在右上角；点开后才出现登录卡。
  if (portal) {
    await page.waitForSelector(".public-login-btn", { timeout: 20000 });
    await page.click(".public-login-btn");
  }
  // 等目标端的登录卡真正渲染出来，避免拿到上一端残留的表单（后台 → 门户时踩过）
  await page.waitForFunction(
    (isPortal) => {
      const h = document.querySelector(".login-title");
      return !!h && h.textContent === (isPortal ? "海外客户端系统" : "海外广告代理业务系统");
    },
    { timeout: 20000 },
    portal
  );
  // Quark/Chromium 的 networkidle2 可能早于 React 首屏；显式等输入框出现再填。
  await page.waitForSelector(".login-card input", { timeout: 20000 });
  await sleep(300);
  await fill(page, 0, username);
  await fill(page, 1, password);
  // 以登录后的侧栏出现作为成功信号，避免旧页面的 .login-card 还没卸载造成误判。
  // Vite 刚编译完 / 机器忙时 React 的 onSubmit 可能还没挂上，第一次点击会走原生表单提交
  // 把页面刷掉（输入被清空）——所以点击失败就重填、补一次 requestSubmit。
  let loggedIn = await clickLogin(page);
  if (!loggedIn) {
    await page.waitForSelector(".login-card input", { timeout: 10000 }).catch(() => {});
    await sleep(300);
    await fill(page, 0, username);
    await fill(page, 1, password);
    await page.evaluate(() => document.querySelector("form.login-card")?.requestSubmit());
    loggedIn = await page
      .waitForSelector(".sidebar", { timeout: 15000 })
      .then(() => true)
      .catch(() => false);
  }
  if (!loggedIn) throw new Error(`登录失败：${username}`);
  await sleep(1400);
}

/** 点登录按钮并等侧栏；返回是否真的登进去了。 */
async function clickLogin(page) {
  try {
    await page.click('button[type="submit"]');
  } catch {
    // 页面已经跳走时点击会抛，交给下面的侧栏判定
  }
  return page
    .waitForSelector(".sidebar", { timeout: 12000 })
    .then(() => true)
    .catch(() => false);
}

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: "new",
  args: ["--no-sandbox", "--disable-gpu", "--window-size=1560,1000"],
  defaultViewport: { width: 1560, height: 1000 },
});

const page = await browser.newPage();
page.on("console", (m) => {
  if (m.type() === "error") errors.push(m.text());
});
page.on("pageerror", (e) => errors.push(String(e)));
// 500 只报「Failed to load resource」不带地址，定位不了是哪个接口；把 URL 一起记下来
page.on("response", (r) => {
  if (r.status() >= 500) errors.push(`HTTP ${r.status()} ${r.request().method()} ${r.url()}`);
});

// ---------- 内部后台 ----------
await loginAs(page, "admin", "admin123", false);

// 审核用例会改变状态，重跑前把演示客户重置回审核起点（只清审核记录，不动流水）
await page.evaluate(async () => {
  const s = JSON.parse(localStorage.getItem("adsys.session") || "null");
  await fetch("/api/dev/reset-demo", {
    method: "POST",
    headers: s?.token ? { Authorization: `Bearer ${s.token}` } : {},
  });
});
await page.reload({ waitUntil: "networkidle2" });
await sleep(1200);

let t = await texts(page);
check(
  "管理员登录后进入资金总览",
  // 「共 N 个广告账户」计数行与总览副标题已按业务要求注释下线，这里不再校验这两句文案。
  t.includes("资金总览"),
  ""
);
await page.screenshot({ path: `${SHOT}/desk-overview.png` });

const walletCards = await page.evaluate(
  () => document.querySelectorAll(".wallet-card").length
);
check(
  "总览按广告账户逐卡展示余额",
  walletCards > 0 && t.includes("钱包余额（") && t.includes("更新于"),
  `cards=${walletCards}`
);
check(
  "总览不再按一代/客户做汇总表",
  !t.includes("全部一代合计") && !t.includes("平台钱包 · 按客户") && !t.includes("广告账户余额 · 按一代"),
  ""
);
check(
  "总览不再单列客户子钱包与一代钱包（同一份钱只在一处看）",
  !t.includes("客户子钱包") && !t.includes("一代钱包"),
  [t.includes("客户子钱包") ? "仍出现客户子钱包" : "", t.includes("一代钱包") ? "仍出现一代钱包" : ""]
    .filter(Boolean)
    .join(" / ")
);
// 「资金总金额」按业务要求已去掉：广告账户余额（媒体侧预算）与平台钱包（我方负债）
// 相加在业务上不成立，留着只会让人误判。
check("总览不再显示资金总金额", !t.includes("资金总金额"), "");
// 筛选控件是 input[placeholder] + select>option，都不进 innerText，必须查 DOM
// 这个区块要多拉一次接口才渲染，固定 sleep 会偶发抢跑，显式等它出现
await page
  .waitForFunction(
    () =>
      [...document.querySelectorAll(".card-head h2")].some((h) =>
        (h.textContent || "").includes("交易明细")
      ),
    { timeout: 15000 }
  )
  .catch(() => {});
const rechargeBlock = await page.evaluate(() => {
  const headings = [...document.querySelectorAll(".card-head h2")].map((h) => h.textContent || "");
  const hasCard = headings.some((x) => x.includes("交易明细"));
  const placeholders = [...document.querySelectorAll("input[placeholder]")].map(
    (i) => i.getAttribute("placeholder") || ""
  );
  const selectOptCounts = [...document.querySelectorAll("select")].map((s) => ({
    opts: [...s.options].map((o) => o.textContent || ""),
    count: s.options.length,
  }));
  const flat = selectOptCounts.flatMap((s) => s.opts);
  const txnSel = selectOptCounts.find((s) => s.opts.some((x) => x.includes("全部交易类型")));
  const statusSel = selectOptCounts.find((s) => s.opts.some((x) => x.includes("全部状态")));
  const agentSel = selectOptCounts.find((s) => s.opts.some((x) => x.includes("选择代理商")));
  const buttons = [...document.querySelectorAll("button")].map((b) => b.textContent || "");
  return {
    hasCard,
    hasAgentSearch: placeholders.some((p) => p.includes("客户名称/编号")),
    hasAccountSearch: placeholders.some((p) => p.includes("广告账户ID")),
    hasTxn: flat.some((x) => x.includes("全部交易类型")),
    hasStatus: flat.some((x) => x.includes("全部状态")),
    hasAgent: flat.some((x) => x.includes("选择代理商")),
    hasExport: buttons.some((x) => x.trim() === "导出"),
    // 选项数：只有"全部 XXX"一项 == 枚举没下来（下拉等于摆设）
    txnOptCount: txnSel?.count ?? 0,
    statusOptCount: statusSel?.count ?? 0,
    agentOptCount: agentSel?.count ?? 0,
  };
});
check(
  "总览有交易明细区块与五维筛选/导出",
  rechargeBlock.hasCard &&
    rechargeBlock.hasAgentSearch &&
    rechargeBlock.hasAccountSearch &&
    rechargeBlock.hasTxn &&
    rechargeBlock.hasStatus &&
    rechargeBlock.hasAgent &&
    rechargeBlock.hasExport,
  JSON.stringify(rechargeBlock)
);
// 选项必须真的下来：状态 7 种 + 全部；代理商至少一家 + 占位项
check(
  "交易明细下拉都有真实选项（不是空壳）",
  rechargeBlock.txnOptCount >= 2 &&
    rechargeBlock.statusOptCount >= 8 &&
    rechargeBlock.agentOptCount >= 2,
  `txn=${rechargeBlock.txnOptCount} status=${rechargeBlock.statusOptCount} agent=${rechargeBlock.agentOptCount}`
);
check(
  "交易明细含到账金额与同步时间列",
  t.includes("到账金额") && t.includes("同步时间") && t.includes("交易金额"),
  ""
);
check("总览不再显示账本自检面板（已按要求下线）", !t.includes("账本自检"), "");

// 客户管理
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("客户管理"));
  b?.click();
});
await sleep(1500);
t = await texts(page);
check("客户管理页加载", t.includes("客户列表") && t.includes("待初审"));
check("客户列表有种子客户", t.includes("某某公司"));
await page.screenshot({ path: `${SHOT}/desk-clients.png` });

// 打开详情 + 审核
const opened = await page.evaluate(() => {
  const rows = [...document.querySelectorAll("tbody tr")];
  // 公司名已统一脱敏成「某某公司」，按状态徽标找待审核的那家
  const target = rows.find((r) => r.textContent.includes("待审核"));
  const btn = target?.querySelector("button");
  btn?.click();
  return !!btn;
});
await sleep(1500);
if (opened) {
  t = await texts(page);
  check("详情抽屉打开", t.includes("商务条款") && t.includes("审核记录"));
  check("详情显示审核步骤", t.includes("销售初审") && t.includes("风控审核"));
  await page.screenshot({ path: `${SHOT}/desk-client-detail.png` });

  const hasAuditBtn = await page.evaluate(() =>
    [...document.querySelectorAll(".drawer-foot button")].some((b) => /通过/.test(b.textContent))
  );
  check("审核按钮按角色显示", hasAuditBtn, "");
  await page.evaluate(() => {
    const b = [...document.querySelectorAll(".drawer-foot button")].find((x) => /通过/.test(x.textContent));
    b?.click();
  });
  await sleep(1600);
  t = await texts(page);
  check("审核操作后状态流转", t.includes("已通过") || t.includes("待风控"), "");
  await page.screenshot({ path: `${SHOT}/desk-after-review.png` });
}

// 资金流水 —— 台账口径：四个筛选 + 一张表 + 翻页
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("资金流水"));
  b?.click();
});
await sleep(2400);
t = await texts(page);
check(
  "流水页为单表台账",
  t.includes("账单ID") && t.includes("交易类型") && t.includes("交易前余额") && t.includes("交易后余额"),
  ""
);

// 筛选控件是 input[placeholder] + select>option，都不进 innerText，必须查 DOM
const flowBar = await page.evaluate(() => {
  const ph = [...document.querySelectorAll("input[placeholder]")].map((i) => i.getAttribute("placeholder") || "");
  const sels = [...document.querySelectorAll("select")];
  const pick = (kw) => sels.find((s) => [...s.options].some((o) => (o.textContent || "").includes(kw)));
  const pager = document.querySelector(".client-pager");
  return {
    bill: ph.some((p) => p.includes("账单ID")),
    client: ph.some((p) => p.includes("客户名称/编号")),
    agent: !!pick("请选择代理商"),
    txn: !!pick("交易类型"),
    agentCount: pick("请选择代理商")?.options.length ?? 0,
    txnCount: pick("交易类型")?.options.length ?? 0,
    pagerText: (pager?.innerText || "").includes("前往"),
    pagerSizes: [...(pager?.querySelectorAll("select") ?? [])].flatMap((s) =>
      [...s.options].map((o) => o.textContent || "")
    ),
    jumpInput: document.querySelectorAll(".pager-jump").length,
  };
});
check(
  "流水页四个筛选齐全",
  flowBar.bill && flowBar.client && flowBar.agent && flowBar.txn,
  JSON.stringify(flowBar)
);
check(
  "流水筛选下拉有真实选项",
  flowBar.agentCount >= 2 && flowBar.txnCount >= 2,
  `agent=${flowBar.agentCount} txn=${flowBar.txnCount}`
);
check(
  "流水页有翻页条（条数/页 + 前往）",
  flowBar.pagerText && flowBar.pagerSizes.some((x) => x.includes("条/页")) && flowBar.jumpInput > 0,
  JSON.stringify(flowBar)
);

// 字段查询：客户名已统一脱敏成「某某公司」，改用唯一的 CRM 编号精确命中一家，
// 命中一家时应带出该客户的期初/期末
await page.evaluate(() => {
  const el = [...document.querySelectorAll("input[placeholder]")].find((i) =>
    (i.getAttribute("placeholder") || "").includes("客户名称")
  );
  if (!el) return;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
  setter.call(el, "H269151");
  el.dispatchEvent(new Event("input", { bubbles: true }));
});
await sleep(300);
await page.evaluate(() => {
  [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "查询")?.click();
});
await sleep(1800);
t = await texts(page);
check("按客户查询带出期初→期末", t.includes("期初 → 期末"), "");
check("台账表有数据行", t.includes("现金账户充值") || t.includes("广告账户充值"), "");
await page.screenshot({ path: `${SHOT}/desk-ledger.png` });

// 先清掉客户筛选：广告账户充值只发生在另一个客户身上，叠加客户条件会筛成 0 行
await page.evaluate(() => {
  [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "重置")?.click();
});
await sleep(1600);

// 交易类型筛选：广告账户充值 → 广告账户ID 列必须有值
await page.evaluate(() => {
  const sel = [...document.querySelectorAll("select")].find((s) =>
    [...s.options].some((o) => (o.textContent || "").includes("交易类型"))
  );
  if (!sel) return;
  const opt = [...sel.options].find((o) => (o.textContent || "").trim() === "广告账户充值");
  sel.value = opt?.value ?? "";
  sel.dispatchEvent(new Event("change", { bubbles: true }));
});
await sleep(300);
await page.evaluate(() => {
  [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "查询")?.click();
});
await sleep(1600);
t = await texts(page);
check("交易类型可筛选", t.includes("广告账户充值"), "");
const accCells = await page.evaluate(() => {
  const head = [...document.querySelectorAll("table.flow-table thead th")].map((th) => (th.textContent || "").trim());
  const idx = head.indexOf("广告账户ID");
  const rows = [...document.querySelectorAll("table.flow-table tbody tr")];
  return { idx, values: rows.map((r) => (r.children[idx]?.textContent || "").trim()) };
});
check(
  "广告账户流水都带账户 ID",
  accCells.idx > 0 && accCells.values.length > 0 && accCells.values.every((v) => v && v !== "—"),
  JSON.stringify(accCells).slice(0, 200)
);
await page.screenshot({ path: `${SHOT}/desk-ledger-txn.png` });

// 重置
await page.evaluate(() => {
  [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "重置")?.click();
});
await sleep(1600);
t = await texts(page);
check("重置后回到全量台账", !t.includes("期初 → 期末"), "");

// 账户消耗视图
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".subtabs button")].find((x) => x.textContent.includes("账户消耗"));
  b?.click();
});
await sleep(1800);
t = await texts(page);
check("账户消耗视图可切换", t.includes("账户 ID") && t.includes("消耗"), "");
await page.screenshot({ path: `${SHOT}/desk-spend.png` });

// 管理端「交易记录」已按要求注释掉，本段自检一并注释；恢复页面时把下面整段放开即可。
// // ---------- 交易记录：只读流水（列 / 筛选 / 分页） ----------
// await page.evaluate(() => {
//   const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("交易记录"));
//   b?.click();
// });
// await sleep(2200);
// const txnPage = await page.evaluate(() => {
//   const heads = [...document.querySelectorAll("table thead th")].map((x) => x.textContent.trim());
//   const bar = document.querySelector(".txn-filterbar");
//   const rows = [...document.querySelectorAll("tbody tr")].filter((r) => !r.querySelector(".empty"));
//   return {
//     heads,
//     selects: [...(bar?.querySelectorAll("select") || [])].map((s) => s.options.length),
//     dates: (bar?.querySelectorAll('input[type="date"]') || []).length,
//     rows: rows.length,
//     firstTxn: (rows[0]?.children[0]?.textContent || "").trim(),
//     pager: !!document.querySelector(".client-pager"),
//   };
// });
// check(
//   "交易记录页列与 CRM 原型一致",
//   ["交易编号", "交易类型", "账户ID", "交易金额", "操作备注", "状态", "反馈信息", "操作人", "操作时间"].every(
//     (h) => txnPage.heads.includes(h)
//   ),
//   txnPage.heads.join("/")
// );
// check(
//   "交易记录筛选齐全（日期×2 + 类型/状态下拉）",
//   txnPage.dates === 2 && txnPage.selects.length >= 2 && txnPage.selects.every((n) => n >= 2),
//   JSON.stringify(txnPage)
// );
// check(
//   "交易记录有数据且编号是 ADTR 流水号",
//   txnPage.rows > 0 && txnPage.firstTxn.startsWith("ADTR") && txnPage.pager,
//   JSON.stringify(txnPage).slice(0, 220)
// );
// 
// // 状态胶囊必须按执行口径上色：已完成=绿、失败=红、待开始=灰（TXN_* 前缀那套）
// const txnBadges = await page.evaluate(() => {
//   const rows = [...document.querySelectorAll("tbody tr")];
//   return rows.map((r) => {
//     const cells = [...r.children];
//     const badge = r.querySelector(".badge");
//     return { status: (badge?.textContent || "").trim(), cls: badge?.className || "" };
//   });
// });
// const doneBadges = txnBadges.filter((b) => b.status === "已完成");
// const failedBadges = txnBadges.filter((b) => b.status === "失败");
// check(
//   "交易状态配色正确（已完成绿 / 失败红）",
//   doneBadges.every((b) => b.cls.includes("green")) && failedBadges.every((b) => b.cls.includes("red")),
//   JSON.stringify(txnBadges.slice(0, 3))
// );
// check(
//   "交易记录有刷新按钮",
//   await page.evaluate(() =>
//     [...document.querySelectorAll("button")].some((b) => (b.textContent || "").includes("刷新"))
//   ),
//   ""
// );
// 
// // 按交易编号精确查一条：客服报号就是拿这个号来查的
// const firstTxnNo = txnPage.firstTxn;
// await page.evaluate((no) => {
//   const el = [...document.querySelectorAll(".txn-filterbar input")].find((i) =>
//     (i.getAttribute("placeholder") || "").includes("交易编号")
//   );
//   if (!el) return;
//   const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
//   setter.call(el, no);
//   el.dispatchEvent(new Event("input", { bubbles: true }));
// }, firstTxnNo);
// await sleep(200);
// await page.evaluate(() => {
//   [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "查询")?.click();
// });
// await sleep(1500);
// const filtered = await page.evaluate(() => ({
//   text: document.body.innerText,
//   first: (document.querySelector("tbody tr td")?.textContent || "").trim(),
// }));
// check("交易编号可精确查询", filtered.text.includes(firstTxnNo) && filtered.first === firstTxnNo, filtered.first);
// 
// // 先清掉编号条件，否则「已完成」会和上一条 CANCELLED 单据叠加成 0 行
// await page.evaluate(() => {
//   [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "重置")?.click();
// });
// await sleep(1400);
// 
// // 状态筛选：已完成 → 页面里不能再出现「已撤销」
// await page.evaluate(() => {
//   const sel = [...document.querySelectorAll(".txn-filterbar select")].find((s) =>
//     [...s.options].some((o) => (o.textContent || "").includes("已撤销"))
//   );
//   if (!sel) return;
//   const opt = [...sel.options].find((o) => (o.textContent || "").trim() === "已完成");
//   sel.value = opt?.value ?? "";
//   sel.dispatchEvent(new Event("change", { bubbles: true }));
// });
// await sleep(200);
// await page.evaluate(() => {
//   [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "查询")?.click();
// });
// await sleep(1500);
// const statusFiltered = await page.evaluate(() => {
//   const rows = [...document.querySelectorAll("tbody tr")];
//   const labels = rows.map((r) => (r.querySelector(".badge")?.textContent || "").trim());
//   return { total: document.body.innerText, labels };
// });
// check(
//   "交易状态可筛选",
//   statusFiltered.labels.length > 0 && statusFiltered.labels.every((x) => x === "已完成"),
//   JSON.stringify(statusFiltered.labels)
// );
// await page.evaluate(() => {
//   [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "重置")?.click();
// });
// await sleep(1200);
// await page.screenshot({ path: `${SHOT}/desk-transactions.png` });

// ---------- 两个地址各自独立 ----------
await page.evaluate(() => localStorage.clear());
await page.goto(`${BASE}/portal`, { waitUntil: "networkidle2" });
await sleep(500);
t = await texts(page);
check("门户地址 /portal 显示海外客户端系统登录", t.includes("海外客户端系统") && !t.includes("海外广告代理业务系统"), t.slice(0, 60));
check("门户登录页无 Tab 切换", (await page.$$(".tabs")).length === 0);
check("门户登录页不显示测试账号提示", !t.includes("client1") && !t.includes("123456"), "");
await page.screenshot({ path: `${SHOT}/portal-login.png` });

await page.goto(BASE, { waitUntil: "networkidle2" });
await sleep(500);
t = await texts(page);
check("后台地址显示系统名", t.includes("海外广告代理业务系统"), "");
check("后台登录页无副标题", !t.includes("二级代理资金与投放管理"), "");
check("后台登录页标题居中", await page.evaluate(() => {
  const h = document.querySelector(".login-title");
  if (!h) return false;
  return getComputedStyle(h).textAlign === "center";
}), "");
await page.screenshot({ path: `${SHOT}/desk-login.png` });

// ---------- 客户门户 ----------
await loginAs(page, "client1", "123456", true);
t = await texts(page);
check("客户登录进入我的钱包", t.includes("我的钱包") || t.includes("总可用"), "");
check("门户不出现返点字样", !t.includes("返点"));
check("门户不出现成本系数", !t.includes("成本系数"));
await page.screenshot({ path: `${SHOT}/portal-wallet.png` });

const navText = await page.evaluate(() => document.querySelector(".sidebar")?.innerText || "");
check("客户端侧栏无内部菜单", !navText.includes("客户管理") && !navText.includes("资金流水"), navText.replace(/\n/g, "/"));
check("客户端侧栏有交易记录", navText.includes("交易记录"), navText.replace(/\n/g, "/"));

// ---------- 门户广告账户管理：筛选 / 列表 / 充值入口 ----------
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("广告账户"));
  b?.click();
});
await sleep(1500);
t = await texts(page);
check("客户侧栏可进入广告账户", t.includes("广告账户") && t.includes("账户余额"), "");
check("客户可见自己的广告账户", t.includes("act_1001") || t.includes("act_1002"), "");
const accPage = await page.evaluate(() => {
  const placeholders = [...document.querySelectorAll("input[placeholder]")].map(
    (i) => i.getAttribute("placeholder") || ""
  );
  const buttons = [...document.querySelectorAll("button")].map((b) => (b.textContent || "").trim());
  const heads = [...document.querySelectorAll("table thead th")].map((x) => x.textContent.trim());
  return {
    hasKeyword: placeholders.some((p) => p.includes("账户ID")),
    hasSubject: placeholders.some((p) => p.includes("开户主体")),
    hasQuery: buttons.includes("查询"),
    hasExport: buttons.includes("导出"),
    hasRecharge: buttons.includes("充值"),
    hasDateFilters: document.querySelectorAll(".pw-date input[type=date]").length >= 2,
    hasSubjectCol: heads.includes("开户主体"),
  };
});
check(
  "广告账户页有日期/账户/主体筛选与导出",
  accPage.hasKeyword && accPage.hasSubject && accPage.hasQuery && accPage.hasExport && accPage.hasDateFilters,
  JSON.stringify(accPage)
);
check("广告账户页有开户主体列与充值入口", accPage.hasSubjectCol && accPage.hasRecharge, "");
await page.screenshot({ path: `${SHOT}/portal-accounts.png` });

const openedRecharge = await page.evaluate(() => {
  const btn = [...document.querySelectorAll("table tbody button")].find(
    (b) => (b.textContent || "").trim() === "充值" && !b.disabled
  );
  if (!btn) return false;
  btn.click();
  return true;
});
await sleep(900);
t = await texts(page);
check(
  "广告账户充值弹层可打开并提示冻结流程",
  openedRecharge && t.includes("广告账户充值") && t.includes("现金可用") && t.includes("冻结"),
  t.slice(0, 80)
);
await page.screenshot({ path: `${SHOT}/portal-account-recharge.png` });
await page.evaluate(() => document.querySelector(".pw-close")?.click());
await sleep(400);

// 回到钱包页再看充值记录 Tab（广告账户已独立成页）
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("我的钱包"));
  b?.click();
});
await sleep(1200);

await page.evaluate(() => {
  const b = [...document.querySelectorAll(".subtabs button")].find((x) => x.textContent.includes("充值记录"));
  b?.click();
});
await sleep(1200);
t = await texts(page);
check("充值记录页可打开", t.includes("提交充值"), "");
await page.screenshot({ path: `${SHOT}/portal-topups.png` });

// ---------- 门户交易记录：只读、本客户、广告账户充值 ----------
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("交易记录"));
  b?.click();
});
await sleep(1800);
const portalTxn = await page.evaluate(() => {
  const heads = [...document.querySelectorAll("table thead th")].map((x) => x.textContent.trim());
  const rows = [...document.querySelectorAll("table tbody tr")].filter((r) => !r.querySelector(".empty"));
  return {
    heads,
    rows: rows.length,
    firstNo: (rows[0]?.children[0]?.textContent || "").trim(),
    firstStatus: (rows[0]?.querySelector(".badge")?.textContent || "").trim(),
    text: document.body.innerText,
    filters: document.querySelectorAll(".pw-filters select").length,
    dates: document.querySelectorAll(".pw-filters input[type=date]").length,
  };
});
check(
  "门户交易记录列与原型一致",
  ["交易编号", "交易类型", "账户ID", "交易金额", "状态", "反馈信息", "操作人", "操作时间"].every(
    (h) => portalTxn.heads.includes(h)
  ),
  portalTxn.heads.join("/")
);
check(
  "门户交易记录有本客户的 ADTR 流水",
  portalTxn.rows > 0 && portalTxn.firstNo.startsWith("ADTR"),
  JSON.stringify(portalTxn).slice(0, 220)
);
check(
  "门户交易记录含日期/类型/状态筛选",
  portalTxn.dates === 2 && portalTxn.filters >= 2,
  JSON.stringify({ dates: portalTxn.dates, filters: portalTxn.filters })
);
check(
  "门户交易记录不泄露渠道与返点",
  !portalTxn.text.includes("Yatop") && !portalTxn.text.includes("返点") && !portalTxn.text.includes("代理商"),
  ""
);

// 按当前行状态筛选：筛完所有行的状态必须一致
await page.evaluate((label) => {
  const sel = [...document.querySelectorAll(".pw-filters select")].find((s) =>
    [...s.options].some((o) => (o.textContent || "").trim() === label)
  );
  if (!sel) return;
  const opt = [...sel.options].find((o) => (o.textContent || "").trim() === label);
  sel.value = opt?.value ?? "";
  sel.dispatchEvent(new Event("change", { bubbles: true }));
}, portalTxn.firstStatus);
await sleep(200);
await page.evaluate(() => {
  [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "查询")?.click();
});
await sleep(1500);
const portalFiltered = await page.evaluate(() => {
  const rows = [...document.querySelectorAll("table tbody tr")].filter((r) => !r.querySelector(".empty"));
  return rows.map((r) => (r.querySelector(".badge")?.textContent || "").trim());
});
check(
  "门户交易状态可筛选",
  portalFiltered.length > 0 && portalFiltered.every((x) => x === portalTxn.firstStatus),
  JSON.stringify(portalFiltered)
);
await page.evaluate(() => {
  [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "重置")?.click();
});
await sleep(1000);
await page.screenshot({ path: `${SHOT}/portal-transactions.png` });

// ---------- 门户问题工单：列表可展开看详情 ----------
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("问题工单"));
  b?.click();
});
await sleep(1500);
t = await texts(page);
check("问题工单页可进入", t.includes("提交问题") && t.includes("我的工单"), "");
check(
  "工单列表带操作列",
  await page.evaluate(() => {
    const heads = [...document.querySelectorAll("table thead th")].map((x) => x.textContent.trim());
    return heads.includes("处理说明") && heads.includes("操作");
  }),
  ""
);
const ticketOpened = await page.evaluate(() => {
  const row = [...document.querySelectorAll("table tbody tr")].find((r) =>
    [...r.querySelectorAll("button")].some((b) => b.textContent.trim() === "查看")
  );
  if (!row) return false;
  [...row.querySelectorAll("button")].find((b) => b.textContent.trim() === "查看").click();
  return true;
});
await sleep(1200);
const ticketDetail = await page.evaluate(() => {
  const row = document.querySelector("tr.ticket-detail-row");
  if (!row) return null;
  const text = row.innerText;
  return {
    hasDesc: text.includes("问题描述"),
    hasReply: text.includes("处理说明"),
    hasTime: text.includes("提交时间"),
    text: text.replace(/\n+/g, "/").slice(0, 120),
  };
});
check("工单可点开", ticketOpened, "");
check(
  "工单详情含问题描述与处理说明",
  !!ticketDetail && ticketDetail.hasDesc && ticketDetail.hasReply && ticketDetail.hasTime,
  JSON.stringify(ticketDetail)
);
await page.screenshot({ path: `${SHOT}/portal-tickets.png` });

// ---------- 门户开户申请：列表列 / 两栏弹层 / 必填拦截 ----------
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("开户申请"));
  b?.click();
});
await sleep(1500);
t = await texts(page);
check(
  "开户申请页可进入",
  t.includes("开户主体") && t.includes("申请单编号") && t.includes("申请状态"),
  ""
);
const openCols = await page.evaluate(() =>
  [...document.querySelectorAll(".portal-open-table thead th")].map((x) => x.textContent.trim())
);
check(
  "开户申请表列与原型一致",
  [
    "申请单编号", "开户主体", "开户媒体", "申请开户数", "申请状态",
    "驳回原因", "反馈信息", "申请人", "申请时间", "操作",
  ].every((h) => openCols.includes(h)),
  openCols.join("/")
);
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".filter-actions button")].find((x) =>
    x.textContent.includes("开户申请")
  );
  b?.click();
});
await sleep(700);
const applyUi = await page.evaluate(() => {
  const m = document.querySelector(".open-apply-modal");
  const cols = document.querySelector(".open-apply-cols");
  if (!m || !cols) return null;
  return {
    width: Math.round(m.getBoundingClientRect().width),
    cols: getComputedStyle(cols).gridTemplateColumns.split(" ").length,
    text: m.innerText,
  };
});
check(
  "开户弹层是两栏且够宽",
  !!applyUi && applyUi.width >= 900 && applyUi.cols === 2,
  JSON.stringify({ w: applyUi?.width, cols: applyUi?.cols })
);
check(
  "开户弹层含企业信息与账户信息",
  !!applyUi &&
    ["企业信息", "账户信息", "营业执照/BR", "投放国家", "账户行业", "绑定BCID", "产品链接"].every(
      (k) => applyUi.text.includes(k)
    ),
  ""
);
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".open-apply-modal .modal-foot button")].find((x) =>
    x.textContent.includes("提交")
  );
  b?.click();
});
await sleep(500);
t = await texts(page);
check("开户弹层未上传执照时拦截提交", t.includes("请上传或选择营业执照/BR"), "");
await page.screenshot({ path: `${SHOT}/portal-open.png` });
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".open-apply-modal .modal-foot button")].find((x) =>
    x.textContent.includes("取消")
  );
  b?.click();
});
await sleep(300);

// ---------- 直接输入门户地址必须出现登录页 ----------
// 回归：以前客户登录一次，之后谁在这个浏览器里输 /portal 都直接进站（共用电脑会串号）。
await page.reload({ waitUntil: "networkidle2" });
await sleep(900);
t = await texts(page);
check("门户页内刷新保持登录", t.includes("我的钱包") || t.includes("总可用"), t.slice(0, 60));

await page.goto(`${BASE}/portal`, { waitUntil: "networkidle2" });
await sleep(900);
t = await texts(page);
check("再次输入 /portal 回到登录页", t.includes("海外客户端系统") && !t.includes("我的钱包"), t.slice(0, 60));
check(
  "门户会话不写 localStorage",
  await page.evaluate(() => !localStorage.getItem("adsys.portal")),
  ""
);
await page.screenshot({ path: `${SHOT}/portal-login-again.png` });

// ---------- 销售角色（权限差异）----------
await page.evaluate(() => localStorage.clear());
await loginAs(page, "sales01", "123456", false);
await page.evaluate(() => {
  const b = [...document.querySelectorAll(".nav-item")].find((x) => x.textContent.includes("资金流水"));
  b?.click();
});
await sleep(1600);
const hasExport = await page.evaluate(() =>
  [...document.querySelectorAll("button")].some((b) => b.textContent.includes("导出 Excel"))
);
check("销售看不到导出按钮", !hasExport, "");
const hasReverse = await page.evaluate(() =>
  [...document.querySelectorAll("tbody button")].some((b) => b.textContent.includes("冲正"))
);
check("销售看不到冲正按钮", !hasReverse, "");
await page.screenshot({ path: `${SHOT}/sales-ledger.png` });

console.log("\n--- 浏览器控制台错误 ---");
if (errors.length === 0) console.log("无");
else errors.slice(0, 8).forEach((e) => console.log(" !", e.slice(0, 200)));

const failed = results.filter((r) => !r.ok);
console.log(`\n通过 ${results.length - failed.length} / ${results.length}`);
fs.writeFileSync(`${SHOT}/result.json`, JSON.stringify({ results, errors }, null, 2));

await browser.close();
process.exit(failed.length || errors.length ? 1 : 0);
