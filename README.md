# 海外广告代理业务系统

二级广告代理的内部业务系统 + 客户自助门户。PRD v1.2。

当前进度：**P1 账本内核 + P2 认证/权限/客户审核/资金流水三视图 + 前端页面**。

## 一键启动

双击 `start.bat`，会开两个窗口（后端 + 前端），并自动打开浏览器。

**两个地址，各走各的：**

| 入口 | 地址 | 账号 |
|---|---|---|
| 内部后台 | http://127.0.0.1:5173 | 员工 |
| 客户门户 | http://127.0.0.1:5173/portal | 客户 |
| 接口文档 | http://127.0.0.1:8012/docs | — |

内部员工和客户拿到的不是同一个 URL，登录页也不互通：员工走后台入口、客户走门户入口，交叉登录后端直接 403。

### 测试账号

| 端 | 账号 | 密码 | 能做什么 |
|---|---|---|---|
| 内部后台 | `admin` | `admin123` | 全部 |
| 内部后台 | `sales01` | `123456` | 只看自己名下客户，做初审 |
| 内部后台 | `risk01` | `123456` | 风控审核 |
| 内部后台 | `finance01` | `123456` | 定额度、看全部流水、导出、核销、冲正 |
| 内部后台 | `ops01` | `123456` | 只读为主 |
| 客户门户 | `client1` / `client2` / `client3` | `123456` | 只看自己的数据 |

### 新增客户 → 客户登录门户

后台「客户管理 → 添加客户」填**登录邮箱**（必填），「初始密码」留空即用系统默认密码
（默认 `123456`，可用环境变量 `CLIENT_DEFAULT_PASSWORD` 覆盖）。
创建成功的弹窗会给出登录地址 `/portal`、登录邮箱和初始密码，直接交给客户即可登录。

- 登录时邮箱**不区分大小写**，首尾空格自动忽略；
- 客户忘记密码：客户详情里「重置密码」，留空就重置回默认密码；
- 以上三种账号密码都不落明文（bcrypt），只回显给操作人一次。

### 建议的验证路线

1. 打开 http://127.0.0.1:5173，用 `admin` 登录 → 「客户管理」→ 找状态为「待审核」的客户（演示客户的公司名已统一脱敏为「某某公司」，按状态和 CRM 编号区分）
2. 点「查看」→ 点「初审通过」→ 换成 `risk01` 登录继续 → 再用 `finance01` 定额度
3. 走完三级变成「已开通」，详情页能看到 3 条审核记录
4. 去「资金流水」→ 切三个视图：客户往来（期初/期末）、全量流水、账户消耗
5. 另开一个浏览器标签访问 http://127.0.0.1:5173/portal，用 `client1` 登录 → 验证看不到返点、看不到别人的数据、侧栏没有内部菜单

## 手工命令

```bash
# 装依赖（首次）
.venv/Scripts/python.exe -m pip install -r requirements.txt
cd frontend && npm install

# 单元测试（账本铁律）
.venv/Scripts/python.exe -m pytest -v

# 证照 OCR 抽取规则（不跑推理，秒回）
.venv/Scripts/python.exe -m pytest backend/tests/test_license_ocr.py -q

# 接口验收（需后端已启动）
.venv/Scripts/python.exe backend/tests/smoke_p2.py
.venv/Scripts/python.exe backend/tests/smoke_p3.py
.venv/Scripts/python.exe backend/tests/smoke_authz.py   # 越权与口径（每次收口必跑）

# 前端端到端（需前后端都启动）
cd frontend && node e2e-check.mjs
node p3-pages-check.mjs          # 充值 / 开户 / 账户 / 消耗报表四页渲染
node spend-report-check.mjs      # 消耗报表：KPI / 趋势 / 四个 Tab / 明细 / 导入弹窗

# 起后端
.venv/Scripts/python.exe -m uvicorn backend.app.main:app --reload --port 8012

# 起前端
cd frontend && npx vite
```

## 数据库

本地 SQLite 单文件 `data/app.db`，**零安装**。

- 首次启动自动建表 + 灌种子数据
- 表结构升级走 `migrate.py` 增量 ALTER，**不删库、不丢数据**
- 换 Postgres：改 `.env` 的 `DATABASE_URL` 一行，代码不动
- 重置演示客户审核状态：`POST /api/dev/reset-demo`（只清审核记录，不动流水）

> **部署提示**：前端是单页应用，`/portal` 不是真实目录。用 Nginx / 静态服务器时必须配 SPA fallback
> （`try_files $uri $uri/ /index.html;`），否则刷新 `/portal` 会 404。Vite dev / preview 已内置，本地不用管。

## 目录

