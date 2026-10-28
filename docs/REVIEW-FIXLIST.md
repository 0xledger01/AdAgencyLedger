# 前端闭环评审 · 待修清单（给开发）

| 项 | 内容 |
|---|---|
| 版本 | v1.1（v1.0 评审 → 本轮已修，见各条 ✅ 标记） |
| 日期 | 2026-10-28 |
| 评审范围 | `frontend/src` 全部 9 个页面 + `backend/app/api` 全部 7 个路由 + `services/` 核心 6 个模块 |
| 评审基准 | `docs/CONTRACT-P3.md`（冻结契约）、`AGENTS.md`（铁律）、`DESIGN.md`（设计决策） |
| 评审结论 | **账本内核是可信的**（`ledger.py` / `guards.py` / 幂等 / 冲正红字 / 触发器双保险，实现质量高）。问题集中在**能力与入口没接上**：3 条资金守卫在 UI 层可绕过，8 个后端接口前端零入口，权限矩阵与按钮判定错位。 |
| 怎么用 | 按 P0 → P1 → P2 → P3 顺序修。每条给了 `文件:行`、现状、要求、验收。改完在 `smoke_p3.py` 补断言（附录 A），跑 `pytest` + `smoke_p2/p3` + `e2e-check.mjs` 全绿再提。 |

## 修复状态（2026-10-28 收口）

| 项 | 状态 | 落地位置 |
|---|---|---|
| F-01 冲正双签 | ✅ | `desk.py` `ReverseIn.second_approver` + `AuditLog`；`DeskLedger` 冲正弹窗 |
| F-02 执行权限 | ✅ | `rbac.py` `RECHARGE_EXEC{OPS,ADMIN}`；`recharge.audit_exec`；`DeskRecharge.canExec` |
| F-03 权限基座 | ✅ | `rbac.perms_for()` → `/auth/me` + login 返回 `perms`；`api.ts hasPerm`；`App.tsx` 按 perm 过滤菜单 + 启动校验 |
| F-04 大额二次确认 | ✅ | 新增 `services/limits.py`；`confirm` 接口返回 `need_second_confirm`；前端二次确认后带 `force` |
| F-05 门户可用额 | ✅ | `portal.py my_wallet` 增 `frozen`，`total_available=(balance-frozen)+ad` |
| F-06 门户凭证 | ✅ 由 P3.5 窗口完成 | `services/storage.py` / `api/files.py` / `confirm_topup` 强制校验 |
| F-07 八个断头接口 | ✅ 8/8 | 见下节逐条 |
| F-08 三个死胡同 | ✅ 2/3 | `PENDING_PAY` 可退回；重提入口已开；**批次号未做**（见遗留） |
| P2 十四条 | ✅ 11/14 | F-12 分页、F-08-3 批次号 未做 |
| P3 五条 | ✅ 5/5 | 见下节 |

**验证结果**：`pytest 72 passed` ｜ `smoke_p2 60/60` ｜ `smoke_p3 74/74` ｜ `smoke_authz 20/20`（新增） ｜ `e2e 30/30` ｜ `tsc -b` 0 error。

---


---

## 0. 总览

| 级别 | 含义 | 条数 |
|---|---|---|
| **P0** | 资金可被单人点走 / 功能物理不可用，上线前必修 | 6 |
| **P1** | 流程断头，真实客户跑不通完整链路 | 2 组（8 接口 + 3 死胡同） |
| **P2** | 前端页面硬伤，影响自查与协作效率 | 14 |
| **P3** | 后端越权与口径不一，数据看了会误判 | 5 |

**建议修复顺序**（有依赖关系，别并行乱改）：

```
F-01 流水冲正双签  ┐
F-02 加款执行权限  ├─ 三个都是"钱"的口子，先封
F-06 门户凭证必填  ┘
        ↓
F-03 权限下发改造（GET /auth/me 返回 perms）── 这是后面所有按钮判定的基座
        ↓
F-04 大额二次确认 / F-05 门户可用额
        ↓
F-07 补 8 个断头接口（其中"入驻重提""开户新建"决定新客户能否跑通）
        ↓
F-08 三个状态死胡同 → P2 页面整理 → P3 后端口径
```

> ⚠️ **F-03 要先做**：现在前端每个页面各自写 `canXxx = ["FINANCE","ADMIN"].includes(role)`，硬编码在 6 个文件里，且和后端 `rbac.MATRIX` 已经对不上。不把这层统一，后面每加一个按钮都要再错一次。

