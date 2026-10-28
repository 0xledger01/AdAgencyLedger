from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, IdMixin, Money, TimestampMixin, utcnow
from .enums import (
    AdAccountStatus,
    ClientLeadStatus,
    ClientStatus,
    FundAccountType,
    FundOpType,
    FundSourceType,
    PaymentMethod,
    Platform,
    TopUpStatus,
)


class AgentChannel(Base, IdMixin, TimestampMixin):
    """一级代理渠道。"""

    __tablename__ = "agent_channel"

    name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)  # Yatop / 山恒 / ...
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    settled: Mapped[bool] = mapped_column(default=True, nullable=False)  # 是否已接入 RPA
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)


class Client(Base, IdMixin, TimestampMixin):
    __tablename__ = "client"

    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    credit_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    wechat: Mapped[str | None] = mapped_column(String(60), nullable=True)

    platforms: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 逗号分隔 FB,GOOGLE
    industry: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_regions: Mapped[str | None] = mapped_column(String(120), nullable=True)
    monthly_spend_expect: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    settle_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    source_channel: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # 入驻材料与主体信息
    registered_place: Mapped[str | None] = mapped_column(String(120), nullable=True)
    founded_at: Mapped[str | None] = mapped_column(String(20), nullable=True)
    license_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    idcard_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    qualification_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    settle_intent: Mapped[str | None] = mapped_column(String(20), nullable=True)  # PREPAID / CREDIT

    # 运营客户管理（P6）：CRM 对照与归属
    crm_name: Mapped[str | None] = mapped_column(String(200), nullable=True)   # CRM 里的客户名
    crm_code: Mapped[str | None] = mapped_column(String(60), nullable=True)    # CRM 客户编号
    ae_owner: Mapped[str | None] = mapped_column(String(60), nullable=True)    # 负责 AE
    created_by: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 创建人

    sales_owner: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # 以下三项对客户角色完全不可读
    cost_ratio: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    rebate_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    channel: Mapped[str | None] = mapped_column(String(60), nullable=True)

    credit_limit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    credit_used: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    payment_terms: Mapped[int] = mapped_column(default=0, nullable=False)

    status: Mapped[ClientStatus] = mapped_column(
        SAEnum(ClientStatus, native_enum=False, length=20), nullable=False, default=ClientStatus.PENDING_REVIEW, index=True
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)  # 驳回重提 +1，历史版本不覆盖
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    ad_accounts: Mapped[list["AdAccount"]] = relationship(back_populates="client", lazy="selectin")


