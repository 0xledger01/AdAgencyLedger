# P3.5 接口契约（冻结版）· 凭证上传 + 确认到账强制校验 + 开户资质查看

> 背景：P3 收口时监督窗口挖出的最大风控缺口——`voucher` 只存路径字符串，`data/uploads/` 是空的，`confirm_topup` 对有无凭证一视同仁。
> 结果：任何有 `TOPUP_CONFIRM` 权限的人可以在**没有任何证据**的情况下把钱记进客户钱包，而 D3.1 主账本会把这条无凭证流水当事实永久留存。
>
> 本轮只做这一条链，不做 D7/D8/D9。

## 0. 范围

| # | 内容 | 为什么必须 |
|---|---|---|
| 1 | 文件上传 + 受控读取（含安全校验） | 凭证/执照要能真的传上去、看得见 |
| 2 | 确认到账强制校验凭证（特例需留原因） | 补上唯一的人工风控点 |
| 3 | `PENDING_PAY` 补传凭证出口 | 否则"没传凭证的充值单"是死状态 |
| 4 | D2 审批页可见客户资质 | 5 项清单里"主体一致/行业可投"现在无法核实 |

不在本轮：Excel 导入模板（D6）、per-order 冻结关联、客户子账号、D7–D10。

## 1. 不变式（与 P3 一致，新增第 9 条）

1. 余额只能由 `services/ledger.py` 的 `post()` / `freeze()` / `unfreeze()` 改。
2. `wallet_transaction` 只追加，纠错只能 `reverse()`。
3. 所有资金接口必须带幂等键。
4. `unknown` / `TO_VERIFY` 在人工核查前不动账。
5. 客户侧永不出现 `rebate` / 返点字段。
6. 每个端点挂 `require_perm`；`/api/desk` 整组已拦客户身份。
7. 金额用 `money()`，禁止 float。
8. 新字段同步 `migrate.py`。
9. **新增：任何上传文件必须经过「大小 + 类型白名单 + 内容魔数」三重校验；读取必须做路径穿越防护；客户只能读自己上传的文件。**

## 2. 新增表 `file_object`

```python
class FileObject(Base, TimestampMixin):
    """上传文件登记。不存绝对路径，只存相对 key，便于日后换 MinIO/OSS。"""
    __tablename__ = "file_object"
    id: int
    key: str            # 2026/10/9f3a2c1e.png —— 相对 upload_dir，unique
    original_name: str  # 客户看到的原始文件名
    content_type: str
    size: int           # 字节
    sha256: str         # 内容摘要，用于去重与取证
    uploader_id: int    # user.id
    client_id: int | None   # 客户上传时记 client_id；内部上传为 None
    biz_type: str | None    # TOPUP_VOUCHER / LICENSE / IDCARD / QUALIFICATION / OTHER
```

必须挂 `TimestampMixin`（AGENTS.md 约定）。`key` 加 unique 索引。

## 3. 文件接口

### 3.1 POST /api/files（上传）
**权限：任何已登录用户**（内部 + 客户都要能传）。`multipart/form-data`：
- `file`: 文件（必填）
- `biz_type`: `TOPUP_VOUCHER | LICENSE | IDCARD | QUALIFICATION | OTHER`（默认 `OTHER`）

校验顺序（任一不过即拒，**不要先落盘再校验**）：
1. 空文件 → 400 `INVALID_FILE`「文件为空」
2. `size > 5 * 1024 * 1024` → 400 `INVALID_FILE`「文件超过 5MB」
3. 扩展名白名单：`.jpg .jpeg .png .webp .pdf`（大小写不敏感）→ 否则 400 `INVALID_FILE`
4. **内容魔数嗅探**（不信 `Content-Type`、不信扩展名）：
   - `\xFF\xD8\xFF` → image/jpeg
   - `\x89PNG\r\n\x1a\n` → image/png
   - `RIFF....WEBP` → image/webp
   - `%PDF-` → application/pdf
   - 其它 → 400 `INVALID_FILE`「文件内容不是允许的图片/PDF」
5. 声明类型与嗅探结果不一致时，**以嗅探结果为准**写入 `content_type`

存储：`{upload_dir}/{YYYY}/{MM}/{uuid4().hex}{ext}`，返回的 `key` 是不含 `upload_dir` 的相对路径。
同 `sha256` 已存在且 `biz_type` 相同时，**复用已有记录**（返回同一 key，`deduped: true`），不重复落盘。

