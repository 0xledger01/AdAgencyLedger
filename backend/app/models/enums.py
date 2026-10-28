from __future__ import annotations

from enum import StrEnum


class ClientStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"          # 待审核（销售初审）
    PENDING_RISK = "PENDING_RISK"              # 待风控
    PENDING_LIMIT = "PENDING_LIMIT"            # 待定额度
    ACTIVE = "ACTIVE"                          # 已开通
    REJECTED = "REJECTED"                      # 已驳回
    DISABLED = "DISABLED"                      # 已停用


CLIENT_STATUS_LABEL = {
    ClientStatus.PENDING_REVIEW: "待审核",
    ClientStatus.PENDING_RISK: "待风控",
    ClientStatus.PENDING_LIMIT: "待定额度",
    ClientStatus.ACTIVE: "已开通",
    ClientStatus.REJECTED: "已驳回",
    ClientStatus.DISABLED: "已停用",
}


class OwnerType(StrEnum):
    PLATFORM = "PLATFORM"    # 平台钱包（我方持有，资产）
    CLIENT = "CLIENT"        # 客户子钱包（负债）
    AGENT = "AGENT"          # 一代钱包（外部数据，仅参照）


class BalanceType(StrEnum):
    PREPAID = "PREPAID"      # 预存余额
    CREDIT = "CREDIT"        # 可用授信


class Direction(StrEnum):
    IN = "IN"                # 进
    OUT = "OUT"              # 出


class BizType(StrEnum):
    TOPUP = "TOPUP"          # 充值
    RECHARGE = "RECHARGE"    # 加款（客户钱包 → 广告账户）
    SPEND = "SPEND"          # 消耗
    REBATE = "REBATE"        # 返点（仅内部可见）
    REFUND = "REFUND"        # 退款
    ADJUST = "ADJUST"        # 调账
    TRANSFER = "TRANSFER"    # 打给一代


BIZ_TYPE_LABEL = {
    BizType.TOPUP: "充值",
    BizType.RECHARGE: "加款",
    BizType.SPEND: "消耗",
    BizType.REBATE: "返点",
    BizType.REFUND: "退款",
    BizType.ADJUST: "调账",
    BizType.TRANSFER: "打给一代",
}

# 客户侧永远看不到的业务类型
CLIENT_HIDDEN_BIZ_TYPES = {BizType.REBATE}


class PaymentMethod(StrEnum):
    BANK_TRANSFER = "BANK_TRANSFER"    # 线下转账
    ONLINE = "ONLINE"                  # 在线支付（二期）
    THIRD_PARTY = "THIRD_PARTY"        # 三方代付


class PaymentChannel(StrEnum):
    """客户充值时选择的收款渠道（门户展示用）。

    线上三家只登记渠道名，支付回调尚未接入；真正能落单的只有线下汇款，
    否则客户点完拿不到收款页，单据也只能永远挂在待支付。
    """

    YEEPAY = "YEEPAY"            # 易宝支付
    AIRWALLEX = "AIRWALLEX"      # 空中云汇
    WORLDFIRST = "WORLDFIRST"    # 万里汇
    OFFLINE = "OFFLINE"          # 线下汇款


PAYMENT_CHANNEL_LABEL = {
    PaymentChannel.YEEPAY: "易宝支付",
    PaymentChannel.AIRWALLEX: "空中云汇",
    PaymentChannel.WORLDFIRST: "万里汇",
    PaymentChannel.OFFLINE: "线下汇款",
}

# 渠道 → 单据上的支付方式。线上渠道保留真实分类，方便以后接回调。
PAYMENT_CHANNEL_METHOD = {
    PaymentChannel.YEEPAY: PaymentMethod.ONLINE,
    PaymentChannel.AIRWALLEX: PaymentMethod.THIRD_PARTY,
    PaymentChannel.WORLDFIRST: PaymentMethod.THIRD_PARTY,
    PaymentChannel.OFFLINE: PaymentMethod.BANK_TRANSFER,
}


class FundSourceType(StrEnum):
    SELF = "SELF"                        # 客户自有
    THIRD_PARTY = "THIRD_PARTY"          # 三方代付
    CHANNEL_ADVANCE = "CHANNEL_ADVANCE"  # 渠道垫资