---

## 遗留（本轮明确不做，已知且有意）

| 项 | 为什么没做 | 建议时机 |
|---|---|---|
| **F-08-3 消耗导入批次号** | 要动 `spend_daily` 加列 + `migrate.py`，而 P3.5 窗口正在改同一处 migration（`file_object` 表），同文件并发改会互相覆盖 | P3.5 合入后单独一轮，2h。**历史数据以后补不回来，越早越好** |
| **F-12 列表分页** | 5 个页面 + 后端 `sort` 参数，属独立改造，不影响资金正确性 | 下一轮。当前 `limit=200` 上限对财务查账是硬伤，建议优先于 P4 |
| F-07-3 调账 / F-07-4 单据详情 / F-07-5 消耗明细 | ✅ 已做 | — |



## P0 · 资金与权限（必修，本轮全部 ✅）

### F-01 流水页冲正绕过双签 ✅

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/api/desk.py:299`（接口）、`frontend/src/pages/DeskLedger.tsx:173`（入口） |
| 现状 | 充值单冲正（`desk_topup.py:298`）强制 `second_approver` 必填且 ≠ 操作人；但流水页冲正 `POST /api/desk/client-ledger/{tx_id}/reverse` 只收 `reason`。前端就一个 `window.prompt` 输原因，**同一个财务账号能单人把任意一笔流水冲掉**。 |
| 为什么是 P0 | 充值单那条链路守住了，流水页开了后门，等于没守。`AGENTS.md` 铁律 4「钱只能由人点出去」+ PRD「调账与冲正需两人确认」。 |
| 要求 | ① `ReverseIn` 增加 `second_approver: str \| None`，服务端校验非空且 `!= user.display_name`，否则 400 `STATE_ERROR`；② 冲正原因同时写 `AuditLog`（含第二签人姓名）；③ 前端 `doReverse` 换成 modal（同 `DeskTopups.tsx:604` 的冲正弹窗），两个必填框 + 自签互斥校验；④ 或者更彻底：**下线流水页冲正入口**，冲正一律从单据（充值单 / 调账单）发起 —— 这个更符合"单据是事实、流水是结果"的模型，建议选 ④。 |
| 验收 | 不带 `second_approver` → 400；`second_approver == 操作人` → 400；正常 → 200 且原流水 `reversed=true`，再冲同一笔 → 400 |

### F-02 加款执行用的是"查看"权限

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/api/desk_recharge.py:115`、`frontend/src/pages/DeskRecharge.tsx:439` |
| 现状 | `POST /recharges/{id}/exec` 挂的是 `require_perm(Perm.RECHARGE_VIEW)`。`RECHARGE_VIEW` 包含 FINANCE / OPS / ADMIN / **CLIENT** → 只要能看列表就能把冻结的钱打出去。前端「执行」按钮也没做任何角色判断，所有能进这个页面的人都能点。 |
| 为什么是 P0 | 这是全系统唯一"真的出账"的动作，权限等于看报表。 |
| 要求 | ① `rbac.py` 新增 `Perm.RECHARGE_EXEC = "recharge.exec"`，矩阵 = `{OPS, ADMIN}`；② exec 改挂该权限；③ 前端 `canExec = hasPerm("recharge.exec")`，不满足则按钮不渲染（hover 提示"仅运营可执行"）；④ 顺手把 `exec_recharge` 的审计补上：谁在什么时间、以哪个 outcome 执行的，写 `AuditLog`。 |
| 验收 | SALES / FINANCE / CLIENT 调 exec → 403；OPS → 200；前端用 `finance01` 登录看不到「执行」按钮 |
| ⚠️ 联动改动 | **现有测试把错误权限固化了**：`smoke_p3.py:450,493` 用 `T_FIN`（finance01）调 `exec` 并断言 200 成功。改权限后这两处必须换成 `T_OPS`，否则测试与实现互相矛盾。改之前先跑一遍确认现状，改完再跑必须全绿 |

