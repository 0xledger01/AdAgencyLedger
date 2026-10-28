"""D3 充值管理（契约第 3 节）：列表 / 代录 / 确认到账 / 退回 / 冲正 / 调账。

钱一分都不能凭空来：确认到账才入账，冲正只加红字，调账必须双签 + 幂等单号。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ..models.base import money
from ..models.client import Client, ClientTopUp
from ..models.docs import AuditLog
from ..models.enums import (
    FUND_SOURCE_LABEL,
    PAYMENT_METHOD_LABEL,
    TOPUP_STATUS_LABEL,
    BizType,
    Direction,
    FundSourceType,
    OwnerType,
    PaymentMethod,
    TopUpStatus,
)
from ..models.user import User
from ..models.wallet import Wallet, WalletTransaction
from ..services import limits
from ..services import storage
from ..services import topup as svc_topup
from ..services.anonymize import display_name, display_text
from ..services.errors import InvalidAmount, StateError
from ..services.ledger import get_or_create_wallet, post
from ..services.rbac import Perm
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["充值管理"], dependencies=[Depends(require_desk)])

PENDING_STATUSES = (TopUpStatus.PENDING_PAY, TopUpStatus.PENDING_CONFIRM)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _item(topup: ClientTopUp, client: Client | None) -> dict:
    return {
        "id": topup.id,
        "biz_no": topup.biz_no,
        "client_id": topup.client_id,
        "client_name": client.company_name if client else None,
        "amount": str(money(topup.amount)),
        "currency": topup.currency,
        "payment_method": str(topup.payment_method),
        "payment_method_label": PAYMENT_METHOD_LABEL.get(
            topup.payment_method, str(topup.payment_method)
        ),
        "fund_source_type": str(topup.fund_source_type),
        "fund_source_label": FUND_SOURCE_LABEL.get(
            topup.fund_source_type, str(topup.fund_source_type)
        ),
        "status": str(topup.status),
        "status_label": TOPUP_STATUS_LABEL.get(topup.status, str(topup.status)),
        "voucher": topup.voucher,
        # 凭证可读性（契约 P3.5 4.4）：前端据此决定显示缩略图 / 标红 / 破折号
        "voucher_url": topup.voucher if storage.is_file_url(topup.voucher) else None,
        "has_voucher": bool(topup.voucher),
        "voucher_missing": bool(topup.voucher)
        and storage.is_file_url(topup.voucher)
        and not storage.exists_on_disk(topup.voucher),
        "payer": topup.payer,
        "remark": topup.remark,
        "reject_reason": topup.reject_reason,
        "confirmed_by": topup.confirmed_by,
        "confirmed_at": _dt(topup.confirmed_at),
        "created_at": _dt(topup.created_at),
    }


def _parse_status(value: str) -> TopUpStatus:
    try:
        return TopUpStatus(value)
    except ValueError as exc:
        raise StateError(f"状态取值非法：{value}") from exc


def _conditions(
    status_f: str | None, client_id: int | None, keyword: str | None,
    date_from: date | None, date_to: date | None,
) -> list:
    conds = []
    if status_f:
        conds.append(ClientTopUp.status == _parse_status(status_f))
    if client_id:
        conds.append(ClientTopUp.client_id == client_id)
    if keyword:
        like = f"%{keyword.strip()}%"
        conds.append(
            or_(
                ClientTopUp.biz_no.like(like),
                Client.company_name.like(like),
                ClientTopUp.payer.like(like),
            )
        )
    if date_from:
        conds.append(ClientTopUp.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        conds.append(ClientTopUp.created_at <= datetime.combine(date_to, time.max))
    return conds


def _amount_by_currency(db, conds: list) -> tuple[int, list[dict]]:  # noqa: ANN001
    rows = db.execute(
        select(
            ClientTopUp.currency,
            func.coalesce(func.sum(ClientTopUp.amount), 0),
            func.count(),
        )
        .join(Client, Client.id == ClientTopUp.client_id)
        .where(*conds)
        .group_by(ClientTopUp.currency)
    ).all()
    count = sum(int(n) for _ccy, _amt, n in rows)
    return count, [
        {"currency": ccy, "amount": str(money(amt))} for ccy, amt, _n in rows
    ]


@router.get("/topups", response_model=dict, summary="充值单列表 + 金额汇总")
def list_topups(
    db: DbSession,
    user: User = require_perm(Perm.TOPUP_VIEW),
    status_f: str | None = Query(None, alias="status"),
    client_id: int | None = None,
    keyword: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    conds = _conditions(status_f, client_id, keyword, date_from, date_to)
    stmt = (
        select(ClientTopUp, Client)
        .join(Client, Client.id == ClientTopUp.client_id)
        .where(*conds)
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(ClientTopUp.created_at.desc(), ClientTopUp.id.desc())
        .limit(limit).offset(offset)
    ).all()

    pending_count, pending_amount = _amount_by_currency(
        db, [*conds, ClientTopUp.status.in_(PENDING_STATUSES)]
    )
    _n, received_amount = _amount_by_currency(
        db, [*conds, ClientTopUp.status == TopUpStatus.RECEIVED]
    )

    return {
        "total": int(total),
        "items": [_item(t, c) for t, c in rows],
        "summary": {
            "pending_count": pending_count,
            "pending_amount": pending_amount,
            "received_amount": received_amount,
        },
    }


class TopUpIn(BaseModel):
    client_id: int
    amount: Decimal
    currency: str = "USD"
    payment_method: str = PaymentMethod.BANK_TRANSFER
    payer: str | None = None
    fund_source_type: str = FundSourceType.SELF
    voucher: str | None = None
    remark: str | None = Field(None, max_length=500)


@router.post("/topups", response_model=dict, summary="财务代录充值单")
def create_topup(
    body: TopUpIn, db: DbSession, user: User = require_perm(Perm.TOPUP_CONFIRM)
) -> dict:
    topup = svc_topup.create_topup(
        db,
        client_id=body.client_id,
        amount=body.amount,
        currency=body.currency,
        payment_method=body.payment_method,
        payer=body.payer,
        fund_source_type=body.fund_source_type,
        voucher=body.voucher,
        remark=body.remark,
        operator=user.display_name,
    )
    if topup.status is TopUpStatus.PENDING_PAY:
        # services/topup 按"有没有凭证"决定状态；财务代录的业务语义是"钱已收到、等财务确认"，
        # 因此无凭证也落 PENDING_CONFIRM（PENDING_PAY 只留给门户自助支付）。
        # 否则契约 3.5「仅 PENDING_CONFIRM 可退回」这条永远走不到。
        topup.status = TopUpStatus.PENDING_CONFIRM
        db.flush()
    client = db.get(Client, topup.client_id)
    db.commit()
    return _item(topup, client)


@router.get("/topups/{topup_id}", response_model=dict, summary="充值单详情 + 关联流水")
def topup_detail(
    topup_id: int, db: DbSession, user: User = require_perm(Perm.TOPUP_VIEW)
) -> dict:
    topup = db.get(ClientTopUp, topup_id)
    if topup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "充值单不存在")

    txs = db.execute(
        select(WalletTransaction)
        .where(
            WalletTransaction.ref_doctype == "client_topup",
            WalletTransaction.ref_id == topup_id,
        )
        .order_by(WalletTransaction.occurred_at, WalletTransaction.id)
    ).scalars().all()

    return {
        "item": _item(topup, db.get(Client, topup.client_id)),
        "ledger": [
            {
                "id": tx.id,
                "seq": tx.seq,
                "direction": str(tx.direction),
                "amount": str(money(tx.amount)),
                "currency": tx.currency,
                "balance_after": str(money(tx.balance_after)),
                "biz_type": str(tx.biz_type),
                "operator": display_name(tx.operator),
                "remark": display_text(tx.remark),
                "occurred_at": _dt(tx.occurred_at),
            }
            for tx in txs
        ],
    }


class ConfirmIn(BaseModel):
    remark: str | None = Field(None, max_length=500)
    # 无凭证特例（契约 P3.5 4.3）：不少于 5 字，否则拒绝
    no_voucher_reason: str | None = Field(None, max_length=300)
    # 大额二次确认：第一次不带 force 只返回提示，确认后带 force=true 重发
    force: bool = False


@router.post("/topups/{topup_id}/confirm", response_model=dict, summary="确认到账（幂等 + 凭证校验 + 大额二次确认）")
def confirm_topup(
    topup_id: int, body: ConfirmIn, db: DbSession,
    user: User = require_perm(Perm.TOPUP_CONFIRM),
) -> dict:
    topup_obj = db.get(ClientTopUp, topup_id)
    if topup_obj is None:
        raise StateError(f"充值单不存在（id={topup_id}）")

    # 凭证闸口先于大额闸口：无凭证是硬拒绝，不该先给"请二次确认"的错觉
    if topup_obj.status is not TopUpStatus.RECEIVED:
        if not topup_obj.voucher and len((body.no_voucher_reason or "").strip()) < 5:
            raise StateError(
                "无凭证不得确认到账，如确属线下现金等特殊情况请填写原因（不少于 5 字）"
            )
        if not storage.exists_on_disk(topup_obj.voucher) and storage.is_file_url(topup_obj.voucher):
            raise StateError("凭证文件已丢失，请重新上传或登记异常后再确认到账")

    # 大额闸口：达到阈值先返回提示，不入账（DESIGN §8）
    gate = limits.check_large_amount(db, topup_obj, force=body.force)
    if gate is not None:
        return gate

    topup = svc_topup.confirm_topup(
        db, topup_id, operator=user.display_name, remark=body.remark,
        no_voucher_reason=body.no_voucher_reason,
    )
    if body.force:
        db.add(
            AuditLog(
                actor=user.display_name,
                action="TOPUP_CONFIRM_FORCE",
                doctype="client_topup",
                doc_id=topup.id,
                after=f"amount={money(topup.amount)} {topup.currency}",
                reason="大额二次确认后入账",
            )
        )
    tx = db.execute(
        select(WalletTransaction).where(
            WalletTransaction.idempotency_key == f"topup:{topup.id}"
        )
    ).scalar_one()
    wallet = db.get(Wallet, tx.wallet_id)
    assert wallet is not None
    db.commit()
    return {
        "id": topup.id,
        "status": str(topup.status),
        "status_label": TOPUP_STATUS_LABEL.get(topup.status, str(topup.status)),
        "tx_id": tx.id,
        "balance": str(money(wallet.balance)),
        "available": str(wallet.available),
        "no_voucher": not bool(topup_obj.voucher),
    }


class RejectIn(BaseModel):
    reason: str | None = Field(None, max_length=300)


@router.post("/topups/{topup_id}/reject", response_model=dict, summary="退回充值单")
def reject_topup(
    topup_id: int, body: RejectIn, db: DbSession,
    user: User = require_perm(Perm.TOPUP_CONFIRM),
) -> dict:
    reason = (body.reason or "").strip()
    if not reason:
        raise StateError("退回必须填写原因")
    topup = svc_topup.reject_topup(
        db, topup_id, operator=user.display_name, reason=reason
    )
    db.commit()
    return {
        "id": topup.id,
        "status": str(topup.status),
        "status_label": TOPUP_STATUS_LABEL.get(topup.status, str(topup.status)),
    }


class ReverseIn(BaseModel):
    reason: str | None = Field(None, max_length=300)
    second_approver: str | None = Field(None, max_length=60)


@router.post("/topups/{topup_id}/reverse", response_model=dict, summary="冲正（双签，只加红字）")
def reverse_topup(
    topup_id: int, body: ReverseIn, db: DbSession,
    user: User = require_perm(Perm.TOPUP_ADJUST),
) -> dict:
    approver = (body.second_approver or "").strip()
    if not approver:
        raise StateError("冲正必须填写第二签人")
    if approver == user.display_name:
        raise StateError("冲正需两人确认：第二签人不能是操作人本人")

    reason = (body.reason or "").strip()
    if not reason:
        raise StateError("冲正必须填写原因")

    topup = svc_topup.reverse_topup(
        db, topup_id, operator=user.display_name, reason=reason
    )
    # 现金加款的原流水键是 topup:<id>，减款是 fund-out:<id>，两条都要能冲正
    origin = db.execute(
        select(WalletTransaction)
        .where(
            WalletTransaction.idempotency_key.in_(
                [f"topup:{topup.id}", f"fund-out:{topup.id}"]
            )
        )
        .order_by(WalletTransaction.id)
    ).scalar_one_or_none()
    reverse_tx = None
    if origin is not None:
        reverse_tx = db.execute(
            select(WalletTransaction).where(
                WalletTransaction.idempotency_key == f"reverse:{origin.id}"
            )
        ).scalar_one_or_none()

    wallet = db.execute(
        select(Wallet).where(
            Wallet.owner_type == OwnerType.CLIENT,
            Wallet.owner_id == topup.client_id,
            Wallet.currency == topup.currency,
        )
    ).scalar_one_or_none()
    db.commit()
    return {
        "id": topup.id,
        "status": str(topup.status),
        "status_label": TOPUP_STATUS_LABEL.get(topup.status, str(topup.status)),
        "reverse_tx_id": reverse_tx.id if reverse_tx else None,
        "balance": str(money(wallet.balance)) if wallet else None,
    }


# ------------------------------------------------------------------ 调账

class AdjustIn(BaseModel):
    amount: Decimal
    direction: Literal["IN", "OUT"]
    reason: str | None = Field(None, max_length=300)
    request_no: str | None = Field(None, max_length=60)
    second_approver: str | None = Field(None, max_length=60)


@router.post("/clients/{client_id}/adjust", response_model=dict, summary="调账（双签 + 幂等单号）")
def adjust_balance(
    client_id: int, body: AdjustIn, db: DbSession,
    user: User = require_perm(Perm.TOPUP_ADJUST),
) -> dict:
    amount = money(body.amount)
    if amount <= 0:
        raise InvalidAmount("调账金额必须大于 0（方向由 direction 决定）")
    request_no = (body.request_no or "").strip()
    if not request_no:
        raise StateError("调账必须提供 request_no 作为幂等键")
    reason = (body.reason or "").strip()
    if not reason:
        raise StateError("调账必须填写原因")
    approver = (body.second_approver or "").strip()
    if not approver:
        raise StateError("调账必须填写第二签人")
    if approver == user.display_name:
        raise StateError("调账需两人确认：第二签人不能是操作人本人")

    client = db.get(Client, client_id)
    if client is None:
        raise StateError(f"客户不存在（id={client_id}）")

    wallet = get_or_create_wallet(
        db, owner_type=OwnerType.CLIENT, owner_id=client_id,
        currency=client.settle_currency,
    )
    assert isinstance(wallet, Wallet)
    tx, _created = post(
        db,
        wallet_id=wallet.id,
        direction=Direction(body.direction),
        amount=amount,
        biz_type=BizType.ADJUST,
        idempotency_key=f"adjust:{request_no}",
        ref_doctype="client_adjust",
        ref_id=None,
        operator=user.display_name,
        remark=f"{reason}（第二签：{approver}）",
    )
    db.commit()
    return {
        "tx_id": tx.id,
        "balance": str(money(wallet.balance)),
        "available": str(wallet.available),
    }
