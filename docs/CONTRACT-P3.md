# P3 接口契约（冻结版 · 4 窗口协作基准）

> 本文件先冻结，再动代码。后端按此实现，前端按此对接，验收脚本按此黑盒测试。
> 任何一方认为契约有问题，**先报告，不许自行改契约**。改动只能由指挥窗口统一写入。

## 0. 本轮范围

| 模块 | 内容 |
|---|---|
| D3 充值管理 | 列表 / 代录 / 确认到账 / 退回 / 冲正 / 调账 |
| D2 开户管理 | 列表 / 代提交 / 审批（5 项核对清单）/ 驳回 / 提交到一代 / 批量提交 / Mock 回执 |
| D5 加款任务 | 列表 / 人工发起（冻结）/ Mock 执行三态 / unknown 核查 / 重试 / 熔断开关 |
| D6 消耗导入 | 批量导入预校验 + 报告 / 列表 / 异常标记 |

不在本轮：P4（返点/对账/报表/一代钱包差额）、P5（对账单 PDF/工单回复/定时任务/设置）、文件上传、客户子账号。

## 1. 不变式（写代码前必须复述一遍）

1. 余额只能由 `services/ledger.py` 的 `post()` / `freeze()` / `unfreeze()` 改。
2. `wallet_transaction` 只追加，纠错只能 `reverse()`。
3. 所有资金接口必须带幂等键，格式 `<业务>:<单据id>`。
4. `unknown` / `TO_VERIFY` 状态在人工核查前**不动账**（冻结也不释放）。
5. 客户侧永不出现 `rebate` / 返点字段。
6. 每个新端点必须挂 `require_perm(Perm.XXX)`；`/api/desk` 整组已拦客户身份。
7. 金额用 `models.base.money()`，禁止 float。
8. 新增表字段同步 `migrate.py` 的 `_ADD_COLUMNS`。

## 2. 新增枚举标签（`models/enums.py` 追加，后端负责）

```python
TOPUP_STATUS_LABEL = {PENDING_PAY:"待支付", PENDING_CONFIRM:"待确认", RECEIVED:"已到账", RETURNED:"已退回", REVERSED:"已冲正"}
RECHARGE_STATUS_LABEL = {PENDING_EXEC:"待执行", EXECUTING:"执行中", AUDITING:"审批中", SUCCESS:"成功", REJECTED:"已拒绝", TO_VERIFY:"待核查"}
OPEN_REQUEST_STATUS_LABEL = {PENDING_APPROVAL:"待审批", PENDING_SUBMIT:"待提交", AGENT_REVIEWING:"一代审核中", OPENED:"已开户", AGENT_REJECTED:"一代拒绝", REJECTED:"已驳回"}
PAYMENT_METHOD_LABEL = {BANK_TRANSFER:"线下转账", ONLINE:"在线支付", THIRD_PARTY:"三方代付"}
FUND_SOURCE_LABEL = {SELF:"客户自有", THIRD_PARTY:"三方代付", CHANNEL_ADVANCE:"渠道垫资"}
```

所有列表接口返回 `status` + `status_label`，前端**只用 status_label**，不自己映射中文。

## 3. D3 充值管理 —— `/api/desk/topups`

### 3.1 GET /api/desk/topups
权限 `TOPUP_VIEW`。Query：`status` `client_id` `keyword`（单号/公司名/付款方模糊）`date_from` `date_to` `limit`(50,≤200) `offset`

```json
{
  "total": 12,
  "items": [{
    "id": 3, "biz_no": "TP2026102800003", "client_id": 2, "client_name": "…",
    "amount": "5000.0000", "currency": "USD",
    "payment_method": "BANK_TRANSFER", "payment_method_label": "线下转账",
    "fund_source_type": "SELF", "fund_source_label": "客户自有",
    "status": "PENDING_CONFIRM", "status_label": "待确认",
    "voucher": null, "payer": "…", "remark": null, "reject_reason": null,
    "confirmed_by": null, "confirmed_at": null,
    "created_at": "2026-10-28 13:00:00"
  }],
  "summary": {
    "pending_count": 3,
    "pending_amount": [{"currency": "USD", "amount": "12000.0000"}],
    "received_amount": [{"currency": "USD", "amount": "80000.0000"}]
  }
}
```
排序：`created_at DESC`。`summary` 受**同样的筛选条件**影响（除 limit/offset）。

