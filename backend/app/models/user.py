from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin, Money, TimestampMixin, utcnow


class Role(StrEnum):
    CLIENT = "CLIENT"      # 客户（外部）
    SALES = "SALES"        # 销售
    RISK = "RISK"          # 风控
    FINANCE = "FINANCE"    # 财务
    OPS = "OPS"            # 运营
    ADMIN = "ADMIN"        # 管理员


ROLE_LABEL = {
    Role.CLIENT: "客户",
    Role.SALES: "销售",
    Role.RISK: "风控",
    Role.FINANCE: "财务",
    Role.OPS: "运营",
    Role.ADMIN: "管理员",
}

INTERNAL_ROLES = {Role.SALES, Role.RISK, Role.FINANCE, Role.OPS, Role.ADMIN}


class User(Base, IdMixin, TimestampMixin):
    """登录账号。客户账号必须绑定且只绑定一个 Client。"""

    __tablename__ = "sys_user"

    username: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(60), nullable=False)
    role: Mapped[Role] = mapped_column(SAEnum(Role, native_enum=False, length=16), nullable=False, index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("client.id"), nullable=True, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 客户主账号/子账号
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ClientReview(Base):
    """入驻审核记录。谁审的、何时、什么动作、什么原因。"""

    __tablename__ = "client_review"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    step: Mapped[str] = mapped_column(String(20), nullable=False)      # REVIEW / RISK / LIMIT
    action: Mapped[str] = mapped_column(String(20), nullable=False)    # APPROVE / REJECT
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    operator: Mapped[str] = mapped_column(String(60), nullable=False)
    operator_role: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # 审核时条款快照
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)


class LedgerReconcile(Base):
    """流水核销标记。

    为什么不给 WalletTransaction 加 is_reconciled 字段：流水不可修改是铁律，
    核销是「财务事后确认」这一独立事实，必须单独记账。
    """

    __tablename__ = "ledger_reconcile"
    __table_args__ = (
        UniqueConstraint("tx_id", name="uq_reconcile_tx"),
        Index("ix_reconcile_period", "period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tx_id: Mapped[int] = mapped_column(ForeignKey("wallet_transaction.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # 2026-10
    reconciled_by: Mapped[str] = mapped_column(String(60), nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ClientApplication(Base, TimestampMixin):
    """入驻申请单：客户提交的原始内容 + 版本快照，驳回重提不覆盖历史。"""

    __tablename__ = "client_application"
    __table_args__ = (Index("ix_app_client_version", "client_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 主体
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    credit_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    registered_place: Mapped[str | None] = mapped_column(String(120), nullable=True)
    founded_at: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 联系人
    contact_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    contact_title: Mapped[str | None] = mapped_column(String(60), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    wechat: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # 业务
    platforms: Mapped[str | None] = mapped_column(String(60), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_regions: Mapped[str | None] = mapped_column(String(120), nullable=True)
    monthly_spend_expect: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    settle_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    # 材料
    license_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    idcard_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    qualification_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 结算意向
    settle_intent: Mapped[str | None] = mapped_column(String(20), nullable=True)  # PREPAID / CREDIT

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PortalTicket(Base, IdMixin, TimestampMixin):
    """门户问题工单：客户提交 → 管理端受理 / 回复 / 结单。

    只有客户能创建（/api/portal/tickets）；内部员工只改受理状态与回复，
    不涉及任何资金字段，也不允许删除工单（留痕比整洁重要）。
    """

    __tablename__ = "portal_ticket"

    client_id: Mapped[int] = mapped_column(ForeignKey("client.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    # 内部设置的优先级，客户侧不展示；默认普通，方便待处理队列先看紧急件。
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="NORMAL")
    # 回复是给客户看的正文；多次回复按时间追加，不覆盖历史。
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
