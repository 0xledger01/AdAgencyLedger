from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import BigInteger, DateTime, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

SCALE = Decimal("0.0001")
SCALE_INT = 10000  # 金额一律按 4 位小数存整数，杜绝对账浮点误差


def money(value) -> Decimal:  # noqa: ANN001
    if value is None:
        return Decimal("0.0000")
    return Decimal(str(value)).quantize(SCALE, rounding=ROUND_HALF_UP)


class Money(TypeDecorator):
    """跨库精确金额类型：SQLite 存 BIGINT（×10000），Postgres 存 NUMERIC(20,4)。"""

    impl = Numeric(20, 4)
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Numeric(20, 4))

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        d = money(value)
        if dialect.name == "sqlite":
            return int(d * SCALE_INT)
        return d

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if dialect.name == "sqlite":
            return Decimal(int(value)) / SCALE_INT
        return money(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class IdMixin:
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    biz_no: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True, index=True)
