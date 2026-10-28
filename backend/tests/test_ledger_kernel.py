"""账本内核验收测试 —— 对应 PRD「七条铁律」与 D3.1 硬规则。

跑法：.venv/Scripts/python.exe -m pytest -v
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from backend.app.models import AdAccount, ClientTopUp, OwnerType, Platform, Wallet, WalletTransaction
from backend.app.models.base import money
from backend.app.services.errors import (
    IdempotencyConflict,
    ImmutableViolation,
    InsufficientBalance,
    InvalidAmount,
    LedgerChainBroken,
    StateError,
)
from backend.app.services.ledger import (
    client_total_balance,
    freeze,
    post,
    reverse,
    verify_wallet_chain,
    wallet_summary,
)
from backend.app.services.topup import confirm_topup, create_topup, reject_topup, reverse_topup


# ------------------------------------------------------------ 铁律 1：余额只能由流水产生

def test_new_wallet_balance_must_be_zero(db: Session, client) -> None:  # noqa: ANN001
    w = Wallet(owner_type=OwnerType.CLIENT, owner_id=client.id, currency="USD", balance=money("100"))
    db.add(w)
    with pytest.raises(ImmutableViolation):
        db.flush()
    db.rollback()


def test_direct_balance_mutation_rejected(db: Session, funded) -> None:  # noqa: ANN001
    funded.balance = money("999999")
    with pytest.raises(ImmutableViolation) as exc:
        db.flush()
    assert "账本流水" in str(exc.value)
    db.rollback()


def test_direct_frozen_mutation_rejected(db: Session, funded) -> None:  # noqa: ANN001
    funded.frozen = money("1")
    with pytest.raises(ImmutableViolation):
        db.flush()
    db.rollback()


def test_post_updates_balance_and_writes_tx(db: Session, funded) -> None:  # noqa: ANN001
    tx, created = post(
        db,
        wallet_id=funded.id,
        direction="OUT",
        amount="2500",
        biz_type="RECHARGE",
        idempotency_key="test:recharge:1",
        operator="ops",
    )
    assert created is True
    assert money(funded.balance) == money("7500")
    assert money(tx.balance_before) == money("10000")
    assert money(tx.balance_after) == money("7500")
    assert tx.seq == 2


# ------------------------------------------------------------ 流水不可改、不可删

def test_tx_cannot_be_updated(db: Session, funded) -> None:  # noqa: ANN001
    tx, _ = post(
        db, wallet_id=funded.id, direction="OUT", amount="100",
        biz_type="RECHARGE", idempotency_key="test:immutable:update",
    )
    tx.amount = money("1")
    with pytest.raises(ImmutableViolation) as exc:
        db.flush()
    assert exc.value.code == "LEDGER_IMMUTABLE"
    db.rollback()


def test_tx_cannot_be_deleted(db: Session, funded) -> None:  # noqa: ANN001
    tx, _ = post(
        db, wallet_id=funded.id, direction="OUT", amount="100",
        biz_type="RECHARGE", idempotency_key="test:immutable:delete",
    )
    db.delete(tx)
    with pytest.raises(ImmutableViolation):
        db.flush()
    db.rollback()


def test_raw_sql_update_tx_blocked_by_db_trigger(engine, db: Session, funded) -> None:  # noqa: ANN001
    post(
        db, wallet_id=funded.id, direction="OUT", amount="100",
        biz_type="RECHARGE", idempotency_key="test:raw:sql",
    )
    db.commit()

    with engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            conn.execute(text("UPDATE wallet_transaction SET amount = 1"))
        assert "append-only" in str(exc.value)
        conn.rollback()


# ------------------------------------------------------------ 幂等

def test_idempotent_replay_only_once(db: Session, funded) -> None:  # noqa: ANN001
    args = dict(
        wallet_id=funded.id, direction="OUT", amount="1000",
        biz_type="RECHARGE", idempotency_key="test:idem:1",
    )
    tx1, created1 = post(db, **args)
    tx2, created2 = post(db, **args)

    assert created1 is True and created2 is False
    assert tx1.id == tx2.id
    assert money(funded.balance) == money("9000"), "重复投递不得二次扣款"
    assert db.query(WalletTransaction).filter_by(wallet_id=funded.id).count() == 2


def test_idempotency_key_reuse_with_different_amount_rejected(db: Session, funded) -> None:  # noqa: ANN001
    post(db, wallet_id=funded.id, direction="OUT", amount="100",
         biz_type="RECHARGE", idempotency_key="test:idem:conflict")
    with pytest.raises(IdempotencyConflict):
        post(db, wallet_id=funded.id, direction="OUT", amount="200",
             biz_type="RECHARGE", idempotency_key="test:idem:conflict")


def test_idempotency_key_unique_at_db_level(db: Session, funded) -> None:  # noqa: ANN001
    tx = WalletTransaction(
        wallet_id=funded.id, seq=99, direction="IN", amount=money("1"), currency="USD",
        exchange_rate=money("1"), base_amount=money("1"),
        balance_before=money("0"), balance_after=money("1"),
        biz_type="TOPUP", idempotency_key="test:fund:1",
    )
    db.add(tx)
    with pytest.raises((IntegrityError, Exception)):
        db.flush()
    db.rollback()


# ------------------------------------------------------------ 余额链连续

def test_chain_is_continuous_after_operations(db: Session, funded) -> None:  # noqa: ANN001
    for i in range(5):
        post(db, wallet_id=funded.id, direction="OUT", amount="100",
             biz_type="RECHARGE", idempotency_key=f"test:chain:{i}")
    post(db, wallet_id=funded.id, direction="IN", amount="300",
         biz_type="TOPUP", idempotency_key="test:chain:in")

    assert verify_wallet_chain(db, funded.id) == 7
    assert money(funded.balance) == money("10000") - 500 + 300


def test_chain_detects_tampered_balance(db: Session, funded) -> None:  # noqa: ANN001
    post(db, wallet_id=funded.id, direction="OUT", amount="100",
         biz_type="RECHARGE", idempotency_key="test:chain:tamper")
    db.commit()

    # 绕过 ORM 和触发器，直接改钱包余额（模拟运维手工 UPDATE）
    db.execute(text("UPDATE wallet SET balance = balance + 100"))
    db.commit()
    db.expire_all()

    with pytest.raises(LedgerChainBroken) as exc:
        verify_wallet_chain(db, funded.id)
    assert "余额" in str(exc.value)


# ------------------------------------------------------------ 余额校验

def test_insufficient_balance_rejected(db: Session, funded) -> None:  # noqa: ANN001
    with pytest.raises(InsufficientBalance):
        post(db, wallet_id=funded.id, direction="OUT", amount="10001",
             biz_type="RECHARGE", idempotency_key="test:insufficient")
    assert money(funded.balance) == money("10000")


def test_invalid_amount_rejected(db: Session, funded) -> None:  # noqa: ANN001
    for bad in (0, -1, "0.00001"):
        with pytest.raises(InvalidAmount):
            post(db, wallet_id=funded.id, direction="IN", amount=bad,
                 biz_type="TOPUP", idempotency_key=f"test:bad:{bad}")


def test_frozen_amount_not_available(db: Session, funded) -> None:  # noqa: ANN001
    freeze(db, funded.id, "6000")
    assert funded.available == money("4000")
    with pytest.raises(InsufficientBalance):
        post(db, wallet_id=funded.id, direction="OUT", amount="5000",
             biz_type="RECHARGE", idempotency_key="test:frozen")


# ------------------------------------------------------------ 充值单：未确认不动账

def test_unconfirmed_topup_does_not_touch_balance(db: Session, client, wallet) -> None:  # noqa: ANN001
    t = create_topup(db, client_id=client.id, amount="8000", currency="USD", payer="周九")
    assert t.status == "PENDING_PAY"
    assert money(wallet.balance) == money("0")
    assert db.query(WalletTransaction).filter_by(wallet_id=wallet.id).count() == 0


def test_confirm_topup_posts_entry_and_is_idempotent(db: Session, client, wallet) -> None:  # noqa: ANN001
    t = create_topup(
        db, client_id=client.id, amount="8000", currency="USD", payer="周九", voucher="test/receipt.png"
    )
    confirm_topup(db, t.id, operator="财务-A")
    confirm_topup(db, t.id, operator="财务-B")  # 重复点，不能重复入账

    assert money(wallet.balance) == money("8000")
    assert db.query(WalletTransaction).filter_by(wallet_id=wallet.id).count() == 1


def test_reject_topup_requires_reason_and_no_entry(db: Session, client, wallet) -> None:  # noqa: ANN001
    t = create_topup(db, client_id=client.id, amount="500", currency="USD", voucher="a.png")
    with pytest.raises(StateError):
        reject_topup(db, t.id, operator="财务", reason="")
    reject_topup(db, t.id, operator="财务", reason="凭证模糊")
    assert money(wallet.balance) == money("0")


def test_reverse_topup_appends_red_entry(db: Session, client, wallet) -> None:  # noqa: ANN001
    t = create_topup(
        db, client_id=client.id, amount="2000", currency="USD", payer="周九", voucher="test/receipt.png"
    )
    confirm_topup(db, t.id, operator="财务")
    reverse_topup(db, t.id, operator="财务", reason="银行退票")

    assert money(wallet.balance) == money("0")
    rows = db.query(WalletTransaction).filter_by(wallet_id=wallet.id).order_by(WalletTransaction.seq).all()
    assert [r.direction for r in rows] == ["IN", "OUT"]
    assert rows[0].amount == rows[1].amount  # 原流水一个字没改
    assert t.status == "REVERSED"
    verify_wallet_chain(db, wallet.id)


def test_reverse_helper_keeps_chain_valid(db: Session, funded) -> None:  # noqa: ANN001
    tx, _ = post(db, wallet_id=funded.id, direction="OUT", amount="300",
                 biz_type="ADJUST", idempotency_key="test:rev:origin")
    reverse(db, tx=tx, operator="财务", reason="录错")
    assert money(funded.balance) == money("10000")
    verify_wallet_chain(db, funded.id)


# ------------------------------------------------------------ 客户总余额

def test_client_total_balance_includes_ad_accounts(db: Session, client, wallet) -> None:  # noqa: ANN001
    post(db, wallet_id=wallet.id, direction="IN", amount="5000",
         biz_type="TOPUP", idempotency_key="test:total:topup")
    db.add(AdAccount(account_id="act_t1", platform=Platform.FB, currency="USD",
                     client_id=client.id, balance=money("1200.50")))
    db.flush()

    assert money(client_total_balance(db, client.id)) == money("6200.50")


def test_wallet_summary_reports_available(db: Session, funded) -> None:  # noqa: ANN001
    freeze(db, funded.id, "1000")
    s = wallet_summary(db, funded.id)
    assert s["balance"] == "10000.0000"
    assert s["frozen"] == "1000.0000"
    assert s["available"] == "9000.0000"