class TopUpStatus(StrEnum):
    PENDING_PAY = "PENDING_PAY"          # 待支付
    PENDING_CONFIRM = "PENDING_CONFIRM"  # 待确认
    RECEIVED = "RECEIVED"                # 已到账
    RETURNED = "RETURNED"                # 已退回
    REVERSED = "REVERSED"                # 已冲正
    # 线下汇款申请单（D3 改版）：发起 → 待审核 → 审核通过 / 已驳回
    PENDING_REVIEW = "PENDING_REVIEW"    # 待审核
    APPROVED = "APPROVED"                # 审核通过（钱已按审核结果入账/入授信）
    REJECTED = "REJECTED"                # 已驳回


class FundAccountType(StrEnum):
    """线下汇款落到哪个资金账户。"""

    CASH = "CASH"        # 现金账户（客户子钱包，预付余额）
    CREDIT = "CREDIT"    # 授信账户（额度账，不进钱包流水）


class FundOpType(StrEnum):
    INCREASE = "INCREASE"  # 加款
    DECREASE = "DECREASE"  # 减款


class RechargeStatus(StrEnum):
    PENDING_EXEC = "PENDING_EXEC"        # 待执行
    EXECUTING = "EXECUTING"              # 执行中
    AUDITING = "AUDITING"                # 审批中
    SUCCESS = "SUCCESS"                  # 成功
    REJECTED = "REJECTED"                # 已拒绝
    TO_VERIFY = "TO_VERIFY"              # 待核查（unknown）
    CANCELLED = "CANCELLED"              # 已撤销（人工放弃，冻结必须释放）


class OpenRequestStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"  # 待审批
    PENDING_SUBMIT = "PENDING_SUBMIT"      # 待提交
    AGENT_REVIEWING = "AGENT_REVIEWING"    # 一代审核中
    OPENED = "OPENED"                      # 已下户
    AGENT_REJECTED = "AGENT_REJECTED"      # 一代拒绝
    REJECTED = "REJECTED"                  # 审批驳回


class Platform(StrEnum):
    FB = "FB"
    GOOGLE = "GOOGLE"
    TIKTOK = "TIKTOK"
    OTHER = "OTHER"


PLATFORM_LABEL = {
    Platform.FB: "Facebook",
    Platform.GOOGLE: "Google",
    Platform.TIKTOK: "TikTok",
    Platform.OTHER: "其他",
}

# 当前业务只做 TikTok：新建开户申请 / 广告账户只允许这一个媒体。
# 枚举与 PLATFORM_LABEL 保留全部取值，存量 FB / Google 账户与账单继续可读可算，
# 只是不再允许新建（见 services/openreq.py 的写入口校验）。
ENABLED_PLATFORMS: tuple[Platform, ...] = (Platform.TIKTOK,)
ENABLED_PLATFORM_LABEL = {p: PLATFORM_LABEL[p] for p in ENABLED_PLATFORMS}


class OpenRequestSource(StrEnum):
    CLIENT = "CLIENT"      # 客户在门户自助申请
    DESK = "DESK"          # 内部代客户提交


OPEN_REQUEST_SOURCE_LABEL = {
    OpenRequestSource.CLIENT: "客户申请",
    OpenRequestSource.DESK: "后台代提交",
}


class OpenMethod(StrEnum):
    AUTO = "AUTO"          # 审批通过后自动下户（Mock RPA）
    MANUAL = "MANUAL"      # 人工录入账户 ID


OPEN_METHOD_LABEL = {
    OpenMethod.AUTO: "自动开户",
    OpenMethod.MANUAL: "人工开户",
}


class AdAccountStatus(StrEnum):
    NORMAL = "NORMAL"      # 正常
    LIMITED = "LIMITED"    # 受限
    BANNED = "BANNED"      # 封停


# ---------------------------------------------------------------- P3 中文标签
# 前端只用 status_label，不自己维护状态映射表（契约第 2 节）

TOPUP_STATUS_LABEL = {
    TopUpStatus.PENDING_PAY: "待支付",
    TopUpStatus.PENDING_CONFIRM: "待确认",
    TopUpStatus.RECEIVED: "已到账",
    TopUpStatus.RETURNED: "已退回",
    TopUpStatus.REVERSED: "已冲正",
    TopUpStatus.PENDING_REVIEW: "待审核",
    TopUpStatus.APPROVED: "审核通过",
    TopUpStatus.REJECTED: "已驳回",
}

