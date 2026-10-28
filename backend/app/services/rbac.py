"""权限矩阵（PRD 3.2）。

服务端校验是唯一有效的校验，前端隐藏只是体验优化。
"""

from __future__ import annotations

from enum import StrEnum

from ..models.user import ROLE_LABEL, Role, User
from .auth import PermissionDenied, is_client


class Perm(StrEnum):
    CLIENT_LIST = "client.list"
    CLIENT_VIEW = "client.view"
    CLIENT_AUDIT = "client.audit"              # 进入审核流程的资格（具体步骤在 service 校验）
    CLIENT_REVIEW = "client.review"            # 初审
    CLIENT_RISK = "client.risk"                # 风控
    CLIENT_LIMIT = "client.limit"              # 定额度 / 商务条款
    CLIENT_DISABLE = "client.disable"
    CLIENT_EDIT = "client.edit"                # 添加 / 编辑 / 批量修改客户资料
    CLIENT_RESET_PASSWORD = "client.reset_password"
    CLIENT_EXPORT = "client.export"
    CLIENT_IMPORT = "client.import"

    # 门户登录页提交的开户申请（销售线索）
    CLIENT_LEAD_VIEW = "client.lead.view"
    CLIENT_LEAD_HANDLE = "client.lead.handle"

    OPEN_REQUEST_VIEW = "open_request.view"
    OPEN_REQUEST_APPROVE = "open_request.approve"
    OPEN_REQUEST_SUBMIT = "open_request.submit"

    AD_ACCOUNT_VIEW = "ad_account.view"        # 账户管理：广告账户列表 / 详情

    BC_VIEW = "bc.view"                        # BC 管理：查看客户 BCID 绑定
    BC_MANAGE = "bc.manage"                    # BC 管理：添加 / 删除 / 设默认

    TOPUP_VIEW = "topup.view"
    TOPUP_APPLY = "topup.apply"                # 发起线下汇款申请（运营/财务）
    TOPUP_CONFIRM = "topup.confirm"
    TOPUP_ADJUST = "topup.adjust"              # 调账 / 冲正（需双签）

    LEDGER_VIEW = "ledger.view"
    LEDGER_VIEW_ALL = "ledger.view_all"        # 看全部客户，否则只看自己名下
    LEDGER_EXPORT = "ledger.export"
    LEDGER_RECONCILE = "ledger.reconcile"

    WALLET_VIEW = "wallet.view"
    WALLET_AGENT_VIEW = "wallet.agent_view"

    RECHARGE_VIEW = "recharge.view"
    RECHARGE_EXEC = "recharge.exec"            # 真的把冻结的钱打出去（RECHARGE_VIEW 不含）
    RECHARGE_RESOLVE = "recharge.resolve"

    SPEND_IMPORT = "spend.import"
    REBATE_VIEW = "rebate.view"
    REBATE_CONFIRM = "rebate.confirm"
    RECON_VIEW = "recon.view"
    REPORT_VIEW = "report.view"

    SETTINGS_MANAGE = "settings.manage"
    PORTAL_ACCOUNT_VIEW = "portal.account.view"

    TICKET_VIEW = "ticket.view"                # 查看客户问题工单
    TICKET_HANDLE = "ticket.handle"            # 受理 / 回复 / 结单


_ALL = {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN}

