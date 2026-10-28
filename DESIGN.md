# 设计决策（DESIGN）

> 给下一轮接手的自己（或同事）看。改代码前先读这里，别猜。

## 0. 业务范围

**当前只做 TikTok。** 新建开户申请只接受 `Platform.TIKTOK`
（`models/enums.ENABLED_PLATFORMS`），门户/后台的媒体下拉只下发 TikTok，
写接口收到其它媒体一律 400「仅支持 TikTok 开户」。存量 FB / Google 的账户、
消耗与流水保留只读：不删、不冲正、照常参与报表，只是不再新建。

## 1. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | FastAPI + SQLAlchemy 2.0（同步 Session） | 转账/记账场景同步更直白，事务边界清晰 |
| 前端 | React + Vite + TS + Tailwind | P2 起建，见 `frontend/` |
| 库 | SQLite（本地）→ Postgres（生产） | 全程 ORM，不写裸 SQL，切库只改 `DATABASE_URL` |
| 文件 | 本地 `data/uploads/` → 后期 MinIO/OSS | 统一 `StorageBackend` 接口 |

## 2. 金额怎么存

**所有金额走自定义 `Money` 类型（`models/base.py`）**：

- SQLite：存 `BIGINT`，值是 `金额 × 10000`（4 位小数）
- Postgres：存 `NUMERIC(20,4)`
- 读出统一是 `Decimal`，永不出现 float

原因：金额用 float 或 SQLite `REAL`，几百笔流水后对账必然差几分钱。这一条不要改。

## 3. 账本内核（`services/ledger.py`）—— 全系统唯一写余额的地方

```
post(wallet, direction, amount, biz_type, idempotency_key, ...)
  ├─ 幂等键已存在 → 返回原流水，created=False（不重复扣款）
  ├─ 幂等键被不同业务占用 → IdempotencyConflict
  ├─ 锁钱包 → 校验可用余额（balance − frozen）
  ├─ 算 seq = max(seq)+1、before/after
  ├─ 改 balance（必须在 allow_balance_mutation 上下文内）
  └─ 插流水
```

约束：
- **余额只能由 `post()` 改**。直改走 `guards.py` 的 ORM 事件拦截。
- **流水只追加**。ORM 事件 + 数据库触发器双保险，连 `UPDATE wallet_transaction` 裸 SQL 都会被 SQLite 触发器 abort。
- **`seq` 每钱包从 1 连续递增**，`balance_after` 必须等于下一条的 `balance_before`。
- **纠错只能 `reverse()`** 新增红字，原流水不改。

## 4. 守卫机制（`services/guards.py`）

用 `ContextVar` 做"钥匙"：只有 `post()/freeze()/unfreeze()` 在 `allow_balance_mutation(wallet_id)` 上下文里改 `balance/frozen` 才放行，其它任何地方改都抛 `ImmutableViolation`。

> ⚠️ 关键坑：`session.flush()` 必须在 `with allow_balance_mutation(...)` **内部**。守卫在 flush 时触发，出了 with 就没钥匙了。

## 5. 资金四层与三个账本

| 层 | 谁的钱 | 加款时怎么动 |
|---|---|---|
| 客户子钱包 | 我方欠客户（负债） | − 加款金额 |
| 一代钱包 | 我方在一代处的余额（外部参照） | − 加款金额 |
| 广告账户 | 媒体侧可用预算 | + 加款金额 |
| 平台钱包 | 我方实持资金 | 不变（打给一代时已出） |

铁律：**消耗只扣广告账户，不重复扣客户钱包**（v1.1 修正的双算问题）。

## 6. 状态与枚举

DB 里存英文 code（`PENDING_REVIEW`），中文标签走 `enums.py` 的 `*_LABEL` 字典。理由：状态机代码好写，前端要中文再加映射层，不影响历史数据。

## 7. 返点不可见（铁律 8）

- `CLIENT_HIDDEN_BIZ_TYPES = {REBATE}`，客户侧查询必须过滤
- 客户侧 API 的 serializer 白名单制，不 dump 整个 ORM 对象
- 对账单 PDF 生成后要做关键词自检：`返点 / rebate / 佣金`

## 8. PRD 待确认项的当前默认值

| # | 问题 | 当前实现 |
|---|---|---|
| 1 | 预付池 vs 按笔付 | **预付池**，`AgentTopUp` 保留，`balance_type` 兼容授信 |
| 2 | 开户审批单级/两级 | 单级（运营）。客户在门户提交申请，管理端审批；审批可选择「通过并开户」（自动生成 Mock 账户）或「仅审批」后走一代 RPA |
| 3 | 大额二次确认 | 5000 USD（`settings.large_amount_threshold_usd`） |
| 4 | 消耗来源 | 优先 Excel 导入（`SpendDaily.source`） |
| 5 | 币种 | USD（充值只做美金，不再提供 CNY） |
| 6 | 退款政策 | 余额可退，已消耗不退 |

改这几项之前先动 `DESIGN.md`，再改代码。

## 9. 账户管理（原「加款任务」）

业务方口径：日常是**按广告账户**干活，不是按加款单干活。所以菜单里的
「加款任务」改成「账户管理」，页面以广告账户为主表：

- 列：ID / 广告账户 / 时区 / 申请单号 / 客户 / 代理商 / 平台 / 开户主体 /
  BC ID / 余额 / 状态 / 授权状态 / 开户时间 / 备注；
