# 开户管理 · 客户申请 → 管理端审批

## 流程

```
客户门户 /portal/openreq
  POST /api/portal/open-requests        客户提交（PENDING_APPROVAL / 待审批）
        ↓
内部后台 /desk/open                    「待处理」队列
  POST /api/desk/open-requests/{id}/approve
        auto_complete=true               审批通过并开户 → OPENED / 已开户
        auto_complete=false              仅审批 → PENDING_SUBMIT
              POST .../submit            提交一代 → AGENT_REVIEWING
              POST .../receipt           回执 → OPENED / AGENT_REJECTED
  POST /api/desk/open-requests/{id}/reject   驳回 → REJECTED（原因回传客户）
```

## 字段

| 字段 | 说明 |
|---|---|
| `source` | `CLIENT` 客户门户申请 / `DESK` 后台代提交 |
| `subject` | 开户主体，客户可填；老数据回填客户公司名 |
| `apply_count` | 申请开户数（1–20） |
| `completed_count` | 完成开户数；自动开户按申请数生成账户后写入 |
| `open_method` | `AUTO` 自动开户 / `MANUAL` 人工录入账户 ID |
| `applicant` | 客户侧提交人（登录账号显示名） |
| `approver` | 管理端处理人（列表显示为「处理人」） |
| `processed_at` | 审批 / 驳回 / 下户时间 |

## 校验

- **当前只做 TikTok**：开户媒体只接受 `TIKTOK`，门户与后台写接口对其余取值一律 400
  「仅支持 TikTok 开户」；下拉只下发 TikTok。存量 FB / Google 账户与账单保留只读，
  不删除、不冲正，仅不再允许新建。
- 客户只有在 `Client.status == ACTIVE` 时才能申请；停用/未完成入驻会被拒绝。
- 同客户 + 同媒体 + 同主体已有在办申请（待审批/待提交/一代审核中）时不允许重复提交。
- 审批仍要求五项核对清单全部勾选（主体/行业/BM/重复开户/渠道）。
- 自动开户生成的账户 ID 形如 `AUTO-<申请单号>-<序号>`，回执 JSON 标记 `mock: true`。
- 客户侧返回白名单字段，不含 `channel`、成本系数、返点等内部信息。

## 页面

- 后台「开户管理」：全部 / 待处理两个页签，按创建时间、代理商、媒体、单号、客户、主体、处理人、账户 ID 筛选；列表列与业务截图一致（ID/申请单号/客户/代理商/开户媒体/开户主体/申请数/完成数/开户方式/状态/处理人/创建时间/操作）。
- 客户门户「开户申请」：提交媒体、主体、数量、BM、时区、币种、日限额、备注；列表展示进度与驳回原因。
