"""上传文件登记。

只存相对 key（如 `2026/10/9f3a.png`），不存绝对路径 —— 日后换 MinIO/OSS 只改 storage 层。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class FileObject(Base, TimestampMixin):
    __tablename__ = "file_object"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    uploader_id: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id"), nullable=True, index=True)
    # 客户上传时记归属；内部员工上传为 None（客户不可读）
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("client.id"), nullable=True, index=True
    )
    biz_type: Mapped[str] = mapped_column(String(32), nullable=False, default="OTHER", index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FileObject #{self.id} {self.key} {self.size}B>"