- 行内「充值」直接对该账户发起充值（仍是 冻结 → 执行 两步，未绕过账本）；
- 行内「详情」抽屉 = 账户信息 + 客户档案 + 客户钱包 + 开户单 + 最近充值记录；
- 「充值记录」Tab 保留原来的加款单列表（执行 / 核查 / 重试 / 撤销都在那里）。

数据来源：`ad_account.timezone` 开户时由开户单带入（`openreq._create_ad_account`），
开户单关联靠 `open_request.agent_receipt` 里的 `ad_account_ids` / `accounts`。
新增权限 `ad_account.view` 只管"看账户"，充值仍要 `recharge.view`，执行仍要 `recharge.exec`。

## 9. 本地 vs 生产的已知差异

| 项 | 本地 SQLite | 生产 Postgres |
|---|---|---|
| 行锁 | 无真 `SELECT FOR UPDATE`，用事务 + 幂等兜底 | 真行锁 |
| 并发压测 | ❌ 测不了 200 并发 | ✅ |
| 触发器 | SQLite TRIGGER | PL/pgSQL 函数 + TRIGGER（`db.py` 已备） |

## 10. P2 新增的设计决策

### 10.1 认证与身份

- **bcrypt(rounds=12) + JWT(HS256, 12h)**。`secret_key` 本地默认值写死在 config，**生产必须用环境变量覆盖**。
- 后台登录 `/api/auth/login` 与门户登录 `/api/auth/portal/login` 分开，交叉登录直接 403（防止内部员工从门户入口绕过菜单）。
- 前端也分两个地址：`/` → 内部后台，`/portal` → 客户门户，**没有页面内切换入口**。URL 决定一切，登录页互不可见。
- 用户名不存在时也跑一次假哈希（`burn_password_check`），避免时序差异泄露账号是否存在。

### 10.2 数据隔离三层落地

1. `APIRouter(prefix="/api/desk", dependencies=[Depends(require_desk)])` —— 整组拦掉客户身份，**不用每个端点记得加**。
2. `ensure_client_scope()` —— 客户角色传入的 `client_id` 一律被登录态覆盖。前端传 `?client_id=2` 完全无效。
3. serializer 白名单 —— 客户侧只 dump 明确列出的字段，不是"排除敏感字段"（排除法漏一个就出事）。

### 10.3 审核状态机

- 步骤由**客户当前状态推导**（`CURRENT_STEP`），不接受前端指定 step，否则销售可以直接跳到定额度。
- 每步允许的角色写死在 `STEP_ROLE`，权限矩阵只负责"能不能进审核流程"（`Perm.CLIENT_AUDIT`）。
- 驳回强制填原因，否则客户不知道为什么被拒。

### 10.4 核销为什么单独一张表

流水不可修改是铁律。核销是"财务事后确认"这一**独立事实**，不是流水本身的属性。
所以用 `ledger_reconcile(tx_id)` 单独记，流水表一个字段都不加。

### 10.5 期初余额怎么算

`opening = 筛选起始时间之前、该钱包 seq 最大的那条 balance_after`，没有则 0。
期末 = 期初 + 本期进 − 本期出，并与本期最后一条 `balance_after` 比对，不一致就 `has_conflict=true` 标红。

> 全量流水视图**没有期初概念**（跨客户跨钱包），所以它的 KPI 用"本期进账 / 出账 / 净额"，不要硬套期初期末。

### 10.6 前端两个坑

- **请求竞态**：客户下拉初始化会触发两次 load（先空后具体客户），慢的先返回会覆盖新结果。用 `seqRef` 递增序号丢弃过期响应。
- **受控输入**：自动化测试填表单必须用 native setter + dispatch `input` 事件，`element.type()` 在受控组件上会追加而不是替换。

## 11. 尚未实现（别以为坏了）

- 对账 D8、报表 D9、返点 D7、系统设置 D10（P4）
- PDF 对账单、定时任务 J1–J6、通知告警（P5）
- RPA 执行端 R1–R8：现只有 `services/rpa_mock.py`（回执带 `mock:true`），无执行端进程与 HTTP 协议
- 客户子账号：`User.client_id` 支持多绑，但没有创建接口
- ~~门户侧"充值到广告账户"入口（PRD P4 入口二）：只有后台代发起~~
  → 已落地，见 §21（客户只发起，执行仍在后台）

## 12. P3 新增的设计决策

### 12.1 加款的幂等键必须来自请求，不能来自单号

`POST /api/desk/recharges` 先冻结再落单，如果幂等键用 `biz_no`（依赖自增 id），就挡不住连点——两次请求各自冻结一次。
所以 body 强制带 `request_no`（前端 `crypto.randomUUID()`），键为 `recharge:<request_no>`，**冻结前先按该键查单**，命中直接返回原单并带 `deduped:true`。

### 12.2 冻结必须有出口

`PENDING_EXEC` 单如果不提供撤销，钱会被永久冻结（只能靠真执行出去才释放）。所以有 `POST /recharges/{id}/cancel`：解冻 + `CANCELLED`，余额不变。

> 尚未解决：冻结额是**钱包级聚合**，没有 per-order 关联。多张单并存时 `retry` 用 `frozen >= amount` 判断"是否已冻结"可能把 A 单的冻结算到 B 单头上。要根治得加 `wallet_hold` 表（P4 再说）。

### 12.3 审批备注写 AuditLog，不加列

`OpenRequest` 没有 `remark` 列，审批意见进 `AuditLog(doctype="open_request")`。理由：审批意见是"事后留痕"的独立事实，和 `ClientReview` 一个思路。

### 12.4 消耗列表的异常标记是展示期计算