class AdAccount(Base, IdMixin, TimestampMixin):
    __tablename__ = "ad_account"

    account_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # 媒体侧账户 ID
    platform: Mapped[Platform] = mapped_column(SAEnum(Platform, native_enum=False, length=16), nullable=False)
    bm_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    channel: Mapped[str | None] = mapped_column(String(60), nullable=True)

    balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))  # 媒体侧余额
    daily_limit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    # 账户时区：开户时由开户单带入，列表里要按账户所在时区看余额/消耗日切
    timezone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[AdAccountStatus] = mapped_column(
        SAEnum(AdAccountStatus, native_enum=False, length=16), nullable=False, default=AdAccountStatus.NORMAL
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped[Client] = relationship(back_populates="ad_accounts")


class ClientBcid(Base, TimestampMixin):
    """客户 BCID 绑定表（「BC 管理」）。

    两种来源：
    - HISTORY：从广告账户 `bm_id`、开户单 `bm_id` / `bc_ids` 自动归纳出的历史绑定；
    - MANUAL：内部在「BC 管理」手工添加的绑定。

    同一客户最多一个默认项，开户申请默认预选它。手工添加同一个号时把历史行
    升级为 MANUAL，不产生重复行（`uq_client_bcid` 兜底）。
    """

    __tablename__ = "client_bcid"
    __table_args__ = (
        UniqueConstraint("client_id", "bcid", name="uq_client_bcid"),
        Index("ix_client_bcid_client_default", "client_id", "is_default"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    bcid: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="MANUAL")
    # 历史绑定的出处：AD_ACCOUNT / OPEN_REQUEST，手工添加为 MANUAL
    source_detail: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # 显式删除留墓碑：历史归纳会跳过它，避免「删掉后刷新又回来」。
    # 不动广告账户与开户单，也不物理删行，便于追溯是谁删的。
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClientLead(Base, IdMixin, TimestampMixin):
    """门户登录页提交的开户申请（销售线索）。

    只收基础联系信息，**不创建 Client / User**：
    销售在管理端看到后先电话 / 微信联系，确认合作后再去「客户管理」正式建档。
    这样既不会让未审核主体混进客户列表，也不会有未验证的登录账号。
    """

    __tablename__ = "client_lead"

    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contact_name: Mapped[str] = mapped_column(String(60), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    wechat: Mapped[str | None] = mapped_column(String(60), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)

    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="TIKTOK")
    monthly_budget_expect: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String(40), nullable=False, default="PORTAL_SELF")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=str(ClientLeadStatus.NEW), index=True
    )
    owner: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class ClientTopUp(Base, IdMixin, TimestampMixin):
    """客户充值 / 线下汇款申请单。铁律：审核通过前不得动账。

    改版（线下汇款列表）后这张表同时承载：
    - 资金账户类型：现金账户（客户子钱包）/ 授信账户（额度账）
    - 操作类型：加款 / 减款
    - 申请金额 = 到账金额（USD）；门户充值的应付金额按央行中间价折 CNY
    - 内部代录仍按同币种 1:1；门户单落 exchange_rate / rate_source 快照
    - 钱包只做美金：服务端仍拒绝 currency == CNY 的到账
    存量 TP 单只有加款语义，新字段走默认值 CASH / INCREASE。
    """

    __tablename__ = "client_topup"

    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    payment_method: Mapped[PaymentMethod] = mapped_column(
        SAEnum(PaymentMethod, native_enum=False, length=20), nullable=False
    )
    # 门户充值选择的收款渠道（易宝 / 空中云汇 / 万里汇 / 线下汇款）。
    # 存量单为 NULL，展示时回落到 payment_method。
    payment_channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fund_source_type: Mapped[FundSourceType] = mapped_column(
        SAEnum(FundSourceType, native_enum=False, length=20), nullable=False, default=FundSourceType.SELF
    )
    voucher: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[TopUpStatus] = mapped_column(
        SAEnum(TopUpStatus, native_enum=False, length=20), nullable=False, default=TopUpStatus.PENDING_PAY, index=True
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- 线下汇款申请单字段（D3 改版） ----
    fund_account_type: Mapped[FundAccountType] = mapped_column(
        SAEnum(FundAccountType, native_enum=False, length=16),
        nullable=False,
        default=FundAccountType.CASH,
    )
    op_type: Mapped[FundOpType] = mapped_column(
        SAEnum(FundOpType, native_enum=False, length=16),
        nullable=False,
        default=FundOpType.INCREASE,
    )
    txn_no: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 银行/渠道交易编号
    payable_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    payable_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    received_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    applied_by: Mapped[str | None] = mapped_column(String(60), nullable=True)   # 申请人
    reviewed_by: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 审核人
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 汇率来源：央行中间价（含报价日）/ 手工填写，供审核人核对
    rate_source: Mapped[str | None] = mapped_column(String(160), nullable=True)


class SpendDaily(Base, TimestampMixin):
    __tablename__ = "spend_daily"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    ad_account_id: Mapped[int] = mapped_column(ForeignKey("ad_account.id"), nullable=False, index=True)
    spend: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    base_spend: Mapped[Decimal] = mapped_column(Money, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="IMPORT")

    # ---- 消耗报表结构（D6 改版）----
    # 总消耗 = GMV Max + BC。老数据只有 spend，迁移时回填到 BC。
    gmv_max_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    gmv_max_net_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    bc_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    bc_net_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    # 现金 / 赠款是 BC 消耗的两种资金来源，二者之和通常等于 BC 总消耗
    bc_cash_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    bc_grant_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    # 投放指标只挂在 BC 侧（GMV Max 报表不提供曝光/点击）
    impressions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    conversions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
