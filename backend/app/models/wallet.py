from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, IdMixin, Money, TimestampMixin, money, utcnow
from .enums import BalanceType, BizType, Direction, OwnerType


class Wallet(Base, TimestampMixin):
    """四层资金模型里的三类钱包：平台 / 客户 / 一代。

    铁律 1：balance 只能由 WalletTransaction 产生，禁止直接修改。
    """

    __tablename__ = "wallet"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "currency", "balance_type", name="uq_wallet_owner_ccy"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_type: Mapped[OwnerType] = mapped_column(
        SAEnum(OwnerType, native_enum=False, length=16), nullable=False, index=True
    )
    owner_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # 平台钱包为 None
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    frozen: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    balance_type: Mapped[BalanceType] = mapped_column(
        SAEnum(BalanceType, native_enum=False, length=16), nullable=False, default=BalanceType.PREPAID
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_error: Mapped[str | None] = mapped_column(String(200), nullable=True)

    transactions: Mapped[list["WalletTransaction"]] = relationship(
        back_populates="wallet", order_by="WalletTransaction.seq", lazy="selectin"
    )

    @property
    def available(self) -> Decimal:
        """可分配余额 = 余额 − 冻结。未核销充值不入账，天然不计入。"""
        return money(self.balance) - money(self.frozen)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Wallet {self.owner_type}:{self.owner_id} {self.currency} {self.balance}>"


class WalletTransaction(Base):
    """资金流水 —— 只追加。任何纠错只能新增反向流水（冲正）。

    铁律 1 / PRD D3.1 硬规则 1：
    - 不可修改、不可删除（ORM 事件 + 数据库触发器双保险）
    - balance_after 必须连续无断号，断点即数据异常
    """

    __tablename__ = "wallet_transaction"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_wallet_tx_idem"),
        UniqueConstraint("wallet_id", "seq", name="uq_wallet_tx_seq"),
        Index("ix_wallet_tx_wallet_time", "wallet_id", "occurred_at"),
        Index("ix_wallet_tx_biz", "biz_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallet.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(nullable=False)  # 钱包内连续序号，从 1 开始

    direction: Mapped[Direction] = mapped_column(SAEnum(Direction, native_enum=False, length=8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    base_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)

    balance_before: Mapped[Decimal] = mapped_column(Money, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Money, nullable=False)

    biz_type: Mapped[BizType] = mapped_column(SAEnum(BizType, native_enum=False, length=16), nullable=False)
    ref_doctype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(nullable=True)
    biz_no: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    voucher: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(60), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    wallet: Mapped[Wallet] = relationship(back_populates="transactions")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tx#{self.id} w{self.wallet_id} seq{self.seq} {self.direction} {self.amount} -> {self.balance_after}>"