`SpendDaily` 不存 anomaly 列，`GET /desk/spend` 每次按"过去 7 日均值"实时算。`ZERO_SPEND` 用账户**当前**余额判定，所以和导入时刻的判定可能有出入——这是有意的，异常是"看的时候的现象"，不是事实。

## 13. P3.5 新增的设计决策

### 13.1 文件不挂 StaticFiles

`upload_dir` **绝不能**直接挂 `StaticFiles`：那样任何人拿到 URL 就能读，归属校验形同虚设。
走 `GET /api/files/{key:path}`：内部员工可读全部，客户只能读 `client_id == 自己` 的文件（内部上传的 `client_id=None` 客户读不到）。

### 13.2 类型判定只信内容魔数

扩展名白名单只管"挑存储后缀"，真正的类型判据是前 32 字节的魔数（JPEG/PNG/WEBP/PDF）。
声明 `Content-Type: image/png` 但内容是文本 → 拒。理由：`Content-Type` 和扩展名都是客户端说了算。

### 13.3 路径穿越要用 resolve，不能用字符串替换

`safe_resolve()` 先 `unquote`（挡 `%2e%2e%2f`），再拒绝含 `..` 段 / 绝对路径 / 盘符，最后 `Path.resolve()` 判断是否仍在 `upload_dir` 之下。
只做 `replace("..", "")` 会被 `....//` 绕过。

### 13.4 确认到账必须校验凭证

`confirm_topup` 是整条资金链路**唯一的人工风控点**。无凭证确认会留下一条永远无法自证的流水，而流水按铁律不可删改。
规则：
- 无凭证 → 400，除非填 `no_voucher_reason`（≥5 字），且该原因写进流水 remark + `AuditLog(CONFIRM_NO_VOUCHER)`
- 凭证是 `/api/files/` 地址 → 校验磁盘上真的存在，否则 400（防"凭证文件丢了还入账"）
- 历史遗留路径（如 `seed/voucher.png`）→ 不做存在性校验，兼容老数据
- **幂等优先**：已 `RECEIVED` 的单直接返回，不再走凭证校验，否则历史无凭证单永远确认不了

### 13.5 `PENDING_PAY` 必须有出口

没传凭证的充值单落 `PENDING_PAY`，如果不给补传入口就是死状态。所以有
`POST /api/portal/topups/{biz_no}/voucher`：仅 `PENDING_PAY` 可补，补完转 `PENDING_CONFIRM`。

### 13.6 seed 幂等看业务键，不看待变状态

`seed_pending_clients` 原来用"有没有 PENDING_REVIEW/PENDING_RISK 的客户"判重。
演示把这两个客户审到 ACTIVE 之后，下次启动就认为"没有待审客户"又灌一对 → 每跑一次演示多一对重名客户（实际踩过：库里出现过 id 4/5 和 6/7 两对）。
改为按**公司名**判重。

## 14. P6 客户管理：列表口径与运营动作

### 14.1 列表字段怎么来（不新增重复状态）

客户管理列表按运营习惯铺开（ID / 登录邮箱 / CRM 客户 / 负责销售 / 负责 AE /
可用 / 冻结 / 授信额度 / 授信余额 / 现金余额 / 启用 / 活跃 / 创建人 / 创建时间）：

- **金额取该客户 `settle_currency` 对应的客户钱包**：现金余额 = `balance`，
  可用 = `balance − frozen`，授信余额 = `credit_limit − credit_used`（不为负）。
  钱包不存在就是 0，不为列表去创建钱包（创建钱包=准备动钱）。
- **登录邮箱取该客户的主门户账号 `sys_user.username`**（`is_primary` 优先）。
- **启用状态由 `Client.status` 推导**，不新增 `enabled` 列：
  `ACTIVE` = 启用、`DISABLED` = 停用、其余审核态 = 未开通。两个"能不能动账"的
  真相源会打架，`ensure_client_wallet` 只认 status。
- **活跃状态 = 最近 30 天有门户登录**（`Client.last_active_at`，登录成功时刷新）。
  阈值集中在 `services/clients.activity_cutoff()`，列表与筛选共用。
- CRM 名/编号、负责 AE、创建人是 P6 新增列，走 `migrate.py` 增量 ALTER；
  老库演示客户由 `backfill_demo_client_profile()` 只补空值。

### 14.2 添加客户 vs 入驻审核是两条路

- 「添加客户」= 运营代客建档，默认 `ACTIVE` 并写 `AuditLog(client.create)`；
  勾掉"立即启用"则落 `PENDING_REVIEW`，回到销售初审 → 风控 → 财务定额。
- 销售只能把 `sales_owner` 挂在自己名下（服务端强制，不信前端）。
- 授信额度是财务口径：创建/编辑/批量里出现 `credit_limit` 都要 `client.limit`。
- 启用/停用批量开关要 `client.disable`；单客户启停仍走 `/clients/{id}/disable`。

### 14.3 新增客户后怎么登录门户

「添加客户」填的**登录邮箱就是门户账号**，初始密码两种来源：

- 填了「初始密码」→ 用填的；
- 留空 → 用系统默认密码 `settings.client_default_password`（默认 `123456`，
  生产用环境变量 `CLIENT_DEFAULT_PASSWORD` 覆盖，至少 6 位）。

创建响应回显「登录邮箱 + 初始密码」，弹窗同时给出 `/portal` 地址和复制按钮。
客户拿到这两项即可登录，不依赖运营抄一次性随机串。