### F-03 权限矩阵与前端按钮全面错位

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/services/rbac.py:54-90`、`backend/app/api/auth.py:77`（`/me` 已存在但前端没调用）、`frontend/src/App.tsx:15`、`DeskOpen.tsx:98-99`、`DeskTopups.tsx:105`、`DeskLedger.tsx:82` |
| 现状 | 具体错位见下表。**用默认的 `sales01` 登录，8 个菜单里有 3 个点进去直接 403**（充值管理、开户管理、消耗导入）。 |
| 要求 | ① 后端 `GET /api/auth/me` 返回 `perms: string[]`（`MATRIX` 反查当前角色）；② 前端 `Session` 增加 `perms`，`api.ts` 提供 `hasPerm(p)`；③ `App.tsx` 的 `DESK_NAV` 每项加 `perm` 字段，渲染前过滤；④ 6 个页面里所有 `["FINANCE","ADMIN"].includes(role)` 全部替换成 `hasPerm(...)`，**删掉 `DeskOpen.tsx:99` 的 `canSubmit = true`**；⑤ 登录后首次进入调一次 `/me`，失败（401）直接清 session 回登录页，解决"token 过期后每个页面各自报错"的问题。 |

**当前错位明细（开发可直接对照改）：**

| 页面 / 按钮 | 前端判定（现状） | 后端要求 | 结果 |
|---|---|---|---|
| 开户管理 · 列表 | 所有人可见 | `OPEN_REQUEST_VIEW` = OPS/ADMIN | SALES/FINANCE/RISK 进来就 403 |
| 开户 · 审批/驳回 | `[RISK, FINANCE, ADMIN]` | `OPEN_REQUEST_APPROVE` = OPS/ADMIN | **RISK/FINANCE 看到按钮但点了 403；OPS 有权反而看不到按钮** |
| 开户 · 提交/批量提交 | `canSubmit = true` | `OPEN_REQUEST_SUBMIT` = OPS/ADMIN | 所有人可见，非 OPS 点了 403 |
| 充值管理 · 列表 | 所有人可见 | `TOPUP_VIEW` = FINANCE/OPS/ADMIN | SALES 进来 403 |
| 消耗导入 · 列表/导入 | 所有人可见 | `SPEND_IMPORT` = FINANCE/OPS/ADMIN | SALES 进来 403 |
| 资金总览 | 所有人可见 | `wallets_overview` 无权限校验（见 F-23） | 销售能看到公司实持资金池；且该卡跨币种求和（见 F-09/F-27） |
| 加款 · 执行 | 无判断 | `RECHARGE_VIEW`（见 F-02） | 全都能执行 |
| 加款 · 核查/重试/撤销 | `[OPS, ADMIN]` | `RECHARGE_RESOLVE` = OPS/ADMIN | ✅ 这条是对的，照它改其余 |

**验收**：`sales01` 登录后侧栏只剩「资金总览 / 客户管理 / 资金流水 / 我的档案」；逐个角色点遍 8 个菜单无 403；`finance01` 在开户页看不到审批按钮；`ops01` 在开户页**能看到**审批按钮且能审批成功。

### F-04 大额二次确认是空配置

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/config.py:25`（`large_amount_threshold_usd = 5000.0`）、`services/topup.py:60` `confirm_topup` |
| 现状 | 全仓库只有 `config.py` 定义了阈值，**0 处引用**。`DESIGN.md` §8 写着"大额二次确认 5000 USD"，实际确认到账没有任何二次确认，6000 也是单击就入账。 |
| 要求 | ① `confirm_topup` 增加 `force: bool = False` 参数：金额（按汇率折 USD，无汇率则按配置币种）≥ 阈值且 `force=false` → 返回 `{"need_second_confirm": true, "threshold": ..., "amount_usd": ...}`，**不入账**；② 前端收到后弹二次确认，确认后重发带 `force=true`；③ 二次确认写入 `AuditLog`（操作人 + 金额 + 时间）；④ 阈值改成从 `SystemSetting` 读，允许后台调（先做读取，页面 P4 再说）。 |
| 验收 | 6000 USD 确认到账 → 第一次返回 `need_second_confirm`，余额不变；带 `force=true` 重发 → 入账，`AuditLog` 有记录；4999 → 直接入账 |

### F-05 门户"总可用"把冻结金额算成可用

| 项 | 内容 |
|---|---|
| 位置 | `backend/app/api/portal.py:187,191,201`、`frontend/src/pages/PortalWallet.tsx:103-111` |
| 现状 | `my_wallet` 用 `w.balance` 累加（未减 `frozen`），`total_available = wallet + ad_accounts`。内部后台（`DeskClients` 钱包 Tab）显示的是 `available = balance - frozen`。**加款单冻结期间，客户看到的可用余额比真实高**，会以为自己还能下单。 |
| 要求 | ① 返回增加 `frozen`；② `total_available = (balance - frozen) + ad_accounts`；③ 前端 KPI 拆成三行：`可用 / 冻结中 / 广告账户`，冻结 > 0 时加一行说明"有加款单正在执行，金额已锁定"；④ 顺带把 `wallet_balance` 改名不要叫"钱包余额"而有歧义 —— 直接用"可用余额"。 |
| 验收 | 发起一笔 500 加款（冻结）→ `/portal/wallet` 的 `total_available` 比操作前少 500，`frozen=500`；撤销后恢复 |

