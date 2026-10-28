"""线下汇款申请单：审核前不动账、审核只动一次、授信与现金分开。"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import (
    AuditLog,
    Client,
    FundAccountType,
    FundOpType,
    OwnerType,
    TopUpStatus,
    WalletTransaction,
)
from backend.app.models.base import money
from backend.app.services import fund_app as svc
from backend.app.services.errors import StateError
from backend.app.services.ledger import get_or_create_wallet, post


def _wallet(db: Session, client: Client):  # noqa: ANN201
    w = get_or_create_wallet(db, owner_type=OwnerType.CLIENT, owner_id=client.id, currency="USD")
    db.flush()
    return w


def _fund(db: Session, client: Client, amount: str = "1000"):  # noqa: ANN201
    w = _wallet(db, client)
    post(
        db,
        wallet_id=w.id,
        direction="IN",
        amount=amount,
        biz_type="TOPUP",
        idempotency_key=f"seed:{client.id}:{amount}",
        operator="seed",
    )
    db.flush()
    return w


def test_create_application_does_not_touch_balance(db: Session, client: Client) -> None:
    w = _fund(db, client)
    before = money(w.balance)
    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="100",
        operator="tester",
    )
    assert app.status is TopUpStatus.PENDING_REVIEW
    assert app.biz_no and app.biz_no.startswith("CRT")
    assert money(w.balance) == before
    # 充值只做美金：应付金额与申请金额同币种、1:1
    assert app.payable_amount == money("100")
    assert app.payable_currency == "USD"
    assert app.exchange_rate == money("1")
    assert app.received_amount == money("100")


def test_cash_increase_requires_voucher_or_reason(db: Session, client: Client) -> None:
    _fund(db, client)
    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="100",
        operator="tester",
    )
    with pytest.raises(StateError):
        svc.approve_application(db, app.id, operator="finance")

    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="100",
        operator="tester",
    )
    ok = svc.approve_application(
        db, app.id, operator="finance", no_voucher_reason="线下现金收款，当面点清"
    )
    assert ok.status is TopUpStatus.APPROVED
    audit = db.execute(
        select(AuditLog).where(AuditLog.doctype == "client_topup", AuditLog.doc_id == app.id)
    ).scalars().all()
    assert any(a.action == "fund_app.approve" for a in audit)


def test_cash_increase_posts_once_and_replay_is_rejected(db: Session, client: Client) -> None:
    w = _fund(db, client)
    before = money(w.balance)
    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="100",
        operator="tester",
    )
    svc.approve_application(
        db, app.id, operator="finance", no_voucher_reason="线下现金收款，当面点清"
    )
    db.flush()
    assert money(w.balance) == before + money("100")

    # 已审核过的单不允许再审核一次（状态机闸口）
    with pytest.raises(StateError):
        svc.approve_application(
            db, app.id, operator="finance", no_voucher_reason="线下现金收款，当面点清"
        )

    # 即便有人把状态改回待审核，账本幂等键也不会二次入账
    app.status = TopUpStatus.PENDING_REVIEW
    db.flush()
    svc.approve_application(
        db, app.id, operator="finance", no_voucher_reason="线下现金收款，当面点清"
    )
    db.flush()
    assert money(w.balance) == before + money("100")
    txs = db.execute(
        select(WalletTransaction).where(
            WalletTransaction.ref_doctype == "client_topup",
            WalletTransaction.ref_id == app.id,
        )
    ).scalars().all()
    assert len(txs) == 1


def test_cash_decrease_rejects_insufficient_balance(db: Session, client: Client) -> None:
    _fund(db, client, "10")
    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.DECREASE,
        amount="50",
        operator="tester",
    )
    # 减款没有"客户应付"，应付金额固定 0
    assert app.payable_amount == money("0")
    assert app.exchange_rate == money("1")
    with pytest.raises(Exception) as exc:
        svc.approve_application(db, app.id, operator="finance")
    assert "可用余额不足" in str(exc.value)
    assert app.status is TopUpStatus.PENDING_REVIEW


def test_credit_increase_reduces_used_and_guards_overpay(db: Session, client: Client) -> None:
    client.credit_limit = money("5000")
    client.credit_used = money("300")
    db.flush()

    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CREDIT,
        op_type=FundOpType.INCREASE,
        amount="100",
        operator="tester",
    )
    svc.approve_application(db, app.id, operator="finance")
    db.flush()
    assert money(client.credit_used) == money("200")

    over = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CREDIT,
        op_type=FundOpType.INCREASE,
        amount="500",
        operator="tester",
    )
    with pytest.raises(StateError):
        svc.approve_application(db, over.id, operator="finance")


def test_credit_decrease_increases_used_and_guards_limit(db: Session, client: Client) -> None:
    client.credit_limit = money("1000")
    client.credit_used = money("900")
    db.flush()

    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CREDIT,
        op_type=FundOpType.DECREASE,
        amount="100",
        operator="tester",
    )
    svc.approve_application(db, app.id, operator="finance")
    db.flush()
    assert money(client.credit_used) == money("1000")

    over = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CREDIT,
        op_type=FundOpType.DECREASE,
        amount="1",
        operator="tester",
    )
    with pytest.raises(StateError):
        svc.approve_application(db, over.id, operator="finance")


def test_reject_requires_reason_and_leaves_ledger_alone(db: Session, client: Client) -> None:
    w = _fund(db, client)
    before = money(w.balance)
    app = svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="100",
        operator="tester",
    )
    with pytest.raises(StateError):
        svc.reject_application(db, app.id, operator="finance", reason="")

    ok = svc.reject_application(db, app.id, operator="finance", reason="流水金额不符")
    assert ok.status is TopUpStatus.REJECTED
    assert ok.reject_reason == "流水金额不符"
    assert money(w.balance) == before
    txs = db.execute(
        select(WalletTransaction).where(
            WalletTransaction.ref_doctype == "client_topup",
            WalletTransaction.ref_id == app.id,
        )
    ).scalars().all()
    assert txs == []


def test_list_pending_filter_and_labels(db: Session, client: Client) -> None:
    svc.create_application(
        db,
        client_id=client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="10",
        operator="tester",
    )
    data = svc.list_applications(db, pending_only=True, limit=50)
    assert data["total"] >= 1
    assert data["summary"]["pending_count"] >= 1
    item = data["items"][0]
    assert item["status_label"] == "待审核"
    assert item["fund_account_type_label"] == "现金账户"
    assert item["op_type_label"] == "加款"