登录接口 `POST /api/auth/portal/login` 会去掉首尾空格并**不区分大小写**匹配邮箱；
建号/改邮箱时也统一小写去空格，只差大小写的重复邮箱按「已被占用」拒绝。

### 14.3.1 重置密码

`POST /clients/{id}/reset-password`（`client.reset_password`，OPS/ADMIN）返回明文，
写 `AuditLog(client.reset_password)`。密码只以 bcrypt 落库，响应不缓存。
**留空则重置回默认密码**（客户忘记密码时的标准动作）。
编辑里补登录邮箱会自动建门户账号并同样回显初始密码。

### 14.4 导入只更新，不批量建号

CSV 导入按 客户编号（本地或 CRM）/ 登录邮箱 / 客户名称 匹配**已有客户**，只改
CRM 名、CRM 编号、负责销售/AE、授信额度。未匹配的行逐行报错，不静默建号——
建号要生成初始密码并交给客户，必须逐条确认。拒收非 UTF-8/GB18030 文件与 >2MB 文件。

### 14.5 同步客户不偷偷联网

没有 CRM 凭证，所以"同步客户"只读 `data/crm_clients.json`（人工导出），按
CRM 编号 → 登录邮箱 → 本地编号匹配，只补 `crm_name/crm_code`；文件不存在时返回
`configured=false` 和明确提示，不假装同步成功。

## 15. D3 改版：充值管理 = 线下汇款申请单

### 15.1 一张表，两种账户

复用 `client_topup`，新增 `fund_account_type / op_type / txn_no / payable_amount /
received_amount / applied_by / reviewed_by / reviewed_at / review_note`。存量 TP 单靠
迁移默认值落成「现金账户 · 加款」，与新单同表，不另起一张互相打架的申请表。

状态追加 `PENDING_REVIEW（待审核）/ APPROVED（审核通过）/ REJECTED（已驳回）`。
旧状态保留：门户自助与旧「财务代录」仍会落 `PENDING_PAY / PENDING_CONFIRM / RECEIVED`，
列表把它们一并视为待处理/已完成，避免历史单永久挂死。

### 15.2 审核通过才动账，且只动一次

| 账户 | 操作 | 审核通过后 |
|---|---|---|
| 现金账户 | 加款 | `post(IN, TOPUP, key=topup:<id>)` → 客户钱包 +到账金额 |
| 现金账户 | 减款 | `post(OUT, REFUND, key=fund-out:<id>)` → 客户钱包 −金额，余额不足直接 400 |
| 授信账户 | 加款 | 客户还款：`client.credit_used -= 金额`，低于 0 拒绝 |
| 授信账户 | 减款 | 占用授信：`client.credit_used += 金额`，超过 `credit_limit` 拒绝 |

现金两条路都挂 `ref_doctype='client_topup', ref_id=<id>`，单据详情能直接看到关联流水。
授信不动现金，因此不写钱包流水，只写 `AuditLog(fund_app.approve)`。

### 15.3 为什么授信加款是"还款"

线下汇款单里的 `授信账户 + 加款` 是客户把欠款还进来（截图里的 `-repay-$BILL`），
所以减少已用授信；`减款` 才是继续占用额度。没有单独"授信余额"字段，超额还款
直接拒绝而不是留负数——负数在报表里无法区分"多收"与"错账"。

### 15.4 凭证闸口保留

现金加款仍要求凭证，或填写不少于 5 字的无凭证原因（写入流水备注 + 审计）。
凭证是 `/api/files/` 地址时校验磁盘存在性；丢失直接拒绝审核。减款与授信不动钱进客户，
不要求凭证。

### 15.5 旧接口不删，但页面只走新流程

`/api/desk/topups`（代录/确认到账/退回/冲正）保留给门户与回归测试；新页面用
`/api/desk/fund-applications`。两条路共用账本幂等键，同一单不可能双臂入账；
冲正入口收敛到资金流水页（双签），充值管理页只负责"发起 → 审核"。

## 16. D6 消耗报表（按 CRM 原型改版）

### 16.1 数据结构：总消耗 = GMV Max + BC

`spend_daily` 从"一个总消耗"扩成报表结构（全部走 `money` / `migrate.py` 增量加列）：

- 金额：`gmv_max_spend` / `gmv_max_net_spend` / `bc_spend` / `bc_net_spend` /
  `bc_cash_spend` / `bc_grant_spend`
- 指标：`impressions` / `clicks` / `conversions`（只挂 BC 侧）
- `spend` 保留为**总消耗**（= GMV Max + BC），`base_spend` 仍按汇率折算

老数据只有 `spend`：迁移回填 `bc_* = spend`（GMV Max 记 0），保证
「GMV Max + BC = 总消耗」恒等，也保证历史口径不被改写。新导入只填总消耗时同样按 BC 落。

### 16.2 币种：一次报表只算一个币种

跨币种求和没有业务含义。`GET /desk/spend/report` 返回 `range.currencies`（当前筛选下的
可选币种）与 `range.currency`（本次口径），前端下拉只换口径，不把 USD 与 HKD 加在一起。
未指定时取消耗最大的币种。

### 16.3 分组：客户 / 账户 / 销售 / 代理商，按对象聚合

客户、销售、代理商报表**一个对象一行**；同一对象跨多个媒体时媒体列显示「多平台」，
不按媒体拆行（媒体筛选仍然生效）。账户报表天然按账户聚合。TOP10 客户始终按客户聚合，
不随 Tab 变化。

日期分组有两种：`mode=summary`（日期列显示「汇总」，环比与上一周期比）与 `mode=daily`
（按对象×天拆行，环比不适用）。上一周期 = 与所选区间等长的前一段。

