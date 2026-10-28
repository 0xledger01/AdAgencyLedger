"""BC 管理：客户 BCID 绑定列表 / 手工添加 / 默认项 / 删除。

管理端菜单已移除（BCID 由客户在门户 `/portal/bc` 自助维护），
本组接口保留给客服/运营兜底，不再有页面入口。

页面口径（对齐 CRM 原型）：
- 列出「当前客户」绑定过（历史）或手工添加的 BCID；
- 每行可勾选「开户后默认绑定此 BCID」，同一客户只保留一个默认项；
- 删除只删绑定关系，不改广告账户 `bm_id`，也不改历史开户单。

历史绑定在读取列表时做一次幂等对账（见 `services/bcid.py`）：
广告账户 / 开户单里出现过的 BCID 会自动补成 `HISTORY` 行。
删除留墓碑，已删除的号不会被对账重新补回来；手工重新添加会复活同一行。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from ..models.client import Client, ClientBcid
from ..models.docs import AuditLog
from ..models.enums import CLIENT_STATUS_LABEL
from ..models.user import User
from ..services import bcid as bcid_svc
from ..services.errors import StateError
from ..services.rbac import Perm, scope_sales_owner
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["BC 管理"], dependencies=[Depends(require_desk)])


class BcidIn(BaseModel):
    bcid: str = Field(min_length=1, max_length=64)
    set_default: bool = False


class DefaultIn(BaseModel):
    is_default: bool = True


def _visible_client(db, user: User, client_id: int) -> Client:  # noqa: ANN001
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户不存在")
    owner = scope_sales_owner(user)
    if owner and client.sales_owner != owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能维护自己名下客户的 BC 绑定")
    return client


def _client_brief(client: Client) -> dict:
    return {
        "id": client.id,
        "biz_no": client.biz_no,
        "company_name": client.company_name,
        "sales_owner": client.sales_owner,
        "status": str(client.status),
        "status_label": CLIENT_STATUS_LABEL.get(client.status, str(client.status)),
    }


def _audit(
    db,
    user: User,
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


@router.get("/bcids/clients", response_model=dict, summary="BC 管理：客户下拉（按权限收口）")
def bc_clients(
    db: DbSession,
    user: User = require_perm(Perm.BC_VIEW),
    keyword: str | None = Query(None, description="客户名称 / 编号 / CRM 编号，模糊匹配"),
    limit: int = Query(200, le=500),
) -> dict:
    stmt = select(Client)
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(Client.company_name.like(kw), Client.biz_no.like(kw), Client.crm_code.like(kw))
        )
    owner = scope_sales_owner(user)
    if owner:
        stmt = stmt.where(Client.sales_owner == owner)
    rows = db.execute(stmt.order_by(Client.id.desc()).limit(limit)).scalars().all()
    return {"items": [_client_brief(c) for c in rows]}


@router.get("/clients/{client_id}/bcids", response_model=dict, summary="BC 管理：绑定列表")
def list_bcids(
    client_id: int,
    db: DbSession,
    user: User = require_perm(Perm.BC_VIEW),
    keyword: str | None = Query(None, description="BCID 模糊匹配"),
    limit: int = Query(10, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    client = _visible_client(db, user, client_id)
    # 读取时对账：把广告账户 / 开户单里新出现的历史 BCID 补进来。
    # 这是一次幂等写入，提交后页面才能稳定看到「历史绑定」。
    bcid_svc.sync_history(db, client_id)
    rows, total = bcid_svc.list_page(
        db, client_id, keyword=keyword, limit=limit, offset=offset
    )
    data = {
        "client": _client_brief(client),
        "items": [bcid_svc.serialize(r) for r in rows],
        "total": total,
        "summary": bcid_svc.summary(db, client_id),
    }
    db.commit()
    return data


@router.post(
    "/clients/{client_id}/bcids",
    response_model=dict,
    summary="BC 管理：手工添加 BCID",
)
def add_bcid(
    client_id: int,
    body: BcidIn,
    db: DbSession,
    user: User = require_perm(Perm.BC_MANAGE),
) -> dict:
    _visible_client(db, user, client_id)
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
    "/clients/{client_id}/bcids/{bcid_id}/default",
    response_model=dict,
    summary="BC 管理：设置 / 取消默认绑定",
)
def set_default(
    client_id: int,
    bcid_id: int,
    body: DefaultIn,
    db: DbSession,
    user: User = require_perm(Perm.BC_MANAGE),
) -> dict:
    _visible_client(db, user, client_id)
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
    data = {
        "item": bcid_svc.serialize(row),
        "summary": bcid_svc.summary(db, client_id),
    }
    db.commit()
    return data


@router.delete(
    "/clients/{client_id}/bcids/{bcid_id}",
    response_model=dict,
    summary="BC 管理：删除绑定",
)
def delete_bcid(
    client_id: int,
    bcid_id: int,
    db: DbSession,
    user: User = require_perm(Perm.BC_MANAGE),
) -> dict:
    _visible_client(db, user, client_id)
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