### 3.2 POST /api/desk/topups（财务代录）
权限 `TOPUP_CONFIRM`。body：
```json
{"client_id": 2, "amount": "5000", "currency": "USD", "payment_method": "BANK_TRANSFER",
 "payer": "张三", "fund_source_type": "SELF", "voucher": null, "remark": "客户微信通知"}
```
→ 单条 item（同 3.1 结构）。走 `services/topup.create_topup`。`amount<=0` → 400 `INVALID_AMOUNT`。

### 3.3 GET /api/desk/topups/{id}
权限 `TOPUP_VIEW`。→ `{"item": {...}, "ledger": [{tx 白名单字段...}]}`
`ledger` = `wallet_transaction` 中 `ref_doctype='client_topup' AND ref_id=id` 的流水（含冲正红字）。
tx 字段：`id, seq, direction, amount, currency, balance_after, biz_type, operator, remark, occurred_at`。

### 3.4 POST /api/desk/topups/{id}/confirm
权限 `TOPUP_CONFIRM`。body `{"remark": "可选"}` → 走 `services.topup.confirm_topup`。
```json
{"id": 3, "status": "RECEIVED", "status_label": "已到账",
 "tx_id": 88, "balance": "5000.0000", "available": "5000.0000"}
```
幂等：重复确认返回 200 且 `tx_id` 相同、余额不再变化（`post` 幂等键 `topup:<id>`）。

### 3.5 POST /api/desk/topups/{id}/reject
权限 `TOPUP_CONFIRM`。body `{"reason": "凭证金额不符"}` → `{"id":3,"status":"RETURNED","status_label":"已退回"}`
仅 `PENDING_CONFIRM` 可退回；`reason` 必填（空 → 400 `STATE_ERROR`）。

### 3.6 POST /api/desk/topups/{id}/reverse（冲正 · 双签）
权限 `TOPUP_ADJUST`。body `{"reason": "银行退票", "second_approver": "赵六"}`
- `second_approver` 必填且 **≠ 当前登录用户 display_name**，否则 400 `STATE_ERROR`（PRD D3「调账与冲正需两人确认」）
- 走 `services.topup.reverse_topup`，只新增红字流水
→ `{"id":3,"status":"REVERSED","status_label":"已冲正","reverse_tx_id":91,"balance":"0.0000"}`

### 3.7 POST /api/desk/clients/{client_id}/adjust（调账 · 双签）
权限 `TOPUP_ADJUST`。body：
```json
{"amount": "100", "direction": "IN", "reason": "多收退款抵扣", "request_no": "ADJ-20261028-001", "second_approver": "张三"}
```
- `amount > 0`（方向由 direction 决定，不要传负数）
- `request_no` 必填，幂等键 = `adjust:<request_no>`；重复提交返回同一笔流水
- `second_approver` 必填且 ≠ 操作人
- 走 `post(biz_type=ADJUST, ref_doctype="client_adjust", ref_id=None)`
→ `{"tx_id": 95, "balance": "…", "available": "…"}`

## 4. D2 开户管理 —— `/api/desk/open-requests`

