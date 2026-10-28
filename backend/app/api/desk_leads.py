"""客户申请（销售线索）管理端接口。

销售在「客户申请」里查看门户提交的基础信息并跟进：
认领 → 联系 → 标记已联系 / 已转客户 / 已关闭。
线索不是客户档案，转客户后仍需在「客户管理」里正式建档。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..models.user import Role, User
from ..services import leads as svc_leads
from ..services.errors import LedgerError
from ..services.rbac import Perm, scope_sales_owner
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["客户申请"], dependencies=[Depends(require_desk)])


def _guard(user: User, lead) -> None:  # noqa: ANN001
    """销售只能看自己认领的或还没人认领的线索。"""
    owner = scope_sales_owner(user)
    if owner and lead.owner not in (None, owner):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己认领的客户申请")


@router.get("/client-leads", response_model=dict, summary="客户申请列表")
def list_client_leads(
    db: DbSession,
    user: User = require_perm(Perm.CLIENT_LEAD_VIEW),
    status_f: str | None = Query(None, alias="status"),
    keyword: str | None = None,
    platform: str | None = None,
    owner: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(20, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return svc_leads.list_leads(
        db,
        status_f=status_f,
        keyword=keyword,
        platform=platform,
        owner=owner,
        date_from=date_from,
        date_to=date_to,
        sales_owner=scope_sales_owner(user),
        limit=limit,
        offset=offset,
    )


@router.get("/client-leads/{lead_id}", response_model=dict, summary="客户申请详情")
def client_lead_detail(
    lead_id: int, db: DbSession, user: User = require_perm(Perm.CLIENT_LEAD_VIEW)
) -> dict:
    try:
        lead = svc_leads.get_lead(db, lead_id)
    except LedgerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    _guard(user, lead)
    return svc_leads.serialize(lead)


class LeadHandleIn(BaseModel):
    status: str | None = Field(None, max_length=20)
    owner: str | None = Field(None, max_length=60)
    note: str | None = Field(None, max_length=2000)
    close_reason: str | None = Field(None, max_length=300)


@router.post("/client-leads/{lead_id}/handle", response_model=dict, summary="跟进客户申请")
def handle_client_lead(
    lead_id: int,
    body: LeadHandleIn,
    db: DbSession,
    user: User = require_perm(Perm.CLIENT_LEAD_HANDLE),
) -> dict:
    try:
        lead = svc_leads.get_lead(db, lead_id)
    except LedgerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    _guard(user, lead)

    owner = body.owner
    if user.role is Role.SALES:
        # 销售只能认领到自己名下，不能把线索挂到同事头上。
        if owner is not None and owner.strip() not in ("", user.display_name):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "只能认领到自己名下")
        if owner is not None:
            owner = user.display_name

    try:
        lead = svc_leads.update_lead(
            db,
            lead,
            status=body.status,
            owner=owner,
            note=body.note,
            close_reason=body.close_reason,
            actor=user.display_name,
        )
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return svc_leads.serialize(lead)
