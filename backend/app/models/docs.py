from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin, Money, TimestampMixin, utcnow
from .enums import OpenRequestStatus, Platform, RechargeStatus


class OpenRequest(Base, IdMixin, TimestampMixin):
    """开户单。铁律 7：未人工审批不得进 RPA 队列。"""

    __tablename__ = "open_request"

    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    platform: Mapped[Platform] = mapped_column(SAEnum(Platform, native_enum=False, length=16), nullable=False)
    bm_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    daily_limit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))

    # 客户自助申请（P7）：申请主体、数量、来源。老数据由迁移补齐默认值。
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="DESK", index=True)
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    apply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_method: Mapped[str] = mapped_column(String(16), nullable=False, default="AUTO")
    applicant: Mapped[str | None] = mapped_column(String(60), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 客户开户申请的企业材料（按 CRM 原型：营业执照 → 自动带出注册地/信用代码/名称）
    license_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    credit_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registered_place: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 账户信息：投放国家 / 行业 / 绑定 BCID（逗号分隔）/ 产品链接
    industry: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_country: Mapped[str | None] = mapped_column(String(60), nullable=True)
    bc_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_link: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[OpenRequestStatus] = mapped_column(
        SAEnum(OpenRequestStatus, native_enum=False, length=20),
        nullable=False,
        default=OpenRequestStatus.PENDING_APPROVAL,
        index=True,
    )
    approver: Mapped[str | None] = mapped_column(String(60), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_receipt: Mapped[str | None] = mapped_column(Text, nullable=True)


class RechargeOrder(Base, IdMixin, TimestampMixin):
    """加款单。铁律 2：必须有人点击发起；铁律 6：unknown 未核查前不动账。"""

    __tablename__ = "recharge_order"

    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    ad_account_id: Mapped[int] = mapped_column(ForeignKey("ad_account.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[RechargeStatus] = mapped_column(
        SAEnum(RechargeStatus, native_enum=False, length=20),
        nullable=False,
        default=RechargeStatus.PENDING_EXEC,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # 交易编号（广告账户交易记录页的首列）：ADTR + 创建日期 + 6 位序号。
    # 与业务单号 biz_no（RC…）分开：前者对账/客服报号用，后者是单据主键。
    # 老库由 migrate.py 回填；新建单在 services/recharge.create_recharge 里落下。
    txn_no: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    approval_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    serial_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 谁点的这一下
    receipt: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentTopUp(Base, IdMixin, TimestampMixin):
    """打给一代（平台钱包 → 一代钱包）。"""

    __tablename__ = "agent_topup"

    channel: Mapped[str] = mapped_column(String(60), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    fund_account: Mapped[str | None] = mapped_column(String(120), nullable=True)
    voucher: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING_CONFIRM", index=True)


class Rebate(Base, IdMixin, TimestampMixin):
    """返点单。铁律 8：客户侧任何页面 / 接口 / 导出都不得出现。"""

    __tablename__ = "rebate"

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # 2026-10
    party_type: Mapped[str] = mapped_column(String(16), nullable=False)  # AGENT(应收) / CLIENT(应付)
    party_id: Mapped[int | None] = mapped_column(nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # IN=应收 / OUT=应付
    base_spend: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING_CONFIRM", index=True)


class Ticket(Base, IdMixin, TimestampMixin):
    __tablename__ = "ticket"

    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)


class SystemSetting(Base, TimestampMixin):
    """键值型系统开关（当前只有平台熔断 circuit:<PLATFORM>）。

    进程内字典只是缓存，库才是真身 —— 重启后熔断状态必须还在。
    """

    __tablename__ = "system_setting"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """所有写操作留痕：操作人 / 时间 / 变更前后值。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    doctype: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    doc_id: Mapped[int | None] = mapped_column(nullable=True)
    before: Mapped[str | None] = mapped_column(Text, nullable=True)
    after: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
