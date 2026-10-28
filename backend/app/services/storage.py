"""文件存储层：校验 → 落盘 → 登记；读取时做归属与路径穿越校验。

三条硬规则（契约 P3.5 第 1 节不变式 9）：
1. 不信 Content-Type、不信扩展名，以**内容魔数**为准；
2. 先校验后落盘，不落一个不合规的文件；
3. key → 绝对路径必须 resolve 后仍在 upload_dir 之内，否则拒绝。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.base import utcnow
from ..models.file import FileObject
from .errors import InvalidFile

MAX_SIZE = 5 * 1024 * 1024
BIZ_TYPES = {"TOPUP_VOUCHER", "LICENSE", "IDCARD", "QUALIFICATION", "OTHER"}
FILE_URL_PREFIX = "/api/files/"

# 扩展名白名单（只用于挑存储后缀，不作为类型判据）
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

# 魔数 → (规范 content_type, 规范扩展名)
_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"%PDF-", "application/pdf", ".pdf"),
]

_KEY_RE = re.compile(r"^\d{4}/\d{2}/[0-9a-f]{32}\.(jpg|jpeg|png|webp|pdf)$")


def sniff(head: bytes) -> tuple[str, str] | None:
    """按内容魔数判定真实类型。返回 (content_type, 扩展名) 或 None。"""
    for magic, ctype, ext in _MAGIC:
        if head.startswith(magic):
            return ctype, ext
    # WEBP: RIFF....WEBP
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def normalize_biz_type(value: str | None) -> str:
    biz = (value or "OTHER").strip().upper()
    if biz not in BIZ_TYPES:
        raise InvalidFile(f"biz_type 取值非法：{value}（可选 {sorted(BIZ_TYPES)}）")
    return biz


def safe_resolve(key: str) -> Path:
    """key → 绝对路径。任何越界尝试一律拒绝（含 `..`、绝对路径、盘符、空 key）。"""
    raw = unquote(key or "").strip()
    if not raw:
        raise InvalidFile("文件 key 不能为空")
    # 归一化后再查一次，挡住 %2e%2e%2f 之类的编码穿越
    normalized = unquote(raw)
    if ".." in normalized.replace("\\", "/").split("/"):
        raise InvalidFile("文件 key 非法")
    if Path(normalized).is_absolute() or re.match(r"^[a-zA-Z]:", normalized):
        raise InvalidFile("文件 key 非法")

    root = settings.upload_dir.resolve()
    target = (root / normalized).resolve()
    if target != root and root not in target.parents:
        raise InvalidFile("文件 key 越界")
    return target


def save(
    session: Session,
    *,
    data: bytes,
    original_name: str,
    declared_type: str | None,
    biz_type: str | None,
    uploader_id: int | None,
    client_id: int | None,
) -> tuple[FileObject, bool]:
    """校验并落盘。返回 (FileObject, 是否复用已有)。"""
    if not data:
        raise InvalidFile("文件为空")
    if len(data) > MAX_SIZE:
        raise InvalidFile(f"文件超过 5MB（实际 {len(data) / 1024 / 1024:.2f}MB）")

    name = (original_name or "upload").strip()
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise InvalidFile(
            f"文件类型不允许：{ext or '无扩展名'}（仅支持 {', '.join(sorted(ALLOWED_EXT))}）"
        )

    detected = sniff(data[:32])
    if detected is None:
        raise InvalidFile("文件内容不是允许的图片/PDF（已按内容魔数校验，与扩展名无关）")
    content_type, real_ext = detected

    biz = normalize_biz_type(biz_type)
    digest = hashlib.sha256(data).hexdigest()

    # 同内容 + 同业务类型 + **同归属** 才复用。
    # 归属必须参与判重：否则客户上传一张和内部员工（client_id=None）或别的客户
    # 相同的图，会命中别人的记录、拿到一个自己读不了（403）的 URL，看起来"上传成功"其实用不了。
    existing = session.execute(
        select(FileObject).where(
            FileObject.sha256 == digest,
            FileObject.biz_type == biz,
            FileObject.client_id.is_(None)
            if client_id is None
            else FileObject.client_id == client_id,
        )
    ).scalar_one_or_none()
    if existing is not None and safe_resolve(existing.key).exists():
        return existing, True

    now = utcnow()
    key = f"{now:%Y/%m}/{uuid.uuid4().hex}{real_ext}"
    target = safe_resolve(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    obj = FileObject(
        key=key,
        original_name=name[:255],
        content_type=content_type,
        size=len(data),
        sha256=digest,
        uploader_id=uploader_id,
        client_id=client_id,
        biz_type=biz,
    )
    session.add(obj)
    session.flush()
    return obj, False


def url_of(obj: FileObject) -> str:
    return f"{FILE_URL_PREFIX}{obj.key}"


def find_by_key(session: Session, key: str) -> FileObject | None:
    return session.execute(select(FileObject).where(FileObject.key == key)).scalar_one_or_none()


def is_file_url(value: str | None) -> bool:
    return bool(value) and str(value).startswith(FILE_URL_PREFIX)


def key_from_url(value: str | None) -> str:
    if not is_file_url(value):
        raise InvalidFile("凭证必须是上传接口返回的文件地址（/api/files/...）")
    assert value is not None
    return value[len(FILE_URL_PREFIX):]


def exists_on_disk(value: str | None) -> bool:
    """凭证指向的文件是否还在磁盘上。历史遗留的非 /api/files/ 路径一律视为「不做校验」。"""
    if not is_file_url(value):
        return True
    try:
        key = key_from_url(value)
    except InvalidFile:
        return False
    try:
        return safe_resolve(key).is_file()
    except InvalidFile:
        return False