```json
{"id": 12, "key": "2026/10/9f3a2c1e8b7d4f10a2c3.png", "url": "/api/files/2026/10/9f3a2c1e8b7d4f10a2c3.png",
 "original_name": "转账回单.png", "content_type": "image/png", "size": 84213,
 "sha256": "e3b0c442...", "deduped": false}
```

### 3.2 GET /api/files/{key:path}（读取）
**权限：任何已登录用户**，但客户身份只能读 `client_id == user.client_id` 的文件（`client_id` 为 None 的内部文件，客户不可读 → 403）。

- 路径穿越防护（**必做，且必须有测试**）：把 `key` 解析为绝对路径后，必须 `Path.resolve()` 后仍在 `upload_dir.resolve()` 之下；含 `..`、绝对路径、盘符、空 key → 400 `INVALID_FILE`。
- 不存在 → 404。
- 响应：`FileResponse`，`Content-Type` 用登记值，`Content-Disposition: inline; filename="<original_name>"`（文件名要做 RFC 5987 编码，中文不乱码）。
- **禁止**把 `upload_dir` 直接挂 `StaticFiles`——那样绕过了身份与归属校验。

`biz_type` 白名单外取值 → 400。

## 4. 充值凭证闭环

### 4.1 门户：提交充值必须能带凭证
`POST /api/portal/topups` 的 `voucher` 改为接收上传返回的 `url`（`/api/files/...`）。
- 有 `voucher` → `PENDING_CONFIRM`
- 无 `voucher` → `PENDING_PAY`（现状已如此，不变）

### 4.2 门户：补传凭证（新增）
`POST /api/portal/topups/{biz_no}/voucher`，权限 `PortalUser`，body `{"voucher": "/api/files/..."}`
- 只能补自己的单（`client_id = user.client_id`，从登录态取，**不许从 body 读**）
- 仅 `PENDING_PAY` 可补，其它状态 400 `STATE_ERROR`
- `voucher` 必须是 `/api/files/` 开头且该文件属于当前客户，否则 400
- → `PENDING_CONFIRM`
→ `{"biz_no": "...", "status": "PENDING_CONFIRM", "status_label": "待确认"}`

### 4.3 内部：确认到账强制校验凭证（**本轮核心**）
`POST /api/desk/topups/{id}/confirm`，body 扩展为：
```json
{"remark": "已核对银行流水", "no_voucher_reason": null}
```
规则：
1. `topup.voucher` **为空** 且 `no_voucher_reason` 为空/长度 < 5 → **400 `STATE_ERROR`**，文案必须含「凭证」二字，例如：`无凭证不得确认到账，如确属线下现金等特殊情况请填写原因（不少于 5 字）`
2. `topup.voucher` 为空但 `no_voucher_reason` 有效（≥5 字）→ 放行，但：
   - 流水的 `remark` 追加 `｜无凭证确认：<reason>`
   - 写一条 `AuditLog(action="CONFIRM_NO_VOUCHER", doctype="client_topup", doc_id=id, reason=reason, actor=operator)`
3. `topup.voucher` 形如 `/api/files/...` 时，**校验该文件在磁盘上存在**；不存在 → 400「凭证文件已丢失，请重新上传或登记异常」
4. `topup.voucher` 是历史遗留的非 `/api/files/` 路径（如 `seed/voucher.png`）→ 不做存在性校验，放行（兼容老数据，但审计视角可查）
5. 其余确认逻辑（幂等键 `topup:<id>`、状态机）**一个字都不改**——重复确认仍必须返回 200 且不重复入账

响应追加 `"no_voucher": true|false`，便于前端提示。

### 4.4 列表/详情带凭证可读性
`GET /api/desk/topups` 与 `GET /api/desk/topups/{id}` 的 `voucher` 字段保持原值，另加：
- `voucher_url`: 若 `voucher` 以 `/api/files/` 开头，原样返回；否则 `null`（历史路径不可预览）
- `has_voucher`: bool（`voucher` 非空即 true）
- `voucher_missing`: bool（`voucher` 非空但文件已不在磁盘 → 供前端标红；历史非 `/api/files/` 路径返回 `false`）

## 5. D2 开户审批可见资质