### F-06 门户充值凭证是硬编码假路径

| 项 | 内容 |
|---|---|
| 位置 | `frontend/src/pages/PortalWallet.tsx:72`、`backend/app/api/portal.py:290`（`TopUpCreateIn`）、`services/topup.py:51` |
| 现状 | 前端提交时写死 `voucher: "portal/upload.png"`、`payer: session.display_name`、`remark: "门户提交"`。**客户没有上传任何东西**，`voucher` 是个不存在的路径。而 `create_topup` 因为"有 voucher"把状态判成 `PENDING_CONFIRM` → 财务点「查看」得到 404，却仍然可以「确认到账」。等于凭证核对这一步是完全空的。`DESIGN.md:133` 自己也承认这是"当前最大风控缺口"。 |
| 要求 | ① 门户充值改成两步：**第一步给收款信息**（户名 / 账号 / 汇款附言 = 客户编号 `biz_no`，从 `/portal/profile` 取）；**第二步客户回填**：付款方名称（必填）、银行流水号或转账截图路径（必填）、金额、币种、备注；② 后端 `TopUpCreateIn` 增加 `bank_serial_no: str \| None`，**门户提交时 `payer` 与（`voucher` 或 `bank_serial_no`）至少一个必填**，否则 400，不允许静默成功；③ `voucher` 传空就落 `PENDING_PAY`（部分支付），不要用假路径骗成 `PENDING_CONFIRM`；④ 财务确认页必须能打开凭证（文件上传接口属 P4，**在此之前至少校验 `voucher` 是 `http` 开头或已存在的相对路径**，点开必须是 200）。 |
| 验收 | 门户提交不带付款方/流水号 → 400；带齐 → 200 且 `status=PENDING_CONFIRM`；财务点凭证能打开；提交纯金额无凭证 → `PENDING_PAY`（并可按 F-16 退回） |

---

## P1 · 流程断头

### F-07 8 个后端接口前端零入口

后端写完了、契约里有、前端没做。逐个补：

| # | 接口 | 断在哪 | 要求 |
|---|---|---|---|
| 1 | `POST /api/portal/onboarding`<br>`POST /api/portal/onboarding/resubmit` | **门户没有入驻申请页**。客户被驳回后（`REJECTED`）在档案页只能看到驳回原因，**没有"修改并重提"的按钮 → 永久卡死**，必须打电话让销售手工改库 | 门户「入驻进度」页在被驳回时展示可编辑表单（`upsert_application` 的字段全集），提交走 `/resubmit`；`PENDING_*` 态只读并提示"审核中不可修改" |
| 2 | `POST /api/desk/open-requests` | 开户单**无法新建**，只能靠 `seed.py` 灌的演示数据。运营想给客户开户无路可走 | `DeskOpen` 加「新建开户单」按钮（`hasPerm("open_request.submit")`），表单：客户下拉 + 平台 + BM ID + 时区 + 币种 + 日限额 |
| 3 | `POST /api/desk/clients/{id}/adjust` | 调账**无任何入口**（契约 D3 范围内） | 客户详情·钱包 Tab 加「调账」按钮：方向 + 金额 + 原因 + `request_no`（前端 `crypto.randomUUID()`）+ 第二签人，全部必填 |
| 4 | `GET /api/desk/topups/{id}` | 充值单**看不到关联流水**，财务确认到账时无法核对"这笔钱到底入了几条、有没有被冲正过" | 充值列表行加「详情」抽屉：单据信息 + 关联流水（含红字冲正）+ 操作痕迹 |
| 5 | `GET /api/portal/accounts/{id}/spend` | 客户看不到消耗明细，**无法自证账户余额为什么少了** → 全部转成客诉 | 门户「我的广告账户」每行加「消耗趋势」，近 30 天曲线 + 合计（数据后端已有） |
| 6 | `POST /api/portal/tickets` | 客户无工单入口，争议只能微信找销售 | 门户加「提交问题」：标题 + 分类 + 内容；顺手补 `GET /portal/tickets` 看自己的历史工单（现在连查询接口都没有，**建议一起补**） |
| 7 | `GET /api/auth/me` | 前端从不校验 token，也不拿权限 | 见 F-03 ④ |
| 8 | `POST /api/desk/clients/{id}/disable`<br>（**不存在，需新建**） | 客户停用无接口，`Perm.CLIENT_DISABLE` 定义了但没有任何路由用它 → 客户违约/欠款时无法停用 | 客户详情加「停用/启用」，需填原因，写 `AuditLog`；停用后该客户门户只读（`ensure_client_wallet` 已会拦资金操作） |