### 4.1 GET /api/desk/open-requests
权限 `OPEN_REQUEST_VIEW`。Query：`status` `client_id` `platform` `limit` `offset`
```json
{"total": 4, "items": [{
  "id": 7, "biz_no": "OR2026102800007", "client_id": 2, "client_name": "…",
  "platform": "FB", "bm_id": "BM-123", "timezone": "America/New_York",
  "currency": "USD", "daily_limit": "1000.0000",
  "status": "PENDING_APPROVAL", "status_label": "待审批",
  "approver": null, "approved_at": null, "reject_reason": null,
  "agent_receipt": null, "created_at": "2026-10-28 12:00:00"
}], "summary": {"PENDING_APPROVAL": 2, "PENDING_SUBMIT": 1, "AGENT_REVIEWING": 1, "OPENED": 0}}
```

### 4.2 POST /api/desk/open-requests（代客户提交）
权限 `OPEN_REQUEST_SUBMIT`。body `{"client_id":2,"platform":"FB","bm_id":"BM-1","timezone":"America/New_York","currency":"USD","daily_limit":"1000"}`
→ item。状态 `PENDING_APPROVAL`。客户不存在 400。

### 4.3 POST /api/desk/open-requests/{id}/approve
权限 `OPEN_REQUEST_APPROVE`。body：
```json
{"checks": {"subject": true, "industry": true, "bm": true, "duplicate": true, "channel": true}, "remark": "核对通过"}
```
- 五个 key 缺一或任一 false → 400 `STATE_ERROR`（回文案案：`核对清单未全部确认：duplicate`）
- 仅 `PENDING_APPROVAL` 可审批
→ `{"id":7,"status":"PENDING_SUBMIT","status_label":"待提交"}`

### 4.4 POST /api/desk/open-requests/{id}/reject
权限 `OPEN_REQUEST_APPROVE`。body `{"reason":"主体与营业执照不一致"}` → `REJECTED`。`reason` 必填。

### 4.5 POST /api/desk/open-requests/{id}/submit（进 RPA 队列）
权限 `OPEN_REQUEST_SUBMIT`。
- **仅 `PENDING_SUBMIT` 允许**，否则 400 `STATE_ERROR`，文案必须含「未审批通过」（铁律 7 服务端拦截，验收要断言这句）
- → `AGENT_REVIEWING`，`agent_receipt` 写入 Mock 排队回执 JSON 字符串
→ `{"id":7,"status":"AGENT_REVIEWING","status_label":"一代审核中","agent_receipt":"…"}`

### 4.6 POST /api/desk/open-requests/batch-submit
权限 `OPEN_REQUEST_SUBMIT`。body `{"ids":[7,8,9]}`
→ `{"submitted":[7],"skipped":[{"id":8,"reason":"状态 PENDING_APPROVAL 未审批通过"}]}`。整批不因个别失败回滚。

### 4.7 POST /api/desk/open-requests/{id}/receipt（Mock 一代回执）
权限 `OPEN_REQUEST_APPROVE`。body `{"result":"OPENED","account_id":"1234567890"}` 或 `{"result":"AGENT_REJECTED","reason":"BM 已被封"}`
- 仅 `AGENT_REVIEWING` 可回执
- `OPENED`：创建 `AdAccount`（client_id 取单据、`platform`/`currency`/`daily_limit` 取单据，`channel` 取客户渠道），`opened_at=now`，状态 `NORMAL`；`account_id` 已存在 → 400 `STATE_ERROR`「账户 ID 已存在」
- → `{"id":7,"status":"OPENED","status_label":"已开户","ad_account_id":31}`

## 5. D5 加款任务 —— `/api/desk/recharges`

### 5.1 GET /api/desk/recharges
权限 `RECHARGE_VIEW`。Query：`status` `client_id` `ad_account_id` `platform` `limit` `offset`
```json
{"total": 5, "items": [{
  "id": 11, "biz_no": "RC2026102800011", "client_id": 2, "client_name": "…",
  "ad_account_id": 31, "account_id": "1234567890", "platform": "FB",
  "amount": "500.0000", "currency": "USD",
  "status": "PENDING_EXEC", "status_label": "待执行",
  "approval_no": null, "serial_no": null, "initiated_by": "钱七",
  "retry_count": 0, "receipt": null,
  "created_at": "2026-10-28 13:10:00", "elapsed_ms": null
}], "summary": {"TO_VERIFY": 1, "PENDING_EXEC": 2}}
```
`elapsed_ms` = 有 receipt 时 `updated_at - created_at` 的毫秒数。