### 16.4 指标口径

- 平均千展 = BC 消耗 ÷ 曝光 × 1000
- 点击率 = 点击 ÷ 曝光 × 100
- 平均点击费用 = BC 消耗 ÷ 点击
- `SPIKE` / `ZERO_SPEND` 异常仍按原来的 7 日均值规则实时计算，不落库

### 16.5 导入格式

前端支持媒体后台导出的中文表头 CSV（账户ID/日期/GMV Max总消耗/…/转化数），
也兼容老格式 `account_id,date,spend[,currency,exchange_rate]` 与 JSON。
`account_id + date` 仍是唯一键，重复导入跳过；总消耗与明细不一致（>0.01）判「金额不一致」，
计数为负判「指标为负」。客户/媒体由账户带出，不在文件里填。

### 16.6 页面

`/desk/spend` 从"粘贴框"改成报表页：数据概览三张 KPI、TOP10 客户、按天折线
（纯 SVG，悬停 tooltip）、四个报表 Tab + 消耗明细、查询/导出、以及「导入消耗」弹窗。
宽表横向滚动时媒体/日期/客户三列钉在左侧。导入仍先预校验，不落库。

### 15.6 美元到账、人民币付款：门户按央行中间价折算

- 钱包只做美金：门户充值填「到账美金」，美元钱包 1:1 入账，不因汇率缩水。
- 客户实际线下转人民币。门户弹层调 `GET /api/portal/topup-quote?amount=10`：
  首选央行授权中间价（中国外汇交易中心 USD/CNY），失败再退商业行情接口，
  返回「应付人民币」（向上取整到分）与报价时间/来源。
- 汇率缓存 1 小时；供应商全挂时最多沿用 24 小时内的旧报价并标 `stale`，
  超过 24 小时返回 `FX_UNAVAILABLE`（503）拒绝报价，不拿旧汇率收钱。
- 下单时服务端重新取价；客户端 `expected_rate` 漂移超过 0.2% 返回
  `RATE_CHANGED`（409），前端重新报价让客户确认新金额。
- 单据落 `exchange_rate / payable_amount / payable_currency=CNY / rate_source`
  快照，财务确认到账时按快照核对。内部代录（`/api/desk/fund-applications`）
  仍是 1:1 口径，只有门户充值走人民币应付。
- 服务端仍拒绝 `currency == CNY` 的到账（钱包币种只支持 USD）。

## 16. 资金流水改版：运营台账口径（2026-10-28）

### 16.1 为什么从"三视图"改成一张表

业务方给的运营台账原型是一行一笔钱：`ID / 账单ID / 客户 / 代理商 / 广告账户 /
交易类型 / 交易金额 / 交易前余额 / 交易后余额 / 备注 / 创建时间`，顶部四个筛选
（代理商 / 账单ID / 客户名称编号 / 交易类型）+ 查询、导出，底部翻页。

- 客户往来、全量流水本质是同一批数据的不同筛选，拆成 Tab 反而要来回切；
  **客户对账需要的期初/期末**没有丢：客户筛选只命中一家时，表格上方多一条
  「期初 → 期末 / 进 / 出 / 未核销」的客户对账条（原先客户往来 Tab 的内容）。
- 账户消耗是另一张表（`spend_daily`），保留为第二个 Tab，口径不变。
- 接口：`GET /api/desk/fund-flow`（列表）/ `GET /api/desk/fund-flow/export`（xlsx / csv），
  权限仍是 `ledger.view` / `ledger.export`，销售只能看自己名下客户。

### 16.2 交易类型不是 biz_type

同一个 `biz_type` 的进/出是两种动作（充值 vs 减款、广告账户充值 vs 退款），
运营是按动作筛账的，所以在 `services/ledger_flow.py` 里维护
`TXN_TYPES: (biz_type, 真实方向) -> 中文`，列表、筛选、导出共用这一张表。

- 冲正流水不改 `biz_type`（铁律：只追加），方向是反的。它的动作要**递归追到根流水**：
  冲正可以被再冲正（把冲正撤回），只翻一次方向会把第二层冲正归成"充值"这种不存在的动作。
  `_reverse_type_map()` 一次解析出 `冲正 id -> 根流水 (biz, direction)`，列表归类与
  `交易类型` 筛选都用它，保证筛出来的和看到的完全一致。
- 代理商 / 广告账户列只对广告账户类流水有值：靠 `ref_doctype='recharge_order'` 反查
  `recharge_order -> ad_account`。其余流水（充值/减款）两列为 `—`，与原型一致；
  筛代理商时按"账户渠道优先、客户渠道兜底"匹配，不会把客户的钱漏掉。
- 汇总按币种分开（禁止跨币种求和），一行 `USD 进 / 出 / 净 · 笔数`。

### 16.3 核销、冲正、导出保留

- 核销：勾选流水 → 标记核销（独立事实，不动流水）；冲正：双签弹窗，只新增反向流水。
- 导出走 `/fund-flow/export`，列与页面一致，冲正流水在交易类型后加「（冲正）」。
- 页面不再放统计卡：台账就是台账，汇总放表尾（`tfoot`）。

## 17. 问题工单：管理端受理（2026-10-28）

客户在门户 `/portal/tickets` 提交，管理端 `/desk/tickets` 受理。

### 17.1 为什么单独开一张表字段而不是复用 `ticket`

