"""D3 充值管理（线下汇款 · 申请单）：列表 / 发起申请 / 审核通过 / 驳回 / 导出。

路由只做参数与权限，状态机与记账全部在 `services/fund_app.py`。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from ..models.client import Client
from ..models.enums import FundAccountType, FundOpType
from ..models.user import User
from ..services import fund_app as svc
from ..services.rbac import Perm
from .deps import DbSession, require_desk, require_perm

router = APIRouter(
    prefix="/api/desk", tags=["充值管理"], dependencies=[Depends(require_desk)]
)


@router.get("/fund-applications", response_model=dict, summary="线下汇款申请单列表")
def list_applications(
    db: DbSession,
    user: User = require_perm(Perm.TOPUP_VIEW),
    status_f: str | None = Query(None, alias="status"),
    pending: bool = Query(False, description="只看待处理（待审核/待确认/待支付）"),
    fund_account_type: str | None = Query(None, description="CASH=现金账户 / CREDIT=授信账户"),
    op_type: str | None = Query(None, description="INCREASE=加款 / DECREASE=减款"),
    bill_no: str | None = Query(None, description="账单 ID，模糊匹配"),
    txn_no: str | None = Query(None, description="交易编号，模糊匹配"),
    client_keyword: str | None = Query(None, description="客户名称 / 客户编号，模糊匹配"),
    applicant: str | None = Query(None, description="申请人，模糊匹配"),
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    return svc.list_applications(
        db,
        status_f=status_f,
        pending_only=pending,
        fund_account_type=fund_account_type,
        op_type=op_type,
        bill_no=bill_no,
        txn_no=txn_no,
        client_keyword=client_keyword,
        applicant=applicant,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


# 注意：/export 必须注册在 /{app_id} 之前，否则会被当成 id 解析
@router.get("/fund-applications/export", summary="导出线下汇款申请单 CSV")
def export_applications(
    db: DbSession,
    user: User = require_perm(Perm.TOPUP_VIEW),
    status_f: str | None = Query(None, alias="status"),
    pending: bool = Query(False),
    fund_account_type: str | None = None,
    op_type: str | None = None,
    bill_no: str | None = None,
    txn_no: str | None = None,
    client_keyword: str | None = None,
    applicant: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    data = svc.list_applications(
        db,
        status_f=status_f,
        pending_only=pending,
        fund_account_type=fund_account_type,
        op_type=op_type,
        bill_no=bill_no,
        txn_no=txn_no,
        client_keyword=client_keyword,
        applicant=applicant,
        date_from=date_from,
        date_to=date_to,
        limit=5000,
        offset=0,
    )
    return Response(
        content=svc.export_csv(data["items"]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="fund-applications.csv"'},
    )


@router.get("/fund-applications/{app_id}", response_model=dict, summary="申请单详情 + 关联流水")
def application_detail(
    app_id: int, db: DbSession, user: User = require_perm(Perm.TOPUP_VIEW)
) -> dict:
    app, client = svc.get_application(db, app_id)
    return {"item": svc.serialize(app, client), "ledger": svc.related_transactions(db, app_id)}


class ApplicationIn(BaseModel):
    client_id: int
    fund_account_type: Literal["CASH", "CREDIT"] = "CASH"
    op_type: Literal["INCREASE", "DECREASE"] = "INCREASE"
    amount: Decimal
    currency: str | None = Field(None, max_length=8)
    received_amount: Decimal | None = None
    txn_no: str | None = Field(None, max_length=64)
    payer: str | None = Field(None, max_length=120)
    voucher: str | None = Field(None, max_length=300)
    remark: str | None = Field(None, max_length=500)


@router.post("/fund-applications", response_model=dict, summary="发起线下汇款申请")
def create_application(
    body: ApplicationIn, db: DbSession, user: User = require_perm(Perm.TOPUP_APPLY)
) -> dict:
    app = svc.create_application(
        db,
        client_id=body.client_id,
        fund_account_type=FundAccountType(body.fund_account_type),
        op_type=FundOpType(body.op_type),
        amount=body.amount,
        currency=body.currency,
        received_amount=body.received_amount,
        txn_no=body.txn_no,
        payer=body.payer,
        voucher=body.voucher,
        remark=body.remark,
        operator=user.display_name,
    )
    client = svc.get_application(db, app.id)[1]
    item = svc.serialize(app, client)
    db.commit()
    return item


class ApproveIn(BaseModel):
    note: str | None = Field(None, max_length=300)
    # 实际到账与申请不一致时，由审核人按银行流水修正
    received_amount: Decimal | None = None
    # 现金加款无凭证特例：不少于 5 字，写进流水备注与审计
    no_voucher_reason: str | None = Field(None, max_length=300)


@router.post("/fund-applications/{app_id}/approve", response_model=dict, summary="审核通过（入账 / 入授信）")
def approve_application(
    app_id: int, body: ApproveIn, db: DbSession,
    user: User = require_perm(Perm.TOPUP_CONFIRM),
) -> dict:
    app = svc.approve_application(
        db,
        app_id,
        operator=user.display_name,
        note=body.note,
        received_amount=body.received_amount,
        no_voucher_reason=body.no_voucher_reason,
    )
    item = svc.serialize(app, db.get(Client, app.client_id))
    db.commit()
    return item


class RejectIn(BaseModel):
    reason: str | None = Field(None, max_length=300)


@router.post("/fund-applications/{app_id}/reject", response_model=dict, summary="驳回申请")
def reject_application(
    app_id: int, body: RejectIn, db: DbSession,
    user: User = require_perm(Perm.TOPUP_CONFIRM),
) -> dict:
    app = svc.reject_application(
        db, app_id, operator=user.display_name, reason=body.reason or ""
    )
    item = svc.serialize(app, db.get(Client, app.client_id))
    db.commit()
    return item
