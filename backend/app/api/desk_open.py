"""D2 开户管理（契约第 4 节）：列表 / 代提交 / 审批 / 驳回 / 提交 / 批量提交 / Mock 回执。

铁律 7：未审批通过的开户单一律进不了 RPA 队列 —— 拦截在服务端。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..models.client import AdAccount, Client
from ..models.docs import OpenRequest
from ..models.enums import CLIENT_STATUS_LABEL, OPEN_REQUEST_STATUS_LABEL
from ..models.user import User
from ..services import openreq as svc_open
from ..services.rbac import Perm
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["开户管理"], dependencies=[Depends(require_desk)])


def _label(order: OpenRequest) -> str:
    return OPEN_REQUEST_STATUS_LABEL.get(order.status, str(order.status))


def _get(db, request_id: int) -> OpenRequest:  # noqa: ANN001
    order = db.get(OpenRequest, request_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "开户单不存在")
    return order


@router.get("/open-requests/{request_id}", response_model=dict, summary="开户单详情（含客户资质与已有账户）")
def open_request_detail(
    request_id: int, db: DbSession, user: User = require_perm(Perm.OPEN_REQUEST_VIEW)
) -> dict:
    """审批页要看的材料：主体一致性、行业可投、是否重复开户 —— 都靠这里。"""
    order = _get(db, request_id)
    client = db.get(Client, order.client_id)
    accounts = (
        db.query(AdAccount).filter(AdAccount.client_id == order.client_id).all()
        if client
        else []
    )

    return {
        "item": svc_open.serialize(order, client),
        "client": None if client is None else {
            "id": client.id,
            "company_name": client.company_name,
            "credit_code": client.credit_code,
            "registered_place": client.registered_place,
            "founded_at": client.founded_at,
            "industry": client.industry,
            "platforms": client.platforms,
            "website": client.website,
            "contact_name": client.contact_name,
            "phone": client.phone,
            "email": client.email,
            "status": str(client.status),
            "status_label": CLIENT_STATUS_LABEL.get(client.status, str(client.status)),
            "license_url": client.license_url,
            "idcard_url": client.idcard_url,
            "qualification_url": client.qualification_url,
        },
        "existing_accounts": [
            {
                "id": a.id,
                "account_id": a.account_id,
                "platform": str(a.platform),
                "status": str(a.status),
                "currency": a.currency,
            }
            for a in accounts
        ],
    }


@router.get("/open-requests", response_model=dict, summary="开户单列表")
def list_open_requests(
    db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_VIEW),
    status_f: str | None = Query(None, alias="status"),
    client_id: int | None = None,
    platform: str | None = None,
    tab: str | None = Query(None, pattern="^(all|pending)$"),
    q: str | None = None,
    biz_no: str | None = None,
    client_q: str | None = None,
    subject: str | None = None,
    handler: str | None = None,
    channel: str | None = None,
    account_id: str | None = None,
    source: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return svc_open.list_open_requests(
        db, status_f=status_f, client_id=client_id, platform=platform,
        tab=tab, q=q, biz_no=biz_no, client_q=client_q, subject=subject,
        handler=handler, channel=channel, account_id=account_id, source=source,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )


class OpenRequestIn(BaseModel):
    client_id: int
    platform: str
    bm_id: str | None = Field(None, max_length=64)
    timezone: str | None = Field(None, max_length=60)
    currency: str = "USD"
    daily_limit: Decimal = Decimal("0")
    subject: str | None = Field(None, max_length=200)
    apply_count: int = Field(1, ge=1, le=20)
    remark: str | None = Field(None, max_length=500)


@router.post("/open-requests", response_model=dict, summary="代客户提交开户申请")
def create_open_request(
    body: OpenRequestIn, db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_SUBMIT),
) -> dict:
    order = svc_open.create_open_request(
        db,
        client_id=body.client_id,
        platform=body.platform,
        bm_id=body.bm_id,
        timezone=body.timezone,
        currency=body.currency,
        daily_limit=body.daily_limit,
        subject=body.subject,
        apply_count=body.apply_count,
        remark=body.remark,
        operator=user.display_name,
    )
    client = db.get(Client, order.client_id)
    db.commit()
    return svc_open.serialize(order, client)


class ChecksIn(BaseModel):
    checks: dict = Field(default_factory=dict)
    remark: str | None = Field(None, max_length=500)
    # 客户申请场景：审批通过即自动开户（Mock RPA）；老的两段式不传该字段
    auto_complete: bool = False
    account_ids: list[str] = Field(default_factory=list)


@router.post("/open-requests/{request_id}/approve", response_model=dict, summary="审批（5 项核对清单）")
def approve_open_request(
    request_id: int, body: ChecksIn, db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_APPROVE),
) -> dict:
    order = svc_open.approve(
        db, request_id, checks=body.checks, remark=body.remark, operator=user.display_name,
        auto_complete=body.auto_complete, account_ids=body.account_ids,
    )
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "status_label": _label(order),
        "completed_count": int(order.completed_count or 0),
        "account_ids": svc_open.serialize(order, db.get(Client, order.client_id))["account_ids"],
    }


class OpenRejectIn(BaseModel):
    reason: str | None = Field(None, max_length=500)


@router.post("/open-requests/{request_id}/reject", response_model=dict, summary="审批驳回")
def reject_open_request(
    request_id: int, body: OpenRejectIn, db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_APPROVE),
) -> dict:
    order = svc_open.reject(
        db, request_id, reason=body.reason, operator=user.display_name
    )
    db.commit()
    return {"id": order.id, "status": str(order.status), "status_label": _label(order)}


@router.post("/open-requests/{request_id}/submit", response_model=dict, summary="提交到一代（RPA 队列）")
def submit_open_request(
    request_id: int, db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_SUBMIT),
) -> dict:
    order = svc_open.submit(db, request_id, operator=user.display_name)
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "status_label": _label(order),
        "agent_receipt": order.agent_receipt,
    }


class BatchSubmitIn(BaseModel):
    ids: list[int] = Field(min_length=1)


@router.post("/open-requests/batch-submit", response_model=dict, summary="批量提交到一代")
def batch_submit(
    body: BatchSubmitIn, db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_SUBMIT),
) -> dict:
    result = svc_open.batch_submit(db, body.ids, operator=user.display_name)
    db.commit()
    return result


class ReceiptIn(BaseModel):
    result: str
    account_id: str | None = Field(None, max_length=64)
    account_ids: list[str] = Field(default_factory=list)
    reason: str | None = Field(None, max_length=500)


@router.post("/open-requests/{request_id}/receipt", response_model=dict, summary="Mock 一代回执")
def open_request_receipt(
    request_id: int, body: ReceiptIn, db: DbSession,
    user: User = require_perm(Perm.OPEN_REQUEST_APPROVE),
) -> dict:
    order, ad_account_id = svc_open.apply_receipt(
        db, request_id, result=body.result,
        account_id=body.account_id, account_ids=body.account_ids,
        reason=body.reason, operator=user.display_name,
    )
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "status_label": _label(order),
        "ad_account_id": ad_account_id,
    }