### 5.2 POST /api/desk/recharges（人工点击发起）
权限 `RECHARGE_VIEW`。body `{"ad_account_id": 31, "amount": "500", "remark": "客户要求", "request_no": "rc-3f2a..."}`
- `request_no` 必填，`min_length=6`，缺 → **422**
- 账户不存在 400；金额 ≤0 → 400 `INVALID_AMOUNT`
- 客户钱包（client_id 取账户所属客户，币种取账户币种）可用余额不足 → 400 `INSUFFICIENT_BALANCE`
- **幂等键 = `recharge:<request_no>`，在 `freeze()` 之前先查单**：命中则原样返回已有单并带 `deduped=true`，**绝不重复冻结**
- 未命中：`freeze(wallet, amount)` 冻结占位 → 落单 `PENDING_EXEC`，`biz_no = RC{yyyyMMdd}{id:05d}`
- `initiated_by = 当前用户 display_name`
→ 单条 item + `deduped: false`

> 铁律 3：资金接口必须带幂等键。这张单的键来自请求（`request_no`），不来自落库后生成的单号——否则"先冻结、后补键"天然挡不住连点。

### 5.7 POST /api/desk/recharges/{id}/cancel（撤销 · 释放冻结）
权限 `RECHARGE_RESOLVE`。body `{"reason": "客户取消加款"}`
- 仅 `PENDING_EXEC` / `TO_VERIFY` 可撤销，其余 400 `STATE_ERROR`
- `reason` 必填（空 → 400）
- `unfreeze` 释放冻结（已释放过则跳过），`balance` 全程不变
- `status=CANCELLED`（`status_label`「已撤销」），`receipt` 记 `{"mock":true,"cancel":{"reason","by","mock"}}`
→ `{"id":11,"status":"CANCELLED","status_label":"已撤销","reason":"客户取消加款"}`

### 5.3 POST /api/desk/recharges/{id}/exec（Mock RPA 执行）
权限 `RECHARGE_VIEW`。body `{"outcome": "SUCCESS"}`，取值 `SUCCESS | UNKNOWN | REJECTED`（可按平台熔断开关校验）

- 熔断开启（该 platform）→ 400 `STATE_ERROR`「平台 FB 已熔断，暂停执行」
- 仅 `PENDING_EXEC` 可执行
- `SUCCESS`：
  1. `unfreeze(wallet, amount)`（**先解冻，再出账**，否则 `post` 可用额校验会失败）
  2. `post(OUT, biz_type=RECHARGE, idempotency_key=f"recharge:{order.id}", ref_doctype="recharge_order", ref_id=order.id)`
  3. `AdAccount.balance += amount`（媒体侧余额，不走钱包守卫）
  4. 一代钱包：按 `AdAccount.channel` 匹配 `AgentChannel.name`，存在钱包则 `post(OUT, biz_type=RECHARGE, idempotency_key=f"recharge-agent:{order.id}", allow_negative=True)`；不存在则跳过，不报错
  5. `status=SUCCESS`，写 `serial_no` / `receipt`
- `UNKNOWN`：`status=TO_VERIFY`，**不解冻、不记账**，`receipt` 记 unknown 原文
- `REJECTED`：`unfreeze`，`status=REJECTED`
→ `{"id":11,"status":"SUCCESS","status_label":"成功","serial_no":"…","balance":"…","account_balance":"…"}`