`models/docs.py::Ticket`（表 `ticket`）是早期同形的并行表，当前没有接口在用；
门户实际读写的是 `models/user.py::PortalTicket`（表 `portal_ticket`）。这次只给
`PortalTicket` 补受理字段 `priority / reply / handled_by / handled_at`，
不合并两张表，避免动历史数据与两套外键。`migrate.py` 对老库补列，
工单默认 `PENDING / NORMAL`。

### 17.2 状态机与回复

- 状态固定四个：`PENDING → PROCESSING → RESOLVED / CLOSED`，结单可重新打开为
  `PROCESSING`；已关闭不能直接回 `PENDING`。状态机在
  `models/enums.py::TICKET_NEXT_STATUSES`，详情接口把 `next_statuses` 下发，
  前端下拉不另写一份。
- **结单必须有回复**：`RESOLVED / CLOSED` 时若工单没有任何回复，后端 400。避免
  客户问题被静默关掉；回复会在门户「处理说明」列展示。
- 多次回复**按时间追加**在同一 `reply` 字段（`[时间 处理人]` 分段），不覆盖历史，
  也不新增消息表；需要对话流时再拆表。
- 每次处理写 `audit_log`（`action=ticket.handle`, `doctype=portal_ticket`），
  记录处理前后状态与改动的字段。

### 17.3 权限与数据范围

- 新增 `ticket.view`（内部五个角色都有）与 `ticket.handle`
  （销售 / 财务 / 运营 / 管理员）；风控只读。
- 销售仍按 `scope_sales_owner` 收口：列表、详情、处理都只能碰自己名下客户的工单。
- 客户侧只能看到自己的工单，回复与中文状态标签走 `TICKET_STATUS_LABEL`。

## 18. 证照 OCR：上传即带出企业信息（2026-10-28）

门户「开户申请」里上传营业执照/BR 后，前端调
`POST /api/portal/open-requests/parse-license`，后端读图识别并把
名称、信用代码/登记证号、注册地带回表单。实现集中在
`services/license_ocr.py`，`services/openreq.py::license_hints` 负责降级编排。

### 18.1 为什么用本地 ONNX 而不是云 OCR

- 证照属于客户敏感材料，不往第三方上传；`rapidocr-onnxruntime` 的模型随 wheel
  附带，装完即可离线跑，不需要 Key、不需要额外服务。
- 代价是推理要 8–20 秒/张（CPU，取决于机器负载；缓存命中约 10 ms）。因此：长边先缩到 1400 px；结果按文件
  `sha256` 做进程内 LRU 缓存（32 条），同一张图选历史文件时不再重跑。
- 开关与参数在 `config.py`：`ocr_enabled` / `ocr_max_side` / `ocr_min_score`。
  关掉 OCR 后接口退回「档案 + 文件名」提示，不报错。

### 18.2 两种证照怎么认

- **香港商业登记证（BR）**：识别到 `Business Registration` / `商業登記` /
  `XXXXXXXX-XXX-XX-XX-X` 形态的登记证号即判为 `HK_BR`。名称优先取中文全称
  （`有限公司/集團/商行…`），同时保留英文名；生效/届满日期按阅读顺序取前两个
  `DD/MM/YYYY`；注册地固定 `HK`。
- **中国大陆营业执照**：识别到 `营业执照` / `统一社会信用代码` 判为
  `CN_LICENSE`。信用代码按 18 位字符集 + 校验位验证，OCR 常把 `0/1/5`
  认成 `O/I/S`，先纠正再验；名称、住所、法定代表人、注册资本、成立日期、
  营业期限按「字段名在左列、值在右列或紧随其下」的版面取。
- 取值用几何位置（同一基线 → 右侧/下方连续块），不是全文正则硬套；
  拿不准就留空并写 `warnings`，前端提示人工核对。

### 18.3 降级顺序（绝不伪造）

1. 本地 OCR 认出关键字段 → `source=ocr`，OCR 空着的字段用「同一张图 = 客户档案
   执照」时的档案值补齐，并在 note 里注明；
2. OCR 没认出 → 用客户档案 → `source=profile`；
3. 再退到文件名里的信用代码/公司名/地区 → `source=filename`；
4. 都不行 → `parsed=false`，`source=manual`，字段保持可编辑。

引擎缺失、坏图、PDF 无渲染组件都走同样路径，`note` 里写明原因。

### 18.4 前端行为

- 只覆盖「空字段」或「上一次也是 OCR 自动填的字段」；用户手改过的值不会被
  换一张图冲掉（`autoFill` 引用记录来源）。
- 注册地下拉默认 `CN`，视为未选择：BR 识别出 `HK` 会直接切过去。
- 页面上不再展示识别摘要卡（类型/置信度/地址/有效期/识别原文已去掉），
  只把识别结果填进表单，并留一行「已带出可识别信息，请核对」的文字提示；
  提示与填写结果都不参与提交字段。

### 18.5 已知边界

- 手写体、严重倾角、强反光扫描件识别率明显下降；字段错了以人工核对为准。
- PDF 证照需要额外装 `pypdfium2` 才能读第一页，未装时提示「先导出为图片」。
- 不自动填「行业」「投放国家」：证照上的经营范围与平台行业码不是一一对应，
  硬映射会把客户带进错误的合规分类。

### 18.6 手工验收

用仓库外的香港 BR 样张（某某公司）走门户开户弹窗，期望：

```
doc_type      HK_BR
name          某某公司
credit_code   75531145-000-07-25-0
registered_place HK
valid_from    2025-07-20
valid_to      2026-07-19
```

名称/证号/注册地应自动填进表单，识别摘要卡出现「香港商业登记证（BR）」。
单测不跑真实推理：`backend/tests/test_license_ocr.py` 用坐标 + 文本守抽取规则
与所有降级路径。