FUND_ACCOUNT_TYPE_LABEL = {
    FundAccountType.CASH: "现金账户",
    FundAccountType.CREDIT: "授信账户",
}

FUND_OP_TYPE_LABEL = {
    FundOpType.INCREASE: "加款",
    FundOpType.DECREASE: "减款",
}

# 申请单还挂在"待处理"里的状态。PENDING_PAY / PENDING_CONFIRM 是改版前
# 门户自助与财务代录留下的存量单，同样要能被审核处理，否则永久挂死。
FUND_APP_PENDING_STATUSES = (
    TopUpStatus.PENDING_PAY,
    TopUpStatus.PENDING_CONFIRM,
    TopUpStatus.PENDING_REVIEW,
)

RECHARGE_STATUS_LABEL = {
    RechargeStatus.PENDING_EXEC: "待执行",
    RechargeStatus.EXECUTING: "执行中",
    RechargeStatus.AUDITING: "审批中",
    RechargeStatus.SUCCESS: "成功",
    RechargeStatus.REJECTED: "已拒绝",
    RechargeStatus.TO_VERIFY: "待核查",
    RechargeStatus.CANCELLED: "已撤销",
}

OPEN_REQUEST_STATUS_LABEL = {
    OpenRequestStatus.PENDING_APPROVAL: "待审批",
    OpenRequestStatus.PENDING_SUBMIT: "待提交",
    OpenRequestStatus.AGENT_REVIEWING: "一代审核中",
    OpenRequestStatus.OPENED: "已开户",
    OpenRequestStatus.AGENT_REJECTED: "一代拒绝",
    OpenRequestStatus.REJECTED: "已驳回",
}

PAYMENT_METHOD_LABEL = {
    PaymentMethod.BANK_TRANSFER: "线下转账",
    PaymentMethod.ONLINE: "在线支付",
    PaymentMethod.THIRD_PARTY: "三方代付",
}

FUND_SOURCE_LABEL = {
    FundSourceType.SELF: "客户自有",
    FundSourceType.THIRD_PARTY: "三方代付",
    FundSourceType.CHANNEL_ADVANCE: "渠道垫资",
}

# 资金总览卡片用：账户状态与币种中文。币种只做展示，不参与任何换算。
# 充值只做美金：系统不再提供人民币（CNY）币种。
AD_ACCOUNT_STATUS_LABEL = {
    AdAccountStatus.NORMAL: "正常",
    AdAccountStatus.LIMITED: "受限",
    AdAccountStatus.BANNED: "封停",
}

CURRENCY_LABEL = {
    "USD": "美元",
    "HKD": "港币",
}


# ---------------------------------------------------------------- 客户开户申请
# 开户申请页面的下拉选项。code 落库、label 展示；前端不再自己维护一份中文表。
OPEN_REGISTERED_PLACE_OPTIONS: dict[str, str] = {
    "CN": "中国大陆",
    "HK": "中国香港",
    "TW": "中国台湾",
    "SG": "新加坡",
    "US": "美国",
    "OTHER": "其他",
}

# 投放国家/地区：TikTok / Meta / Google 常用市场。按中文名排序展示。
OPEN_COUNTRY_OPTIONS: dict[str, str] = {
    "US": "美国",
    "CA": "加拿大",
    "MX": "墨西哥",
    "BR": "巴西",
    "GB": "英国",
    "DE": "德国",
    "FR": "法国",
    "IT": "意大利",
    "ES": "西班牙",
    "NL": "荷兰",
    "SE": "瑞典",
    "PL": "波兰",
    "TR": "土耳其",
    "SA": "沙特阿拉伯",
    "AE": "阿联酋",
    "JP": "日本",
    "KR": "韩国",
    "SG": "新加坡",
    "MY": "马来西亚",
    "TH": "泰国",
    "VN": "越南",
    "ID": "印度尼西亚",
    "PH": "菲律宾",
    "IN": "印度",
    "AU": "澳大利亚",
    "HK": "中国香港",
    "TW": "中国台湾",
    "OTHER": "其他",
}