```
backend/app/
├── config.py            配置（密钥、阈值）
├── db.py                引擎 / 会话 / SQLite PRAGMA / 流水禁改触发器
├── migrate.py           增量迁移（加列不删表）
├── models/              17 张表
│   ├── wallet.py        Wallet / WalletTransaction
│   ├── client.py        Client / AdAccount / ClientTopUp / SpendDaily / AgentChannel
│   ├── user.py          User / ClientReview / ClientApplication / LedgerReconcile
│   └── docs.py          OpenRequest / RechargeOrder / AgentTopUp / Rebate / AuditLog
├── services/
│   ├── ledger.py        ★ 账本内核（唯一余额写入口）
│   ├── guards.py        ★ 铁律守卫（ORM 事件拦直改）
│   ├── auth.py          bcrypt + JWT
│   ├── rbac.py          权限矩阵（PRD 3.2）
│   ├── clients.py       客户管理 + 三级审核状态机
│   ├── ledger_views.py  D3.1 三视图 + 恒等式 + 导出 + 核销 + 冲正
│   ├── topup.py         充值单（确认到账才动账；门户/存量单兼容路径）
│   ├── txn_records.py   广告账户交易记录（只读投影：后台全量 + 门户口径白名单）
│   ├── fund_app.py      线下汇款申请单（待审核 → 审核通过入账/入授信，驳回留痕）
│   ├── fx.py            USD/CNY 报价（央行中间价 + 缓存 + 漂移校验 + 单据快照）
│   ├── license_ocr.py   营业执照/BR 本地 OCR + 字段抽取（离线 onnx 模型）
│   └── errors.py        业务异常
├── api/
│   ├── deps.py          当前用户 / 权限依赖 / 后台与门户身份隔离
│   ├── auth.py          登录（后台 / 门户分离）
│   ├── desk.py          内部后台（整组强制非客户身份）
│   ├── portal.py        客户门户（client_id 只从登录态取）
│   └── portal_transactions.py 门户交易记录（只读，按登录客户隔离）
├── seed.py              种子数据（含 5 个员工 + 3 个客户账号）
└── main.py              FastAPI 入口 + 异常翻译 + 开发工具

frontend/
├── src/api.ts           fetch 封装 + 会话存取 + hasPerm / upload / fileBlobUrl
├── src/components/Layout.tsx
├── src/components/AuthedFile.tsx 带鉴权的文件预览（凭证要先 fetch 成 blob）
├── src/App.tsx                  按 URL 分流 + 按 perms 过滤菜单 + URL 路由
├── src/pages/Login.tsx          登录页（由 URL 决定哪一端，生产不显示测试账号）
├── src/pages/DeskOverview.tsx   资金总览（按币种分列 + 账本自检）
├── src/pages/DeskClients.tsx    客户列表 + 详情抽屉 + 审核 + 商务条款 + 调账 + 停用
├── src/pages/DeskLedger.tsx     流水三视图 + 核销(选期间) + 冲正(双签) + 导出
├── src/pages/DeskTopups.tsx     充值管理 · 线下汇款（全部/待处理 + 五维筛选 + 发起申请 + 处理/查看 + 导出）
├── src/pages/DeskOpen.tsx       开户管理（全部/待处理 + 筛选 + 详情审批 + 自动开户 + Mock 回执）
├── src/pages/PortalOpen.tsx     客户门户开户申请（申请数 / 主体 / 媒体 / 进度）
├── src/pages/DeskAccounts.tsx   账户管理（广告账户主表 + 详情抽屉 + 充值记录 Tab）
├── src/pages/DeskTransactions.tsx 交易记录（只读交易流水 + 六维筛选 + 导出）
├── src/pages/PortalTransactions.tsx 门户交易记录（本客户广告账户充值 + 筛选 + 导出）
├── src/pages/DeskSpend.tsx      消耗报表（KPI / TOP10 / 趋势 / 四维分组 / 导出 / 导入弹窗）
├── src/pages/PortalWallet.tsx   门户钱包（可用/冻结分列）+ 账户消耗 + 充值
├── src/pages/PortalAccounts.tsx 门户广告账户（筛选 / 分页 / 明细 / 发起充值 / 导出）
├── src/pages/PortalProfile.tsx  门户入驻进度 + 被驳回后重提
└── e2e-check.mjs        端到端自检（puppeteer-core + 本机 Edge）

docs/
├── CONTRACT-P3.md        P3 接口契约（冻结版）
├── CONTRACT-P3.5.md      P3.5 凭证上传与强制校验契约
└── REVIEW-FIXLIST.md     前端闭环评审 · 27 条问题与修复状态

DESIGN.md                设计决策与已知差异
AGENTS.md                改代码前必读的约定
```