## 19. 门户登录页开户申请 = 销售线索（2026-10-28）

### 19.1 为什么不在这一步建客户

门户登录页的「开户申请」只收公司名、联系人、手机 / 微信 / 邮箱等
**销售能联系上客户的基础信息**，落 `client_lead`，不创建 `Client` / `User`：

- 未验证主体不会混进客户列表，也不会产生没人管的门户登录账号；
- 销售在管理端「客户申请」认领 → 电话 / 微信联系 → 标记已联系 / 已转客户 / 已关闭；
- 确认合作后仍走「客户管理」正式建档，补齐资质、条款、账号和额度。

### 19.2 入口与权限

- `POST /api/public/client-applications`：匿名提交，只返回统一回执，不回传线索 ID
  或联系方式，避免被拿来探测号码是否已提交；
- 同一手机 / 微信 / 邮箱 7 天内重复提交只保留一条；
- `client.lead.view` / `client.lead.handle`：销售、运营、管理员；
- 销售只能看到自己认领的 + 未认领的线索，运营 / 管理员看全量；
- 线索状态机 `NEW → CONTACTED → CONVERTED`，`NEW / CONTACTED → CLOSED`，
  关闭后允许重新打开；已转客户是终点。

### 19.3 页面

- `/portal` 默认是开户申请落地页，登录按钮在右上角；点「登录」才显示原登录卡；
- 管理端侧栏「客户申请」按状态分 Tab，详情抽屉可复制联系方式、写内部跟进备注；
- 备注只给内部看，客户侧没有任何线索查询接口。

## 20. 交易记录 = 广告账户交易流水（2026-10-28）

### 20.1 口径

- 侧栏新增「交易记录」，只读页；列按 CRM 原型：交易编号 / 交易类型 / 账户ID /
  交易金额 / 操作备注 / 状态 / 反馈信息 / 操作人 / 操作时间。
- 与「账户管理 → 充值记录」分工：那边是操作台（执行 / 核查 / 重试 / 撤销），
  这边是流水与媒体侧反馈，页面上没有任何动账按钮。
- 数据源只有 `recharge_order`，不新增表、不写账。`GET /api/desk/transactions`
  只做查询；`GET /api/desk/transactions/export` 用同一套筛选导出 CSV（带 BOM）。

### 20.2 交易编号（txn_no）

- 新列 `recharge_order.txn_no`，格式 `ADTR + 创建日期 + 6 位序号`
  （如 `ADTR20261028000012`）。
- 与业务单号 `biz_no`（`RC2026102800012`）分开：客服报号用交易编号，
  财务对单据用业务单号；搜索框两个都认。
- 存量单由 `migrate.py` 的 `_backfill_recharge_txn_no` 回填（Python 回填，
  不依赖 SQLite / Postgres 的日期函数差异）；幂等，只补空值。

### 20.3 状态口径

交易状态是执行口径，与充值单精确状态一一映射，不另存：
`PENDING_EXEC → 待开始`、`EXECUTING / AUDITING → 处理中`、`SUCCESS → 已完成`、
`REJECTED → 失败`、`TO_VERIFY → 待核查`、`CANCELLED → 已撤销`。
接口下发 `PENDING / RUNNING / VERIFY / DONE / FAILED / CANCELLED`；前端上色时
统一加 `TXN_` 前缀取 `utils.STATUS_STYLE`，避免与工单 `PENDING`（琥珀色）撞色。

### 20.4 反馈信息 / 操作人

- 反馈信息来自 `receipt`：成功显示媒体流水号，失败显示拒绝原因，待核查显示
  「结果未知」，已撤销显示作废原因；未结束的单子显示 `—`，不编造结果。
- 操作人优先取 `receipt` 里的 `cancel.by` / `verify.by` / `exec_by`，老单据回落到
  `initiated_by`。执行成功也写入 `exec_by`，避免一律显示发起人。
- 操作备注目前只放「重试 N 次」；不拿内部回执原文冒充操作备注。

### 20.5 为什么没有「广告账户退款」

当前没有退款 / 回收广告账户余额的业务入口，凭空下发一个永远筛不到的
「广告账户退款」会误导运营。退款流程落地后在 `services/txn_records.py`
扩交易类型与数据源（前缀 ADTK）。

## 21. 门户广告账户管理（2026-10-28）

### 21.1 口径

- 门户侧栏「广告账户」以媒体账户为主表：媒体 / 账户 ID / 余额 / 账户状态 /
  开户审核 / 时区 / 开户主体 / 备注 / 最近充值，支持开户日期、账户 ID、
  开户主体、媒体、状态筛选与 CSV 导出；
- 每行「明细」抽屉展示账户信息、现金可用 / 冻结、近 30 天消耗与最近充值记录；
- 每行「充值」从**客户现金钱包**发起，冻结后落 `PENDING_EXEC` 加款单，
  由运营在后台「账户管理」执行到账。客户侧没有任何直接出账入口。

### 21.2 铁律落点

- 客户身份只从登录态取 `user.client_id`，接口不接收前端传入的 client_id；
- 请求级幂等键 `request_no`（前端每次打开弹层生成 `crypto.randomUUID()`），
  服务端统一加 `portal-` 前缀，网络重试不会重复冻结；
- 冻结走 `recharge.create_recharge`，不调用 `execute()`；余额只在运营执行时变动；
- 白名单返回：`channel` / `cost_ratio` / `rebate_rate` / 代理商一律不下发，
  CSV 同样过滤；客户的广告账户余额与现金钱包分开展示，不跨币种相加。