### 5.4 POST /api/desk/recharges/{id}/resolve（核查 unknown）
权限 `RECHARGE_RESOLVE`。body `{"result":"SUCCESS","reason":"媒体后台已到账"}`（`SUCCESS | FAILED`）
- 仅 `TO_VERIFY` 可核查，其他状态 400
- `SUCCESS` → 同 5.3 成功路径（幂等键天然防重复）；`FAILED` → 解冻 + `status=REJECTED`
→ `{"id":11,"status":"SUCCESS","status_label":"成功"}`

### 5.5 POST /api/desk/recharges/{id}/retry
权限 `RECHARGE_RESOLVE`。仅 `REJECTED` / `TO_VERIFY` 可重试。
- 重新 `freeze`（若未冻结）+ `status=PENDING_EXEC` + `retry_count+=1`，**复用同一个 `idempotency_key`**
→ `{"id":11,"status":"PENDING_EXEC","retry_count":1}`

### 5.6 熔断开关
- `GET /api/desk/recharges/circuit` 权限 `RECHARGE_VIEW` → `{"items":[{"platform":"FB","enabled":false},{"platform":"GOOGLE","enabled":false}]}`
- `POST /api/desk/recharges/circuit` 权限 `RECHARGE_RESOLVE` body `{"platform":"FB","enabled":true}` → `{"platform":"FB","enabled":true}`
- 存储：进程内字典 + 落 `system_setting` 表（key=`circuit:FB`）；重启后可恢复。若不想建表，允许内存实现但必须在响应里注明 `"persisted": false`。

## 6. D6 消耗导入 —— `/api/desk/spend`

### 6.1 POST /api/desk/spend/import
权限 `SPEND_IMPORT`。
```json
{"dry_run": false, "rows": [
  {"date": "2026-10-27", "account_id": "1234567890", "spend": "120.5", "currency": "USD", "exchange_rate": "1"}
]}
```
处理规则（顺序即优先级）：
1. `date` 非法 / 未来日期 → failed（`日期非法`）
2. `account_id` 不存在 → failed（`账户不存在`）
3. `spend < 0` → failed（`金额为负`）
4. 同账户同日 `spend_daily` 已存在 → skipped（`已存在`）
5. 同批次内重复 → skipped（`批次内重复`）

落库：`SpendDaily(source="IMPORT")`，`base_spend = spend × exchange_rate`；`AdAccount.balance -= spend`（**消耗只扣广告账户，不动客户钱包**；余额可为负，不拦截）。

异常标记（写入响应，不阻断）：
- `SPIKE`：spend > 过去 7 天该账户均值 × 3（无历史则不算）
- `ZERO_SPEND`：spend == 0 且账户 `balance > 0` 且 `status == NORMAL`

```json
{"dry_run": false, "total": 3, "success": 1,
 "skipped": [{"row": 2, "account_id": "…", "date": "2026-10-27", "reason": "已存在"}],
 "failed":  [{"row": 3, "account_id": "…", "date": "2026-10-26", "reason": "账户不存在"}],
 "anomalies":[{"row": 1, "account_id": "…", "date": "2026-10-27", "spend": "120.5000", "reason": "SPIKE"}]}
```
`dry_run=true` 只预校验、不落库。**重复导入第二次必须全部 skipped 且 reason="已存在"**（PRD 验收原文）。

### 6.2 GET /api/desk/spend
权限 `SPEND_IMPORT`（只读同权限）。Query `date_from` `date_to` `client_id` `account_id` `limit` `offset`
```json
{"items": [{"date":"2026-10-27","account_id":"…","client_name":"…","platform":"FB",
            "spend":"120.5000","currency":"USD","base_spend":"120.5000",
            "source":"IMPORT","anomaly":null}],
 "totals": {"spend": [{"currency":"USD","amount":"120.5000"}]}, "total": 1}
```