### 5.1 GET /api/desk/open-requests/{id}
权限 `OPEN_REQUEST_VIEW`。
```json
{"item": { ...同列表 item... },
 "client": {
   "id": 2, "company_name": "杭州云帆…", "credit_code": "91330…",
   "registered_place": "中国", "founded_at": "2020-06-01", "industry": "跨境电商",
   "platforms": "FB,GOOGLE", "website": "https://…",
   "contact_name": "周九", "phone": "138…", "email": "…",
   "status": "ACTIVE", "status_label": "已开通",
   "license_url": "/api/files/…", "idcard_url": null, "qualification_url": null
 },
 "existing_accounts": [{"account_id": "act_1001", "platform": "FB", "status": "NORMAL"}]
}
```
`existing_accounts` 用于核对清单里的「是否重复开户」。

### 5.2 前端审批弹窗
在核对清单上方展示客户资质区：公司名/统一社会信用代码/注册地/行业/官网 + 三个材料缩略图（营业执照/身份证/其他资质），点击在新窗口打开 `/api/files/...`。缺材料显示「未提供」。
`existing_accounts` 非空时显示「该客户已有 N 个广告账户」提示（供 `duplicate` 项核对参考）。

## 6. 前端改动

| 文件 | 改动 |
|---|---|
| `DeskTopups.tsx` | ① 代录表单「凭证（链接）」→ 文件选择器（选完即上传，显示文件名 + 缩略图；失败展示后端 detail）② 列表凭证列 → 缩略图（`<img>` 48px），点击弹窗看大图；`voucher_missing` 标红「凭证丢失」③ 确认到账弹窗：无凭证时显示警示 + 「无凭证原因」输入框（≥5 字才可提交） |
| `DeskOpen.tsx` | 审批弹窗加客户资质区（见 5.2） |
| `PortalWallet.tsx` | ① 充值表单加文件上传（上传后把 `url` 放进 `voucher`）② `PENDING_PAY` 的充值记录加「补传凭证」按钮 → 调 4.2 |
| `api.ts` | 加 `upload(kind, file, bizType)`：`FormData` + `Authorization`，**不要**设 `Content-Type`（让浏览器自带 boundary） |

## 7. 验收断言（`backend/tests/smoke_p3_5.py`）

只打 HTTP，复用 `smoke_p3.py` 的 `call/check/login` 风格。必须覆盖：

| # | 断言 |
|---|---|
| 1 | 上传合法 PNG → 200，返回 key/url/sha256，`url` 以 `/api/files/` 开头 |
| 2 | `GET` 该 url → 200 且字节数与原文件相同 |
| 3 | 上传 `.exe`（内容为 `MZ...`）→ 400，文案含「文件内容」或「文件类型」 |
| 4 | 声明 `image/png` 但内容是纯文本 → 400（魔数嗅探生效，不信 Content-Type） |
| 5 | 上传 >5MB → 400，文案含「5MB」 |
| 6 | `GET /api/files/../../data/app.db` → 400 或 404，**响应体不含 SQLite 头 `SQLite format 3`** |
| 7 | 未登录 `GET /api/files/...` → 401 |
| 8 | 客户 A 用自己的 token 读客户 B 的文件 → 403 |
| 9 | **无凭证充值单 confirm → 400 且文案含「凭证」，余额不变** |
| 10 | 无凭证 + `no_voucher_reason`（≥5 字）→ 200，流水 `remark` 含该原因，AuditLog 有 `CONFIRM_NO_VOUCHER` |
| 11 | 有凭证（上传得到的 url）confirm → 200，余额 +金额 |
| 12 | 重复 confirm 仍 200 且余额不变（幂等未被破坏） |
| 13 | 门户提交无凭证 → `PENDING_PAY`；补传凭证 → `PENDING_CONFIRM`；再补一次 → 400 |
| 14 | `GET /desk/open-requests/{id}` 返回 `client` 与 `existing_accounts` |
| 15 | 门户提交的充值带 `/api/files/` 之外的凭证字符串 → 400（拒绝任意路径注入） |

## 8. 交付标准

```
.venv/Scripts/python.exe -m pytest -p no:warnings -q          # 现有 49 条不许少，新增上传/凭证用例
SMOKE_BASE=http://127.0.0.1:8014 .venv/Scripts/python.exe backend/tests/smoke_p3.py      # 73/73 不许回归
SMOKE_BASE=http://127.0.0.1:8014 .venv/Scripts/python.exe backend/tests/smoke_p3_5.py    # 15/15
cd frontend && npx tsc -b --force                              # 0 error
cd frontend && node e2e-check.mjs                              # 28/28
cd frontend && node p3-pages-check.mjs                         # 15/15
```