OPEN_INDUSTRY_OPTIONS: dict[str, str] = {
    "Ecommerce": "电商",
    "Gaming": "游戏",
    "App": "应用",
    "Social": "社交",
    "Finance": "金融",
    "Education": "教育",
    "Travel": "旅游",
    "Health": "医疗健康",
    "Beauty": "美妆个护",
    "Fashion": "服饰鞋包",
    "Electronics": "3C 数码",
    "Home": "家居",
    "Food": "食品饮料",
    "Auto": "汽车",
    "Other": "其他",
}


# ---------------------------------------------------------------- 问题工单
# 客户在门户提交（/api/portal/tickets），管理端受理。状态只在这四处流转，
# 前端下拉与筛选全部取自这里的 label，不在页面里再维护一份中文表。


class TicketStatus(StrEnum):
    PENDING = "PENDING"          # 待处理（客户已提交，尚未有人接手）
    PROCESSING = "PROCESSING"    # 处理中
    RESOLVED = "RESOLVED"        # 已解决
    CLOSED = "CLOSED"            # 已关闭


TICKET_STATUS_LABEL = {
    TicketStatus.PENDING: "待处理",
    TicketStatus.PROCESSING: "处理中",
    TicketStatus.RESOLVED: "已解决",
    TicketStatus.CLOSED: "已关闭",
}


class TicketPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


TICKET_PRIORITY_LABEL = {
    TicketPriority.LOW: "低",
    TicketPriority.NORMAL: "普通",
    TicketPriority.HIGH: "紧急",
}

# 工单状态机：当前状态 → 允许流转到的状态（含自身，便于只改回复不改状态）。
# 结单（已解决 / 已关闭）可以重新打开；已关闭不能直接回到待处理。
TICKET_NEXT_STATUSES: dict[str, tuple[str, ...]] = {
    TicketStatus.PENDING: (
        TicketStatus.PENDING,
        TicketStatus.PROCESSING,
        TicketStatus.RESOLVED,
        TicketStatus.CLOSED,
    ),
    TicketStatus.PROCESSING: (
        TicketStatus.PROCESSING,
        TicketStatus.PENDING,
        TicketStatus.RESOLVED,
        TicketStatus.CLOSED,
    ),
    TicketStatus.RESOLVED: (
        TicketStatus.RESOLVED,
        TicketStatus.PROCESSING,
        TicketStatus.CLOSED,
    ),
    TicketStatus.CLOSED: (
        TicketStatus.CLOSED,
        TicketStatus.PROCESSING,
    ),
}


# ---------------------------------------------------------------- 客户申请（线索）
# 门户登录页的「开户申请」只收集销售能联系上客户的基础信息，不创建 Client / User。
# 管理端销售认领后自行在客户管理里建档，因此这里是一条独立的线索状态机。
class ClientLeadStatus(StrEnum):
    NEW = "NEW"              # 待联系
    CONTACTED = "CONTACTED"  # 已联系
    CONVERTED = "CONVERTED"  # 已转客户
    CLOSED = "CLOSED"        # 已关闭（无效 / 重复 / 不合作）


CLIENT_LEAD_STATUS_LABEL = {
    ClientLeadStatus.NEW: "待联系",
    ClientLeadStatus.CONTACTED: "已联系",
    ClientLeadStatus.CONVERTED: "已转客户",
    ClientLeadStatus.CLOSED: "已关闭",
}

# 线索状态机：已转客户是终点；关闭的线索允许重新打开。
CLIENT_LEAD_NEXT_STATUSES: dict[str, tuple[str, ...]] = {
    ClientLeadStatus.NEW: (
        ClientLeadStatus.NEW,
        ClientLeadStatus.CONTACTED,
        ClientLeadStatus.CLOSED,
    ),
    ClientLeadStatus.CONTACTED: (
        ClientLeadStatus.CONTACTED,
        ClientLeadStatus.CONVERTED,
        ClientLeadStatus.CLOSED,
    ),
    ClientLeadStatus.CONVERTED: (ClientLeadStatus.CONVERTED,),
    ClientLeadStatus.CLOSED: (
        ClientLeadStatus.CLOSED,
        ClientLeadStatus.NEW,
    ),
}