### F-08 三个状态死胡同

| # | 死胡同 | 位置 | 要求 |
|---|---|---|---|
| 1 | `PENDING_PAY` 单**永远退不掉也确认不了**？确认可以（`confirm_topup` 允许 `PENDING_PAY`），但**退回只放行 `PENDING_CONFIRM`**（`services/topup.py:103`）→ 门户提交的无凭证单一旦客户放弃，就永久挂在"待支付" | `topup.py:103` | 允许 `PENDING_PAY` 也退回（客户主动取消 / 超时未付）。或者加定时任务自动关单（P5），**在此之前必须有手动退回**。 |
| 2 | `REJECTED` 客户无重提入口 | 见 F-07 #1 | 同上 |
| 3 | 消耗导入**无批次号**，导错只能逐条处理，不能整批撤回 | `services/spend.py:96` `import_spend` | 落 `SpendDaily` 时写 `batch_no`（`IMP{yyyyMMddHHmmss}` + 操作人），导入报告返回批次号；页面加「按批次查看 / 撤回整批」（撤回 = 删除该批次 `SpendDaily` + 回滚 `AdAccount.balance`，**需 `hasPerm("spend.import")` + 二次确认 + AuditLog**）。这项涉及改表，不着急可以放 P2 之后，但**批次号字段建议这次就加**，否则历史数据以后补不回来。 |

---

## P2 · 前端页面硬伤

| # | 问题 | 位置 | 要求 |
|---|---|---|---|
| F-09 | **跨币种直接相加**：USD + CNY + HKD 求和成一个数（如"客户子钱包 128,000"），财务看了会误判 | `DeskOverview.tsx:61,70,78`（前端 `reduce` 求和）、`desk.py:336`（后端 `ad_accounts` 也是跨币种 SUM） | 后端按币种分组返回；前端每币种一行 KPI，**禁止跨币种求和**；确实要合计就明确标注"（未折算，仅供参考）"或按当日汇率折算并注明汇率来源 |
| F-10 | **无路由**：页面切换是内存 state，刷新回首页，**链接无法发给同事** | `App.tsx:45` `page` 是 `useState` | 接入 URL（`/desk/ledger?client_id=2`）。不引 react-router 也行，用 `window.history.pushState` + `popstate` 监听，`resolveKind()` 一起处理 |
| F-11 | 菜单不分角色，8 项全量渲染 | `App.tsx:15` | 见 F-03 ③ |
| F-12 | 全部列表 `limit:200` **无分页无排序**，财务要查第 201 笔只能改代码 | 5 个页面：`DeskTopups:152`、`DeskLedger:130`、`DeskRecharge:128`、`DeskOpen:113`、`DeskSpend:120` | 统一加分页（`offset` 后端已支持）+ 至少「时间」「金额」两列可排序（后端要补 `sort` 参数）。表尾"当前显示前 200 条"改成真实分页控件 |
| F-13 | **全量流水 tab 的客户下拉选了没反应** —— 该 tab 根本不传 `client_id` | `DeskLedger.tsx:243`（下拉常显）、`:124`（`tab === "client" ? clientId : undefined`） | 该 tab 隐藏客户下拉；或允许按客户筛选（后端 `global_ledger` 加 `client_id` 参数） |
| F-14 | 核销期间**靠前端猜**：没选日期就默认当月 | `DeskLedger.tsx:155` | 核销弹窗显式选期间（`YYYY-MM`）+ 备注；后端校验所选流水日期落在期间内，否则 400 |
| F-15 | 状态筛选下拉**来自"已学到的标签"**（把列表里见过的 `status_label` 攒成 options）→ 列表为空时下拉是空的，筛不出任何状态 | `DeskTopups:172`、`DeskOpen:136`、`DeskRecharge:195` | 后端加 `GET /api/desk/enums` 一次返回所有状态枚举 + 中文（`TOPUP_STATUS_LABEL` 等已有），前端固定拉一次。**这也符合契约 §2 的精神：中文只能来自后端** |
| F-16 | 驳回/冲正用 `window.prompt`，与 modal 风格不一致、无法校验、移动端体验差 | `DeskClients.tsx:95`、`DeskLedger.tsx:173` | 统一换成 modal（照 `DeskTopups` 的驳回/冲正弹窗写） |
| F-17 | 账户消耗视图**不能导出** | `DeskLedger.tsx:225`（`tab !== "spend"` 才显示导出） | 放开 spend tab 导出，后端 `export_ledger` 加 `view=spend` 分支 |
| F-18 | 客户往来 tab **自动选 id 最小的客户**，你不知道自己在看谁 | `DeskLedger.tsx:95`、`:328` | 默认留空 + 空态提示"请先选择客户"。现在 `sumRows.length===0` 的提示写的是"该客户没有流水"，没选客户时也是这句，语义错，会误导财务以为这个客户真的没流水 |
| F-19 | 登录页**明文列出全部测试账号**，并把 `admin/admin123` 预填在输入框 | `Login.tsx:11,57-64` | `import.meta.env.PROD` 时不渲染提示块、不预填；生产必须清空 |
| F-20 | 门户**硬编码状态中文映射**（违反契约 §2"前端不写状态映射表"），且和内部页两套 | `PortalWallet.tsx:265-281` | 后端 `my_topups` / `my_accounts` 补 `status_label`、`payment_method_label`，前端删映射只读 label |
| F-21 | Mock 能力**无环境开关**：真环境也会渲染「Mock 回执」「执行结果三态」 | `DeskOpen.tsx:381`、`DeskRecharge.tsx:561` | 后端下 `mock_enabled: bool`（跟 `settings.debug` 走），前端只在 true 时渲染 |
| F-22 | 消耗导入**只能粘贴文本，不能选文件**；异常行只标黄不处理 | `DeskSpend.tsx:193` | 加 `<input type="file" accept=".csv">` 读成文本塞进 textarea（复用现有解析）；异常行加「确认无误 / 忽略」+ 备注，落 `SpendDaily.anomaly_note`（需加列，走 `migrate.py` 的 `_ADD_COLUMNS`） |

