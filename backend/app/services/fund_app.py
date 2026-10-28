"""线下汇款申请单：发起 → 待审核 → 审核通过（入账/入授信）/ 驳回。

铁律复述：
- 审核通过前一分钱都不动（待审核只是单据，不是余额）；
- 现金账户走 `services/ledger.py` 的 `post()`，流水只追加；
- 授信账户不是现金，改的是 `client.credit_used`（额度账），不进钱包流水；
- 加款（钱进客户）必须有凭证，或写明不少于 5 字的无凭证原因并留审计。

一张单只允许审核一次：状态离开待处理后再调审核接口直接 400，
同时账本幂等键 `topup:<id>` / `fund-out:<id>` 兜底，不会重复入账。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.client import Client, ClientTopUp
from ..models.docs import AuditLog
from ..models.enums import (
    FUND_ACCOUNT_TYPE_LABEL,
    FUND_APP_PENDING_STATUSES,
    FUND_OP_TYPE_LABEL,
    PAYMENT_CHANNEL_LABEL,
    PAYMENT_METHOD_LABEL,
    TOPUP_STATUS_LABEL,
    BizType,
    ClientStatus,
    Direction,
    FundAccountType,
    FundOpType,
    OwnerType,
    PaymentMethod,
    TopUpStatus,
)
from ..models.wallet import Wallet
from . import storage
from .anonymize import display_name, display_text
from .errors import InvalidAmount, StateError
from .ledger import get_or_create_wallet, post

APP_PREFIX = "CRT"  # 账单 ID 前缀，与线下汇款单据习惯保持一致

# 已入账 / 已审核通过：列表里都算"处理完"，但标签按实际状态给。
DONE_STATUSES = (TopUpStatus.RECEIVED, TopUpStatus.APPROVED)


def _biz_no(seq: int) -> str:
    return f"{APP_PREFIX}{utcnow():%Y%m%d}{seq:05d}"


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def status_label(status: TopUpStatus) -> str:
    return TOPUP_STATUS_LABEL.get(status, str(status))


def is_pending(status: TopUpStatus) -> bool:
    return status in FUND_APP_PENDING_STATUSES


def serialize(app: ClientTopUp, client: Client | None) -> dict:
    status = app.status
    account_type = app.fund_account_type or FundAccountType.CASH
    op_type = app.op_type or FundOpType.INCREASE
    received = money(app.received_amount)
    if received <= 0:
        received = money(app.amount)
    voucher = app.voucher
    channel_label = PAYMENT_CHANNEL_LABEL.get(app.payment_channel) if app.payment_channel else None
    return {
        "id": app.id,
        "biz_no": app.biz_no,
        "client_id": app.client_id,
        "client_name": client.company_name if client else None,
        "client_no": client.biz_no if client else None,
        "fund_account_type": str(account_type),
        "fund_account_type_label": FUND_ACCOUNT_TYPE_LABEL.get(account_type, str(account_type)),
        "op_type": str(op_type),
        "op_type_label": FUND_OP_TYPE_LABEL.get(op_type, str(op_type)),
        "txn_no": app.txn_no,
        "amount": str(money(app.amount)),
        "currency": app.currency,
        "payable_amount": str(money(app.payable_amount)),
        "payable_currency": app.payable_currency or "USD",
        "received_amount": str(received),
        "exchange_rate": str(money(app.exchange_rate) or Decimal("1")),
        "status": str(status),
        "status_label": status_label(status),
        "pending": is_pending(status),
        "voucher": voucher,
        "voucher_url": voucher if storage.is_file_url(voucher) else None,
        "has_voucher": bool(voucher),
        "voucher_missing": bool(voucher)
        and storage.is_file_url(voucher)
        and not storage.exists_on_disk(voucher),
        "payer": app.payer,
        "payment_method": str(app.payment_method),
        "payment_method_label": PAYMENT_METHOD_LABEL.get(
            app.payment_method, str(app.payment_method)
        ),
        "payment_channel": app.payment_channel,
        "payment_channel_label": channel_label
        or PAYMENT_METHOD_LABEL.get(app.payment_method, str(app.payment_method)),
        "remark": app.remark,
        "reject_reason": app.reject_reason,
        "review_note": app.review_note,
        "applied_by": app.applied_by,
        "reviewed_by": app.reviewed_by,
        "reviewed_at": _dt(app.reviewed_at),
        "confirmed_by": app.confirmed_by,
        "confirmed_at": _dt(app.confirmed_at),
        "created_at": _dt(app.created_at),
    }


def _parse_account_type(value: str | None) -> FundAccountType | None:
    if not value:
        return None
    try:
        return FundAccountType(value)
    except ValueError as exc:
        raise StateError(f"资金账户类型取值非法：{value}") from exc


def _parse_op_type(value: str | None) -> FundOpType | None:
    if not value:
        return None
    try:
        return FundOpType(value)
    except ValueError as exc:
        raise StateError(f"操作类型取值非法：{value}") from exc


def _parse_status(value: str | None) -> TopUpStatus | None:
    if not value:
        return None
    try:
        return TopUpStatus(value)
    except ValueError as exc:
        raise StateError(f"状态取值非法：{value}") from exc


def _amount_rows(session: Session, conds: list) -> list[dict]:
    rows = session.execute(
        select(ClientTopUp.currency, func.coalesce(func.sum(ClientTopUp.amount), 0), func.count())
        .join(Client, Client.id == ClientTopUp.client_id)
        .where(*conds)
        .group_by(ClientTopUp.currency)
    ).all()
    return [{"currency": ccy, "amount": str(money(amt)), "count": int(n)} for ccy, amt, n in rows]


def list_applications(
    session: Session,
    *,
    status_f: str | None = None,
    pending_only: bool = False,
    fund_account_type: str | None = None,
    op_type: str | None = None,
    bill_no: str | None = None,
    txn_no: str | None = None,
    client_keyword: str | None = None,
    applicant: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    conds: list = []
    st = _parse_status(status_f)
    if st is not None:
        conds.append(ClientTopUp.status == st)
    if pending_only:
        conds.append(ClientTopUp.status.in_(FUND_APP_PENDING_STATUSES))
    account = _parse_account_type(fund_account_type)
    if account is not None:
        conds.append(ClientTopUp.fund_account_type == account)
    op = _parse_op_type(op_type)
    if op is not None:
        conds.append(ClientTopUp.op_type == op)
    if bill_no and bill_no.strip():
        conds.append(ClientTopUp.biz_no.like(f"%{bill_no.strip()}%"))
    if txn_no and txn_no.strip():
        conds.append(ClientTopUp.txn_no.like(f"%{txn_no.strip()}%"))
    if client_keyword and client_keyword.strip():
        like = f"%{client_keyword.strip()}%"
        conds.append(or_(Client.company_name.like(like), Client.biz_no.like(like)))
    if applicant and applicant.strip():
        conds.append(ClientTopUp.applied_by.like(f"%{applicant.strip()}%"))
    if date_from:
        conds.append(ClientTopUp.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        conds.append(ClientTopUp.created_at <= datetime.combine(date_to, time.max))

    stmt = (
        select(ClientTopUp, Client)
        .join(Client, Client.id == ClientTopUp.client_id)
        .where(*conds)
    )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(ClientTopUp.created_at.desc(), ClientTopUp.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    status_rows = session.execute(
        select(ClientTopUp.status, func.count())
        .select_from(ClientTopUp)
        .join(Client, Client.id == ClientTopUp.client_id)
        .where(*conds)
        .group_by(ClientTopUp.status)
    ).all()
    counts = {str(s): int(n) for s, n in status_rows}
    pending_count = sum(int(n) for s, n in status_rows if is_pending(s))

    pending_amount = _amount_rows(
        session, [*conds, ClientTopUp.status.in_(FUND_APP_PENDING_STATUSES)]
    )
    approved_amount = _amount_rows(
        session, [*conds, ClientTopUp.status.in_(DONE_STATUSES)]
    )

    return {
        "total": int(total),
        "items": [serialize(app, client) for app, client in rows],
        "summary": {
            "counts": counts,
            "pending_count": pending_count,
            "pending_amount": pending_amount,
            "approved_amount": approved_amount,
        },
    }


def get_application(session: Session, app_id: int) -> tuple[ClientTopUp, Client | None]:
    app = session.get(ClientTopUp, app_id)
    if app is None:
        raise StateError(f"申请单不存在（id={app_id}）")
    return app, session.get(Client, app.client_id)


def related_transactions(session: Session, app_id: int) -> list[dict]:
    from ..models.wallet import WalletTransaction

    txs = session.execute(
        select(WalletTransaction)
        .where(
            WalletTransaction.ref_doctype == "client_topup",
            WalletTransaction.ref_id == app_id,
        )
        .order_by(WalletTransaction.occurred_at, WalletTransaction.id)
    ).scalars().all()
    return [
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
    ]


def create_application(
    session: Session,
    *,
    client_id: int,
    fund_account_type: FundAccountType | str,
    op_type: FundOpType | str,
    amount: Decimal | str | float,
    currency: str | None = None,
    received_amount: Decimal | str | float | None = None,
    txn_no: str | None = None,
    payer: str | None = None,
    voucher: str | None = None,
    remark: str | None = None,
    operator: str | None = None,
) -> ClientTopUp:
    """发起申请：只落单，不动账。"""
    amt = money(amount)
    if amt <= 0:
        raise InvalidAmount("申请金额必须大于 0")

    client = session.get(Client, client_id)
    if client is None:
        raise StateError(f"客户不存在（id={client_id}）")
    if client.status is not ClientStatus.ACTIVE:
        raise StateError(f"客户状态 {client.status} 不允许资金操作")

    try:
        account = FundAccountType(fund_account_type)
        op = FundOpType(op_type)
    except ValueError as exc:
        raise StateError(f"资金账户类型 / 操作类型取值非法：{exc}") from exc
    if account is FundAccountType.CREDIT and money(client.credit_limit) <= 0:
        raise StateError("该客户没有授信额度，不能走授信账户（请先在客户管理定额度）")

    ccy = (currency or client.settle_currency or "USD").upper()
    if ccy == "CNY":
        raise StateError("系统只支持美金充值，人民币充值已下线（请把客户结算币种改为 USD）")
    # 充值只做美金：不做汇率折算，应付金额与申请金额同币种、1:1。
    # 减款是往外退 / 占用额度，没有"客户应付"一说，固定 0。
    rate = Decimal("1.0000")
    payable = amt if op is FundOpType.INCREASE else Decimal("0.0000")

    received = money(received_amount) if received_amount is not None else amt
    if received <= 0:
        raise InvalidAmount("到账金额必须大于 0")

    app = ClientTopUp(
        client_id=client_id,
        amount=amt,
        currency=ccy,
        exchange_rate=rate,
        payment_method=PaymentMethod.BANK_TRANSFER,
        payer=payer or None,
        voucher=voucher or None,
        remark=remark or None,
        status=TopUpStatus.PENDING_REVIEW,
        fund_account_type=account,
        op_type=op,
        txn_no=(txn_no or "").strip() or None,
        payable_amount=payable,
        payable_currency=ccy,
        received_amount=received,
        applied_by=operator,
    )
    session.add(app)
    session.flush()
    app.biz_no = _biz_no(app.id)
    session.flush()
    return app


def _check_voucher(app: ClientTopUp, no_voucher_reason: str | None) -> str:
    """现金加款的风控闸口：没凭证必须写原因（≥5 字）并进审计。返回附加备注。"""
    reason = (no_voucher_reason or "").strip()
    if not app.voucher:
        if len(reason) < 5:
            raise StateError(
                "无凭证不得确认到账，如确属线下现金等特殊情况请填写原因（不少于 5 字）"
            )
        return f"｜无凭证确认：{reason}"
    if storage.is_file_url(app.voucher) and not storage.exists_on_disk(app.voucher):
        raise StateError("凭证文件已丢失，请重新上传或登记异常后再审核")
    return ""


def _client_wallet(session: Session, app: ClientTopUp, client: Client) -> Wallet:
    wallet = get_or_create_wallet(
        session,
        owner_type=OwnerType.CLIENT,
        owner_id=client.id,
        currency=app.currency,
    )
    assert isinstance(wallet, Wallet)
    return wallet


def approve_application(
    session: Session,
    app_id: int,
    *,
    operator: str,
    note: str | None = None,
    received_amount: Decimal | str | float | None = None,
    no_voucher_reason: str | None = None,
) -> ClientTopUp:
    app = session.get(ClientTopUp, app_id, with_for_update=True)
    if app is None:
        raise StateError(f"申请单不存在（id={app_id}）")
    if not is_pending(app.status):
        raise StateError(f"当前状态「{status_label(app.status)}」不允许审核")

    client = session.get(Client, app.client_id, with_for_update=True)
    if client is None:
        raise StateError(f"客户不存在（id={app.client_id}）")
    if client.status is not ClientStatus.ACTIVE:
        raise StateError(f"客户状态 {client.status} 不允许资金操作")

    if received_amount is not None:
        received = money(received_amount)
        if received <= 0:
            raise InvalidAmount("到账金额必须大于 0")
        app.received_amount = received
    amt = money(app.received_amount) if money(app.received_amount) > 0 else money(app.amount)

    account = app.fund_account_type or FundAccountType.CASH
    op = app.op_type or FundOpType.INCREASE
    extra = ""
    tx_id: int | None = None
    credit_before = money(client.credit_used)

    if account is FundAccountType.CASH:
        wallet = _client_wallet(session, app, client)
        if op is FundOpType.INCREASE:
            extra = _check_voucher(app, no_voucher_reason)
            direction = Direction.IN
            biz_type = BizType.TOPUP
            key = f"topup:{app.id}"
            remark = "线下汇款审核通过"
        else:
            direction = Direction.OUT
            biz_type = BizType.REFUND
            key = f"fund-out:{app.id}"
            remark = "线下汇款减款审核通过"
        if app.txn_no:
            remark = f"{remark}｜交易编号 {app.txn_no}"
        if note:
            remark = f"{remark}｜{note.strip()}"
        tx, _created = post(
            session,
            wallet_id=wallet.id,
            direction=direction,
            amount=amt,
            biz_type=biz_type,
            idempotency_key=key,
            ref_doctype="client_topup",
            ref_id=app.id,
            biz_no=app.biz_no,
            voucher=app.voucher,
            payer=app.payer,
            operator=operator,
            remark=f"{remark}{extra}",
            exchange_rate=money(app.exchange_rate) or Decimal("1"),
        )
        tx_id = tx.id
    elif op is FundOpType.INCREASE:
        # 授信账户加款 = 客户还款：减少已用授信，不动钱包现金
        if credit_before < amt:
            raise StateError(
                f"还款金额 {amt} 超过已用授信 {credit_before}，请核对后再审核"
            )
        client.credit_used = money(credit_before - amt)
    else:
        # 授信账户减款 = 占用授信：已用授信增加，不得超过额度
        limit = money(client.credit_limit)
        available = limit - credit_before
        if available < amt:
            raise StateError(
                f"授信可用额度不足：可用 {available}，申请 {amt}（额度 {limit}，已用 {credit_before}）"
            )
        client.credit_used = money(credit_before + amt)

    app.status = TopUpStatus.APPROVED
    app.reviewed_by = operator
    app.reviewed_at = utcnow()
    app.review_note = note or None
    app.confirmed_by = operator
    app.confirmed_at = utcnow()
    session.add(
        AuditLog(
            actor=operator,
            action="fund_app.approve",
            doctype="client_topup",
            doc_id=app.id,
            after=json.dumps(
                {
                    "biz_no": app.biz_no,
                    "fund_account_type": str(account),
                    "op_type": str(op),
                    "amount": str(money(app.amount)),
                    "received_amount": str(amt),
                    "currency": app.currency,
                    "tx_id": tx_id,
                    "credit_used_before": str(credit_before),
                    "credit_used_after": str(money(client.credit_used)),
                },
                ensure_ascii=False,
            ),
            reason=note or "线下汇款审核通过",
        )
    )
    session.flush()
    return app


def reject_application(
    session: Session, app_id: int, *, operator: str, reason: str
) -> ClientTopUp:
    app = session.get(ClientTopUp, app_id, with_for_update=True)
    if app is None:
        raise StateError(f"申请单不存在（id={app_id}）")
    if not is_pending(app.status):
        raise StateError(f"当前状态「{status_label(app.status)}」不允许驳回")
    text = (reason or "").strip()
    if not text:
        raise StateError("驳回必须填写原因")

    app.status = TopUpStatus.REJECTED
    app.reject_reason = text
    app.reviewed_by = operator
    app.reviewed_at = utcnow()
    app.review_note = text
    session.add(
        AuditLog(
            actor=operator,
            action="fund_app.reject",
            doctype="client_topup",
            doc_id=app.id,
            after=f"biz_no={app.biz_no} amount={money(app.amount)} {app.currency}",
            reason=text,
        )
    )
    session.flush()
    return app


EXPORT_COLUMNS = [
    ("id", "ID"),
    ("fund_account_type_label", "资金账户类型"),
    ("op_type_label", "操作类型"),
    ("biz_no", "账单ID"),
    ("txn_no", "交易编号"),
    ("client_name", "客户名称"),
    ("client_no", "客户编号"),
    ("amount", "申请金额"),
    ("currency", "币种"),
    ("payable_amount", "应付金额"),
    ("received_amount", "到账金额"),
    ("status_label", "状态"),
    ("applied_by", "申请人"),
    ("reviewed_by", "审核人"),
    ("created_at", "创建时间"),
]


def export_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in EXPORT_COLUMNS])
    for row in items:
        writer.writerow([row.get(key) if row.get(key) is not None else "" for key, _label in EXPORT_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")
