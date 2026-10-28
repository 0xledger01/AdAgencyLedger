# 权限中文名对照表

权限码是程序内部主键（`rbac.Perm`），对外一律展示中文权限名。
这份表由 `backend/app/services/rbac.py` 的 `PERM_LABEL` 导出，改代码后重跑生成，别手改。

| 中文权限 | 权限码 | 可授角色 |
| --- | --- | --- |
| 查看客户列表 | `client.list` | 销售、风控、财务、运营、管理员 |
| 查看客户详情 | `client.view` | 客户、销售、风控、财务、运营、管理员 |
| 受理客户审核 | `client.audit` | 销售、风控、财务、管理员 |
| 客户初审 | `client.review` | 销售、管理员 |
| 客户风控审核 | `client.risk` | 风控、管理员 |
| 设定客户额度 | `client.limit` | 财务、管理员 |
| 停用客户 | `client.disable` | 管理员 |
| 编辑客户资料 | `client.edit` | 销售、运营、管理员 |
| 重置客户密码 | `client.reset_password` | 运营、管理员 |
| 导出客户 | `client.export` | 销售、财务、运营、管理员 |
| 导入客户 | `client.import` | 运营、管理员 |
| 查看开户申请 | `open_request.view` | 客户、运营、管理员 |
| 审批开户申请 | `open_request.approve` | 运营、管理员 |
| 提交开户申请 | `open_request.submit` | 运营、管理员 |
| 查看广告账户 | `ad_account.view` | 销售、风控、财务、运营、管理员 |
| 查看充值单 | `topup.view` | 财务、运营、管理员 |
| 发起充值申请 | `topup.apply` | 财务、运营、管理员 |
| 确认充值入账 | `topup.confirm` | 财务、管理员 |
| 充值调账冲正 | `topup.adjust` | 财务、管理员 |
| 查看资金流水 | `ledger.view` | 客户、销售、财务、运营、管理员 |
| 查看全部客户流水 | `ledger.view_all` | 财务、管理员 |
| 导出资金流水 | `ledger.export` | 客户、财务、管理员 |
| 流水核销对账 | `ledger.reconcile` | 财务、管理员 |
| 查看钱包 | `wallet.view` | 客户、财务、运营、管理员 |
| 查看一代钱包 | `wallet.agent_view` | 财务、运营、管理员 |
| 查看加款任务 | `recharge.view` | 客户、财务、运营、管理员 |
| 执行加款 | `recharge.exec` | 运营、管理员 |
| 处理加款异常 | `recharge.resolve` | 运营、管理员 |
| 导入消耗数据 | `spend.import` | 财务、运营、管理员 |
| 查看返点 | `rebate.view` | 财务、管理员 |
| 确认返点 | `rebate.confirm` | 财务、管理员 |
| 查看对账 | `recon.view` | 财务、运营、管理员 |
| 查看报表 | `report.view` | 销售、财务、运营、管理员 |
| 管理系统设置 | `settings.manage` | 管理员 |
| 查看门户账号 | `portal.account.view` | 客户 |