### 21.3 校验

- 客户未开通（非 `ACTIVE`）、账户封停、平台熔断时拒绝发起；
- 金额最多 2 位小数且不得超过该币种现金可用余额；
- 别人的账户按 404 处理，不暴露账户是否存在。

## 22. 门户交易记录（2026-10-28）

### 22.1 定位

- 门户侧栏新增「交易记录」（`/portal/transactions`），只读展示**当前登录客户**的
  广告账户充值交易；列：交易编号 / 交易类型 / 账户ID / 交易金额 / 状态 /
  反馈信息 / 操作人 / 操作时间。
- 与「我的钱包 → 资金账户流水」分工：流水是**资金账户**的收支台账（含充值、加款、
  冲正、余额前后）；交易记录是**广告账户充值单**的执行结果（ADTR 流水号、媒体侧
  到账状态、失败原因），不重复展示余额变化。
- 没有动账入口：发起充值仍在「广告账户 → 充值」，钱只能由运营执行（铁律 4）。

### 22.2 隔离与白名单

- `client_id` 只从 `PortalUser.client_id` 取；查询同时约束
  `recharge_order.client_id` 与 `ad_account.client_id`，脏数据也不会串号。
- 只下发 12 个字段：`id / txn_no / txn_type / txn_type_label / account_id /
  amount / currency / status / status_label / feedback / operator / occurred_at`。
  客户名、代理商、渠道、业务单号（RC…）、重试次数一律不返回。
- 反馈信息只给结论（已到账 / 充值失败已退回 / 确认中 / 已撤销已退回），
  不透传内部回执原文；操作人走 `anonymize.display_name` 历史人名映射。

### 22.3 接口与导出

- `GET /api/portal/transactions`：日期、交易编号（交易号/业务单号都认）、
  交易类型、广告账户ID、状态、分页；选项由后端 `txn_type_options` /
  `status_options` 下发。
- `GET /api/portal/transactions/export`：同一套筛选导出 CSV（带 BOM），
  导出前做「返点 / rebate / 佣金」自检（铁律 8）。
- 服务层 `services/txn_records.py` 的 `list_client_transactions` /
  `serialize_client` / `export_client_csv` 只读，不新增表、不写账。

### 22.4 验证

- `backend/tests/test_portal_transactions.py`：本客户隔离、白名单字段、
  状态/账户/编号筛选、导出内容、只读（余额与流水条数不变）。
- `frontend/e2e-check.mjs`：客户侧栏入口、列、ADTR 流水、筛选、不泄露渠道/返点。

## 22. BC 管理（客户门户，2026-10-28）

### 22.1 放在哪一端

BC 管理属于**客户端自助能力**：入口是门户侧栏「BC 管理」（`/portal/bc`），
客户维护自己名下的 BCID。内部后台不再渲染该菜单；`/api/desk/clients/{id}/bcids`
保留为客服/运营的兜底接口（无页面入口），不参与客户日常流程。

### 22.2 数据与来源

表 `client_bcid`（`uq_client_bcid` 保证同一客户同一个号只有一行）：

- `HISTORY`（历史绑定）：读取列表时从 `ad_account.bm_id`、`open_request.bm_id`
  与 `open_request.bc_ids` 归纳，幂等补行；已存在的行不改来源、不改默认项。
- `MANUAL`（手工添加）：客户自己录入；同一个号原来是历史行时升级为 `MANUAL`，
  不新增重复行。

排序固定为：默认项置顶 → 手工添加（新添加的在前）→ 历史绑定按创建顺序。
这样刚添加的号一定落在第 1 页，不会被分页藏起来。

### 22.3 默认绑定

- 同一客户最多一个 `is_default=True`；勾选新的会自动取消旧的，取消后回到「未设默认」。
- 开户申请页（`GET /api/portal/open-requests`）把默认项放在 `bc_options` 第一位，
  并直接放进 `default_bc_ids`；没有默认项时退回第一个选项（老行为）。

### 22.4 删除留墓碑（关键）

删除只删绑定关系，不动广告账户 `bm_id`、不动历史开户单；但**不能删完刷新又回来**，
否则「删除」按钮是假的。实现是软删除：

- `client_bcid.is_deleted / deleted_at` 留墓碑，列表、汇总、开户选项、默认项查询全部过滤；
- 历史对账的 `existing` 集合包含墓碑，所以已删除的号不会被重新归纳；
- 客户手工重新添加同一个号时复活原行（`revived=true`），不会产生重复行；
- 迁移在 `migrate.py` 增量补列，老库不需要重建。

### 22.5 权限与校验

- 门户接口 `GET/POST/DELETE /api/portal/bcids…` 只认 `PortalUser`，
  `client_id` 一律从登录态取，URL/body 里塞 `client_id` 无效；
- 操作别人的行按 404 处理，不暴露存在性；
- BCID 允许数字/字母与 `- _ .`，长度 4–64；非法输入走 `STATE_ERROR`（400）；
- 所有增删改写 `AuditLog(doctype="client_bcid")`，actor = 登录客户显示名。

### 22.6 验证

- `backend/tests/test_portal_bcid.py`：只看自己、默认预选、墓碑稳定、复活、
  跨客户 404、未登录/内部账号/客户越端 403、非法输入 400；
- `frontend/portal-bc-check.mjs`：门户入口、无客户选择器、分页、默认单选、
  手工添加、删除刷新不回弹、两个客户隔离，22 项全绿且控制台零错误。
