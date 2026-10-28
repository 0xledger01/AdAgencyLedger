# 项目约定（AGENTS）

## 铁律（改代码前必读）

1. **禁止直接改 `Wallet.balance` / `Wallet.frozen`** —— 只能走 `services/ledger.py` 的 `post()` / `freeze()`。守卫会抛 `ImmutableViolation`。
2. **禁止 UPDATE / DELETE `wallet_transaction`** —— 纠错只能 `reverse()` 新增红字。
3. **所有资金接口必须带幂等键**，格式 `<业务>:<单据id>` 或 `<业务>:<业务唯一号>`。
4. **钱只能由人点出去** —— 不允许写任何自动生成加款单的逻辑，定时任务只能生成"提醒"。
5. **加款对象永远是具体广告账户**，不允许往一代池子打钱却不落账户。
6. **`unknown` / `TO_VERIFY` 状态在人工核查前不动账**。
7. **返点对客户完全不可见** —— 客户侧 API、导出、PDF 都不许出现 `rebate` 字段与"返点"字样。

## 代码约定

- 金额一律 `from backend.app.models.base import money`，禁止用 float 算钱。
- 写库统一 `session.flush()` 拿 id，交事务边界由调用方 `commit()`。
- 不写裸 SQL（触发器与迁移除外），保证 SQLite ↔ Postgres 可切。
- 业务异常继承 `services/errors.LedgerError`，带 `code`。
- 新表必须挂 `TimestampMixin`，单据表挂 `IdMixin`（有 `biz_no`）。

## 验证方式

```bash
.venv/Scripts/python.exe -m pytest -q                  # 单元测试，必须全绿才交
.venv/Scripts/python.exe backend/tests/smoke_p2.py     # 接口验收（57+ 项）
cd frontend && npx tsc -b                              # 类型检查
cd frontend && node e2e-check.mjs                       # 前端端到端（22 项）
```

新增账本相关逻辑时，**先补测试再改实现**。

## P2 新增约定

- **API 端点必须挂权限依赖**：`user: User = require_perm(Perm.XXX)`。新增权限先去 `rbac.MATRIX` 登记。
- **`/api/desk` 整组已强制内部身份**，新增子路由不用重复加 `require_desk`，但**不要**把门户接口挂到这个 router 上。
- **客户侧接口一律走 `PortalUser`**，`client_id` 从 `user.client_id` 取，**永远不要从 query / body 读**。
- **客户侧返回用白名单构造函数**，不要 `dict(orm_obj)` 全量 dump。
- **新增字段要同步 `migrate.py`** 的 `_ADD_COLUMNS`，别指望 `create_all` 给已存在的表加列。
- **前端请求要防竞态**：组件里连续 load 时用递增序号丢弃过期响应（参考 `DeskLedger` 的 `seqRef`）。

## 目录

- `backend/app/models/` 表定义（17 张）
- `backend/app/services/ledger.py` 账本内核（改这里要极其小心）
- `backend/app/services/guards.py` 铁律守卫
- `backend/app/services/auth.py` / `rbac.py` 认证与权限矩阵
- `backend/app/services/clients.py` 客户三级审核
- `backend/app/services/ledger_views.py` D3.1 三视图 / 核销 / 冲正 / 导出
- `backend/app/services/topup.py` 充值单
- `backend/app/api/` desk / portal / auth 三组路由
- `backend/app/seed.py` 种子数据
- `backend/tests/test_ledger_kernel.py` 铁律单元测试
- `backend/tests/smoke_p2.py` 接口验收脚本
- `frontend/` React + Vite 前端，`e2e-check.mjs` 端到端自检
- `DESIGN.md` 设计决策与已知差异
