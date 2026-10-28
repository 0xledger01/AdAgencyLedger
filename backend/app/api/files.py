"""文件上传与受控读取。

铁律：**不把 upload_dir 挂 StaticFiles** —— 那会绕过身份与归属校验。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from ..models.file import FileObject
from ..models.user import Role, User
from ..services import storage
from ..services.errors import InvalidFile
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/api/files", tags=["文件"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _disposition(name: str) -> str:
    """中文文件名走 RFC 5987，避免浏览器按 latin-1 解码成乱码。"""
    ascii_fallback = name.encode("ascii", "ignore").decode() or "file"
    ascii_fallback = ascii_fallback.replace('"', "").replace("\\", "")
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(name)}"


@router.post("", response_model=dict, summary="上传文件（凭证 / 执照 / 身份证 / 资质）")
async def upload_file(
    db: DbSession,
    user: CurrentUser,
    file: UploadFile = File(...),
    biz_type: str = Form("OTHER"),
) -> dict:
    try:
        storage.normalize_biz_type(biz_type)
        data = await file.read()
        obj, deduped = storage.save(
            db,
            data=data,
            original_name=file.filename or "upload",
            declared_type=file.content_type,
            biz_type=biz_type,
            uploader_id=user.id,
            client_id=user.client_id if user.role is Role.CLIENT else None,
        )
    except InvalidFile as exc:
        raise _bad(exc) from exc
    db.commit()
    return {
        "id": obj.id,
        "key": obj.key,
        "url": storage.url_of(obj),
        "original_name": obj.original_name,
        "content_type": obj.content_type,
        "size": obj.size,
        "sha256": obj.sha256,
        "biz_type": obj.biz_type,
        "deduped": deduped,
    }


@router.get("/{key:path}", summary="读取文件（按归属校验，防路径穿越）")
def read_file(key: str, db: DbSession, user: CurrentUser) -> FileResponse:
    try:
        path = storage.safe_resolve(key)
    except InvalidFile as exc:
        raise _bad(exc) from exc

    obj: FileObject | None = storage.find_by_key(db, key.strip())
    if obj is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")

    if user.role is Role.CLIENT:
        if obj.client_id is None or obj.client_id != user.client_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无权读取该文件")

    return FileResponse(
        path,
        media_type=obj.content_type,
        headers={"Content-Disposition": _disposition(obj.original_name)},
    )