### 6.3 Mock RPA 协议模块
新增 `services/rpa_mock.py`，仅做三件事，供 D2/D5 调用：
- `enqueue_open(order) -> str`：返回排队回执 JSON 字符串（含 `task_id`、`agent`、`queued_at`）
- `exec_recharge(order, outcome) -> dict`：返回 `{"outcome","serial_no","approval_no","raw"}`，`outcome` 由调用方传入（默认 `SUCCESS`）
- `heartbeat(agent_code) -> dict`：`{"agent_code","ok","at","version","pending_tasks"}`
所有 Mock 回执必须**明确标注 `"mock": true`**，防止后续误以为真机结果。

## 7. 前端页面（`frontend/src/pages/`）

| 文件 | 菜单 | 必备交互 |
|---|---|---|
| `DeskTopups.tsx` | 充值管理 | 状态筛选 + 金额汇总条 + 表格（单号/客户/金额/方式/状态/凭证/确认人）+ 行操作（确认到账·退回·冲正）+ 财务代录表单 + 冲正二次签名输入框 |
| `DeskOpen.tsx` | 开户管理 | 列表 + 审批弹窗（5 项核对清单勾选，未全勾禁用「通过」）+ 驳回原因 + 单条/批量提交 + Mock 回执注入（选 OPENED/AGENT_REJECTED + 填 account_id） |
| `DeskRecharge.tsx` | 加款任务 | 列表（含待核查高亮）+ 发起加款表单（选账户 + 金额）+ 执行时选 outcome 三态 + unknown「核查」按钮 + 重试 + 熔断开关开关组 |
| `DeskSpend.tsx` | 消耗导入 | 文本框粘贴 CSV（`account_id,date,spend`）或 JSON 数组 + 「预校验」/「导入」两个按钮 + 结果报告三色块（成功/跳过/失败）+ 异常原因列标黄 |

Nav（`App.tsx` 的 `DESK_NAV`）追加 4 项，顺序：资金总览 / 客户管理 / 充值管理 / 开户管理 / 加款任务 / 消耗导入 / 资金流水 / 我的档案。

前端约定：
- 所有中文状态用接口返回的 `status_label`；**前端不写状态映射表**
- 金额显示统一 `money()`（`api.ts` 已有）
- 连续 load 用 `seqRef` 递增序号丢弃过期响应（参考 `DeskLedger.tsx`）
- 不新增依赖，用现有 `.card / .btn / table` 样式类（`styles.css`）；缺样式就在 `styles.css` 补，不引入 CSS 框架

## 8. 验收（指挥窗口写 `backend/tests/smoke_p3.py`）

必须覆盖的断言（黑盒，只打 HTTP）：
1. 财务确认到账后客户钱包余额 = 原余额 + 充值额，且能查到 `TOPUP` 进账流水
2. 重复确认同一单：200，余额不变，流水不新增
3. 退回后状态 `RETURNED`，余额不变
4. 冲正 `second_approver` 为空 / 等于操作人 → 400；正常冲正后余额回退，原流水仍在
5. 未审批的 `/open-requests/{id}/submit` → 400 且文案含「未审批通过」
6. 核对清单缺项审批 → 400
7. 审批 → 提交 → Mock 回执 OPENED → 生成 AdAccount
8. 加款发起后客户钱包 `frozen` 增加、`balance` 不变
9. 加款 `UNKNOWN` → 状态 `TO_VERIFY`，`balance` 与 `frozen` 都不变，`verify` 链完好
10. 核查 `FAILED` → `frozen` 归零、`balance` 不变
11. 加款 `SUCCESS` → 客户钱包 −X、广告账户 +X、流水 `RECHARGE` 出账
12. 同一天消耗重复导入 → 第二次全部 `skipped` 且 reason 含「已存在」
13. 消耗导入不动客户钱包余额
14. 客户角色访问任一 `/api/desk/topups` → 403
15. 运营角色（OPS）调 `TOPUP_CONFIRM` → 403（权限矩阵）

跑法：`.venv/Scripts/python.exe backend/tests/smoke_p3.py`（`SMOKE_BASE` 默认 `http://127.0.0.1:8012`，沿用 `smoke_p2.py` 的 `call/check/login` 风格）。