> 说明：F-19 到 F-22 属于"该做但不阻塞"，如果排期紧，**F-09 / F-10 / F-12 / F-13 / F-15 优先**（这五条直接导致财务看错数、查不到数据）。

---

## P3 · 后端越权与口径

| # | 问题 | 位置 | 要求 |
|---|---|---|---|
| F-23 | **销售能看公司实持资金池**：`wallets_overview` 只挂 `DeskUser`，既无 `Perm` 也无 `scope_sales_owner` | `desk.py:327` | 挂 `Perm.WALLET_VIEW`（不含 SALES）或对 SALES 只返回 `client` 维度并过滤到自己名下客户 |
| F-24 | **销售能看全部客户消耗**：`account-ledger` 无 scope，而 `client-ledger` 有 | `desk.py:310` | 补 `scope_sales_owner`，与 `client-ledger`（`desk.py:229`）逻辑一致 |
| F-25 | **销售 KPI 数 ≠ 自己列表数**：`pending_count(db)` 不分 owner，KPI 卡显示全公司待审，列表只有自己名下 | `services/clients.py:74`、`desk.py:82` | `pending_count` 增加 `sales_owner` 参数，`list_clients` 传同一个 owner |
| F-26 | **账本自检既无鉴权也无入口**：`/api/dev/wallets/{id}/verify` 未校验 debug（同文件 `reset-demo` 校验了），总览页只有一句说明文字 | `main.py:76`、`DeskOverview.tsx:94` | ① 加 `if not settings.debug: raise`；② 总览页加「全量自检」按钮，遍历所有钱包跑 `verify_wallet_chain`，展示"通过 N 个 / 断号 M 个"，结果落 `AuditLog`。这是账本系统唯一的体检入口，不该只是段说明文字 |
| F-27 | **广告账户总额跨币种 SUM** | `desk.py:336-338` | 按 `currency` 分组返回（与 F-09 一起改） |

---

## 附录 A · 补进 `smoke_p3.py` 的断言