# 权限的中文名：对外展示、报错文案、导出与文档统一用这一份，
# 权限码（Perm 值）只作为程序内部主键，不出现在用户可见的界面里。
PERM_LABEL: dict[Perm, str] = {
    # 客户管理
    Perm.CLIENT_LIST: "查看客户列表",
    Perm.CLIENT_VIEW: "查看客户详情",
    Perm.CLIENT_AUDIT: "受理客户审核",
    Perm.CLIENT_REVIEW: "客户初审",
    Perm.CLIENT_RISK: "客户风控审核",
    Perm.CLIENT_LIMIT: "设定客户额度",
    Perm.CLIENT_DISABLE: "停用客户",
    Perm.CLIENT_EDIT: "编辑客户资料",
    Perm.CLIENT_RESET_PASSWORD: "重置客户密码",
    Perm.CLIENT_EXPORT: "导出客户",
    Perm.CLIENT_IMPORT: "导入客户",
    # 客户申请（线索）
    Perm.CLIENT_LEAD_VIEW: "查看客户申请",
    Perm.CLIENT_LEAD_HANDLE: "跟进客户申请",
    # 开户
    Perm.OPEN_REQUEST_VIEW: "查看开户申请",
    Perm.OPEN_REQUEST_APPROVE: "审批开户申请",
    Perm.OPEN_REQUEST_SUBMIT: "提交开户申请",
    # 账户管理
    Perm.AD_ACCOUNT_VIEW: "查看广告账户",
    # BC 管理
    Perm.BC_VIEW: "查看商务中心绑定",
    Perm.BC_MANAGE: "维护商务中心绑定",
    # 充值
    Perm.TOPUP_VIEW: "查看充值单",
    Perm.TOPUP_APPLY: "发起充值申请",
    Perm.TOPUP_CONFIRM: "确认充值入账",
    Perm.TOPUP_ADJUST: "充值调账冲正",
    # 资金流水
    Perm.LEDGER_VIEW: "查看资金流水",
    Perm.LEDGER_VIEW_ALL: "查看全部客户流水",
    Perm.LEDGER_EXPORT: "导出资金流水",
    Perm.LEDGER_RECONCILE: "流水核销对账",
    # 钱包
    Perm.WALLET_VIEW: "查看钱包",
    Perm.WALLET_AGENT_VIEW: "查看一代钱包",
    # 加款任务
    Perm.RECHARGE_VIEW: "查看加款任务",
    Perm.RECHARGE_EXEC: "执行加款",
    Perm.RECHARGE_RESOLVE: "处理加款异常",
    # 消耗 / 返点 / 对账 / 报表
    Perm.SPEND_IMPORT: "导入消耗数据",
    Perm.REBATE_VIEW: "查看返点",
    Perm.REBATE_CONFIRM: "确认返点",
    Perm.RECON_VIEW: "查看对账",
    Perm.REPORT_VIEW: "查看报表",
    # 系统
    Perm.SETTINGS_MANAGE: "管理系统设置",
    Perm.PORTAL_ACCOUNT_VIEW: "查看门户账号",
    # 问题工单
    Perm.TICKET_VIEW: "查看问题工单",
    Perm.TICKET_HANDLE: "处理问题工单",
}

