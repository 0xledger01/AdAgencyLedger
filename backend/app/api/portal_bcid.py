"""客户门户 · BC 管理。

客户端自助维护自己的 BCID 绑定：
- 列出绑定过（历史）或手工添加的 BCID；
- 设置开户后默认绑定项（同一客户唯一）；
- 手工添加 / 删除（删除留墓碑，刷新不会自己回来）。

铁律 3：客户身份只从登录态反查，接口不接受 client_id 参数。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..models.docs import AuditLog
from ..services import bcid as bcid_svc
from ..services.errors import StateError
from .deps import DbSession, PortalUser

router = APIRouter(prefix="/api/portal", tags=["BC 管理"])


class BcidIn(BaseModel):
    bcid: str = Field(min_length=1, max_length=64)
    set_default: bool = False


class DefaultIn(BaseModel):
    is_default: bool = True


def _audit(
    db,  # noqa: ANN001
    user,  # noqa: ANN001
    action: str,
    row_id: int,
    before: dict | None,
    after: dict | None,
) -> None:
    db.add(
        AuditLog(
            actor=user.display_name,
            action=action,
            doctype="client_bcid",
            doc_id=row_id,
            before=json.dumps(before, ensure_ascii=False) if before is not None else None,
            after=json.dumps(after, ensure_ascii=False) if after is not None else None,
        )
    )


@router.get("/bcids", response_model=dict, summary="我的 BC 管理：绑定列表")
def list_my_bcids(
    db: DbSession,
    user: PortalUser,
    keyword: str | None = Query(None, description="BCID 模糊匹配"),
    limit: int = Query(10, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    client_id = user.client_id  # 只认登录态，绝不读 query / body
    # 读取时对账：广告账户 / 开户单里出现过的 BCID 自动补成历史绑定
    bcid_svc.sync_history(db, client_id)
    rows, total = bcid_svc.list_page(
        db, client_id, keyword=keyword, limit=limit, offset=offset
    )
    data = {
        "items": [bcid_svc.serialize(r) for r in rows],
        "total": total,
        "summary": bcid_svc.summary(db, client_id),
    }
    db.commit()
    return data


@router.post("/bcids", response_model=dict, summary="我的 BC 管理：手工添加 BCID")
def add_my_bcid(
    body: BcidIn,
    db: DbSession,
    user: PortalUser,
) -> dict:
    client_id = user.client_id
    row, created, revived = bcid_svc.add_manual(
        db,
        client_id,
        bcid=body.bcid,
        operator=user.display_name,
        set_default=body.set_default,
    )
    _audit(
        db,
        user,
        "bcid.add" if created else ("bcid.restore" if revived else "bcid.promote"),
        row.id,
        None,
        {"bcid": row.bcid, "source": row.source, "is_default": row.is_default},
    )
    data = {
        "item": bcid_svc.serialize(row),
        "created": created,
        "revived": revived,
        "deduped": not created and not revived,
        "summary": bcid_svc.summary(db, client_id),
    }
    db.commit()
    return data


@router.post(
    "/bcids/{bcid_id}/default",
    response_model=dict,
    summary="我的 BC 管理：设置 / 取消默认绑定",
)
def set_my_default(
    bcid_id: int,
    body: DefaultIn,
    db: DbSession,
    user: PortalUser,
) -> dict:
    client_id = user.client_id
    try:
        row = bcid_svc.set_default(db, client_id, bcid_id, is_default=body.is_default)
    except StateError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    _audit(
        db,
        user,
        "bcid.default",
        row.id,
        None,
        {"bcid": row.bcid, "is_default": row.is_default},
    )
    data = {"item": bcid_svc.serialize(row), "summary": bcid_svc.summary(db, client_id)}
    db.commit()
    return data


@router.delete("/bcids/{bcid_id}", response_model=dict, summary="我的 BC 管理：删除绑定")
def delete_my_bcid(
    bcid_id: int,
    db: DbSession,
    user: PortalUser,
) -> dict:
    client_id = user.client_id
    try:
        row = bcid_svc.get_owned(db, client_id, bcid_id)
    except StateError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    before = bcid_svc.serialize(row)
    bcid_svc.delete(db, client_id, bcid_id)
    _audit(db, user, "bcid.delete", bcid_id, before, None)
    data = {"deleted": bcid_id, "summary": bcid_svc.summary(db, client_id)}
    db.commit()
    return data