现在 `smoke_p3.py` 已有 **76 个断言**（覆盖契约 §8 要求的 15 条 + 一批额外边界），正向链路的测试做得不错。**缺的正好是越权与跨端一致性**——这也是本清单 P0 问题能活到现在的原因：测试没测越权，实现就没人复查。请补：

```text
A1  非 OPS 调 approve → 403；OPS 调 → 200          （现在正好相反，见 F-03）
A2  流水冲正不带 second_approver → 400              （F-01）
A3  流水冲正 second_approver == 操作人 → 400        （F-01）
A4  SALES / FINANCE 调 exec → 403                   （F-02）
A5  6000 USD 确认到账 → 200 且 need_second_confirm  （F-04）
A6  加款冻结后 /portal/wallet 的 total_available 下降（F-05）
A7  门户提交充值不带 payer 与凭证 → 400             （F-06）
A8  PENDING_PAY 单可退回 → RETURNED                 （F-08）
A9  SALES 调 /desk/wallets/overview → 403           （F-23）
A10 SALES 调 /desk/account-ledger → 只含自己客户     （F-24）
A11 SALES 的 pending 三个 KPI 之和 == 列表 total     （F-25）
A12 同一批消耗导入第二次全部 skipped，reason 含"已存在"（契约 §6.1，确认已实现）
```

## 附录 B · 前端权限改造参考写法

```ts
// api.ts
export interface Session {
  // ...现有字段
  perms: string[];            // 新增，来自 GET /auth/me
}
export function hasPerm(s: Session | null, p: string): boolean {
  return !!s?.perms?.includes(p);
}

// App.tsx
const DESK_NAV = [
  { key: "overview", label: "资金总览", perm: null },              // 全员
  { key: "clients",  label: "客户管理", perm: "client.list" },
  { key: "topups",   label: "充值管理", perm: "topup.view" },
  { key: "open",     label: "开户管理", perm: "open_request.view" },
  { key: "recharge", label: "加款任务", perm: "recharge.view" },
  { key: "spend",    label: "消耗导入", perm: "spend.import" },
  { key: "ledger",   label: "资金流水", perm: "ledger.view" },
  { key: "profile",  label: "我的档案", perm: null },
];
const nav = DESK_NAV.filter(n => !n.perm || hasPerm(session, n.perm));
```

**原则**：前端隐藏只是体验，**服务端校验才是唯一有效的校验**（`rbac.py` 文件头原话）。所以按钮判定和 `require_perm` 必须取自同一份矩阵，不要各写一份。

## 附录 C · 不在本次范围（别顺手做）

- 文件上传接口（凭证/执照）→ P4。F-06 只做到"路径校验 + 必填"，真上传要等存储层
- 对账 D8 / 报表 D9 / 返点 D7 / 系统设置 D10 → P4
- 对账单 PDF / 定时任务 J1–J6 / 通知告警 → P5
- RPA 执行端 R1–R8 → 现在只有 `rpa_mock.py`，F-21 加开关即可，不要动协议
- `wallet_hold` 表（per-order 冻结关联）→ `DESIGN.md:148` 已记录的技术债，P4。**在此之前 F-02 的权限收紧更关键**，因为 `retry` 用 `frozen >= amount` 判断可能把 A 单冻结算到 B 单头上

---

## 自测命令（改完必跑，全绿才算完）

```bash
.venv/Scripts/python.exe -m pytest -q                    # 单元测试（账本铁律）
.venv/Scripts/python.exe backend/tests/smoke_p2.py       # P2 接口验收
.venv/Scripts/python.exe backend/tests/smoke_p3.py       # P3 接口验收
.venv/Scripts/python.exe backend/tests/smoke_authz.py    # 越权与口径（附录 A，新增）
cd frontend && npx tsc -b                                # 类型检查
cd frontend && node e2e-check.mjs                        # 前端端到端
```

> 需要先起后端（`--port 8012`）+ 前端 dev server；`smoke_*` 可用 `SMOKE_BASE` 指向其他端口。

> 新增账本相关逻辑时，**先补测试再改实现**（`AGENTS.md`）。

---

## 附录 D · 本轮新增的接口与页面（交接用）