MATRIX: dict[Perm, set[Role]] = {
    # 客户管理
    Perm.CLIENT_LIST: {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.CLIENT_VIEW: {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN, Role.CLIENT},
    Perm.CLIENT_AUDIT: {Role.SALES, Role.RISK, Role.FINANCE, Role.ADMIN},
    Perm.CLIENT_REVIEW: {Role.SALES, Role.ADMIN},
    Perm.CLIENT_RISK: {Role.RISK, Role.ADMIN},
    Perm.CLIENT_LIMIT: {Role.FINANCE, Role.ADMIN},
    Perm.CLIENT_DISABLE: {Role.ADMIN},
    Perm.CLIENT_EDIT: {Role.SALES, Role.OPS, Role.ADMIN},
    Perm.CLIENT_RESET_PASSWORD: {Role.OPS, Role.ADMIN},
    Perm.CLIENT_EXPORT: {Role.SALES, Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.CLIENT_IMPORT: {Role.OPS, Role.ADMIN},
    # 客户申请：销售认领跟进，运营 / 管理员看全量
    Perm.CLIENT_LEAD_VIEW: {Role.SALES, Role.OPS, Role.ADMIN},
    Perm.CLIENT_LEAD_HANDLE: {Role.SALES, Role.OPS, Role.ADMIN},
    # 开户
    Perm.OPEN_REQUEST_VIEW: {Role.OPS, Role.ADMIN, Role.CLIENT},
    Perm.OPEN_REQUEST_APPROVE: {Role.OPS, Role.ADMIN},
    Perm.OPEN_REQUEST_SUBMIT: {Role.OPS, Role.ADMIN},
    # 账户管理（只读看账户；充值仍走 recharge.* 权限）
    Perm.AD_ACCOUNT_VIEW: {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN},
    # BC 管理：内部角色都能看；销售 / 运营 / 管理员可维护（销售仍按名下客户收口）
    Perm.BC_VIEW: {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.BC_MANAGE: {Role.SALES, Role.OPS, Role.ADMIN},
    # 充值
    Perm.TOPUP_VIEW: {Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.TOPUP_APPLY: {Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.TOPUP_CONFIRM: {Role.FINANCE, Role.ADMIN},
    Perm.TOPUP_ADJUST: {Role.FINANCE, Role.ADMIN},
    # 流水
    Perm.LEDGER_VIEW: {Role.SALES, Role.FINANCE, Role.OPS, Role.ADMIN, Role.CLIENT},
    Perm.LEDGER_VIEW_ALL: {Role.FINANCE, Role.ADMIN},
    Perm.LEDGER_EXPORT: {Role.FINANCE, Role.ADMIN, Role.CLIENT},
    Perm.LEDGER_RECONCILE: {Role.FINANCE, Role.ADMIN},
    # 钱包
    Perm.WALLET_VIEW: {Role.FINANCE, Role.OPS, Role.ADMIN, Role.CLIENT},
    Perm.WALLET_AGENT_VIEW: {Role.FINANCE, Role.OPS, Role.ADMIN},
    # 加款
    Perm.RECHARGE_VIEW: {Role.FINANCE, Role.OPS, Role.ADMIN, Role.CLIENT},
    Perm.RECHARGE_EXEC: {Role.OPS, Role.ADMIN},
    Perm.RECHARGE_RESOLVE: {Role.OPS, Role.ADMIN},
    # 其他
    Perm.SPEND_IMPORT: {Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.REBATE_VIEW: {Role.FINANCE, Role.ADMIN},
    Perm.REBATE_CONFIRM: {Role.FINANCE, Role.ADMIN},
    Perm.RECON_VIEW: {Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.REPORT_VIEW: {Role.FINANCE, Role.OPS, Role.ADMIN, Role.SALES},
    Perm.SETTINGS_MANAGE: {Role.ADMIN},
    Perm.PORTAL_ACCOUNT_VIEW: {Role.CLIENT},
    # 问题工单：内部角色都能看（销售按名下客户收口），受理交给销售 / 财务 / 运营
    Perm.TICKET_VIEW: {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN},
    Perm.TICKET_HANDLE: {Role.SALES, Role.FINANCE, Role.OPS, Role.ADMIN},
}


def can(user: User, perm: Perm) -> bool:
    return user.role in MATRIX.get(perm, set())


def perms_for(user: User) -> list[str]:
    """当前角色实际持有的权限码。

    前端按钮与后端 require_perm 必须取自同一份矩阵，否则迟早错位 ——
    这是 F-03 的根因：前端各自硬编码 ["FINANCE","ADMIN"]。
    """
    if not user.active:
        return []
    return sorted(p.value for p, roles in MATRIX.items() if user.role in roles)


def perm_labels_for(user: User) -> dict[str, str]:
    """当前角色持有的权限：权限码 → 中文权限名。

    与 perms_for 同源，只做翻译，不另算一套口径；前端展示直接取这里，
    不要在页面里再维护一份「code → 中文」的私有映射。
    """
    return {code: perm_name(code) for code in perms_for(user)}


def perm_name(perm: Perm | str) -> str:
    """权限码取中文名；未知码原样返回，避免把程序撑死。"""
    try:
        return PERM_LABEL[Perm(perm)]
    except ValueError:
        return str(perm)


def perm_title(perm: Perm | str) -> str:
    """报错 / 悬浮提示用的「中文名（权限码）」。"""
    return f"{perm_name(perm)}（{perm}）"


def require(user: User, perm: Perm) -> None:
    if not user.active:
        raise PermissionDenied("账号已停用")
    if not can(user, perm):
        role_label = ROLE_LABEL.get(user.role, str(user.role))
        raise PermissionDenied(f"当前角色（{role_label}）无权执行该操作：{perm_title(perm)}")


def require_internal(user: User) -> None:
    """铁律：客户角色不得访问 /desk 任何接口。"""
    if is_client(user):
        raise PermissionDenied("客户账号无权访问内部后台")


def scope_sales_owner(user: User) -> str | None:
    """销售只看自己名下客户（按 sales_owner 匹配员工名）；None 表示不限。"""
    if user.role is Role.SALES:
        return user.display_name
    return None