| 类型 | 名称 | 用途 |
|---|---|---|
| 接口 | `GET /api/desk/enums` | 状态/方式/币种枚举字典，前端不再自攒标签（F-15） |
| 接口 | `POST /api/desk/clients/{id}/disable` | 停用/启用客户，`Perm.CLIENT_DISABLE` 首次有落地（F-07#8） |
| 接口 | `POST /api/desk/wallets/verify-all` | 全量账本流水链自检 + 审计留痕（F-26） |
| 接口 | `GET /api/portal/tickets` | 客户查自己的工单（F-07#6） |
| 接口 | `GET /api/desk/client-ledger/export?view=spend` | 账户消耗导出 csv/xlsx（F-17） |
| 权限 | `Perm.RECHARGE_EXEC`（OPS/ADMIN） | 把"执行加款"从"看列表"里拆出来（F-02） |
| 字段 | `LoginOut.perms[]` | 权限码随登录/`/me` 下发（F-03） |
| 字段 | `portal/wallet.items[].frozen` | 门户显示冻结额（F-05） |
| 页面 | `pages/PortalProfile.tsx` | 门户入驻进度 + 被驳回后修改重提（F-07#1） |
| 页面 | `App.tsx` `PortalTickets` | 门户提交/查看工单（F-07#6） |
| 脚本 | `backend/tests/smoke_authz.py` | 越权与口径断言 20 条，补上原来完全空白的一层 |

---

## 附录 E · 总览页调整（2026-10-28 业务方要求）

### E1 去掉「资金总金额」

| 项 | 内容 |
|---|---|
| 改动 | 前端删除该卡片；后端 `wallets_overview` 不再返回 `grand_total` |
| 原因 | 「广告账户余额（媒体侧可用预算）」+「平台钱包（我方对客户的负债）」相加在业务上不成立——一个是资产一个是负债，加起来既不是公司实力也不是客户可用，看了只会误判。要总额就按币种分别看两侧 |
| 断言 | `smoke_authz` A14：`grand_total` 不得再出现在响应里 |

### E2 新增「广告账户充值流水」区块（位于「平台钱包 · 按客户」下方）

数据源 `RechargeOrder`（客户钱包 → 广告账户的每一笔加款），五维筛选：

| 维度 | 参数 | 说明 |
|---|---|---|
| 一级代理商 | `agent` | 模糊匹配 `AdAccount.channel`（该字段存的就是 `AgentChannel.name`），输入框带 datalist 可选 |
| 客户 | `client_id` | 下拉，按 client_id 去重 |
| 广告账户 ID | `account_keyword` | 模糊匹配媒体侧 `account_id` 字符串 |
| 交易类型 | `txn_type` | 粗分组：`SUCCESS` 充值成功 / `PENDING` 处理中 / `FAILED` 执行失败 / `CANCELLED` 已撤销 |
| 状态 | `status` | 单据精确状态（待执行/执行中/审批中/成功/已拒绝/待核查/已撤销） |

**「交易类型」与「状态」的关系**：交易类型是运营筛账的粗分组，状态是单据的精确状态，先粗后细，两者不重复。
`TO_VERIFY`（结果未知）归入「处理中」，因为它还没动账、尚未完结 —— 这点在页面脚注里写明了。

实现要点：
- 粗分组映射集中在 `services/recharge.py` 的 `TXN_TYPE_STATUS` / `TXN_TYPE_LABEL`，**前端不写映射**，标签由后端下发
- 枚举经 `GET /desk/enums` 下发（新增 `recharge_txn_type` 与 `agent_channel`），避免"下拉只有一项"的空壳
- 权限：整块由 `hasPerm(session, "recharge.view")` 控制，销售看不到
- 竞态：沿用 `seqRef` 递增序号丢弃过期响应
- 上限 100 条，超过时脚注提示"请用筛选收窄范围"

### E3 本轮新增/变更的断言

| 断言 | 抓什么 |
|---|---|
| `smoke_authz` A14 | 总览不再返回 `grand_total` |
| `smoke_authz` A15 ×5 | 交易类型枚举 4 项、代理商枚举非空、列表含 `agent`/`txn_type_label`、`txn_type=PENDING` 只回处理中、`agent=山恒` 只回该代理商、非法值 422 |
| `e2e` 「总览不再显示资金总金额」 | 卡片确实删了 |
| `e2e` 「充值流水三个下拉都有真实选项」 | **选项数下限断言**：交易类型 ≥5、状态 ≥8。只查文本存在会漏掉"接口没下发枚举、下拉只剩『全部』"这种假绿 |

> 这条断言是踩坑后补的：第一次跑 e2e 时 8012 上还是旧进程，`recharge_txn_type` 没下发，
> 下拉只剩「全部交易类型」一项，但"文本存在"的断言照样通过。现在按**选项数量**断言，才抓得住。



---

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。
