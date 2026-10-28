"""账本内核 —— 全系统唯一的余额写入口。

设计要点：
- 所有余额变动 = 一条 WalletTransaction + wallet.balance 原子更新
- 幂等键唯一：同一动作重复投递只生效一次
- 每个钱包内部 seq 连续，balance_before/after 必须首尾相接
- 出账先校验可用余额（余额 − 冻结），不足直接拒绝
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.enums import BalanceType, BizType, Direction, OwnerType
from ..models.wallet import Wallet, WalletTransaction
from .errors import (
    IdempotencyConflict,
    InsufficientBalance,
    InvalidAmount,
    LedgerChainBroken,
    WalletNotFound,
)
from .guards import allow_balance_mutation


# ---------------------------------------------------------------- 钱包

def get_or_create_wallet(
    session: Session,
    *,
    owner_type: OwnerType,
    owner_id: int | None,
    currency: str,
    balance_type: BalanceType = BalanceType.PREPAID,
    create: bool = True,
) -> Wallet | None:
    stmt = select(Wallet).where(
        Wallet.owner_type == owner_type,
        Wallet.owner_id.is_(None) if owner_id is None else Wallet.owner_id == owner_id,
        Wallet.currency == currency,
        Wallet.balance_type == balance_type,
    )
    wallet = session.execute(stmt).scalar_one_or_none()
    if wallet or not create:
        return wallet
    wallet = Wallet(
        owner_type=owner_type,
        owner_id=owner_id,
        currency=currency,
        balance_type=balance_type,
        balance=Decimal("0"),
        frozen=Decimal("0"),
    )
    session.add(wallet)
    session.flush()
    return wallet


def require_wallet(session: Session, wallet_id: int, *, lock: bool = False) -> Wallet:
    wallet = session.get(Wallet, wallet_id, with_for_update=lock)
    if wallet is None:
        raise WalletNotFound(f"钱包不存在（wallet_id={wallet_id}）")
    return wallet


# ---------------------------------------------------------------- 记账

def post(
    session: Session,
    *,
    wallet_id: int,
    direction: Direction | str,
    amount: Decimal | float | int | str,
    biz_type: BizType | str,
    idempotency_key: str,
    ref_doctype: str | None = None,
    ref_id: int | None = None,
    biz_no: str | None = None,
    voucher: str | None = None,
    payer: str | None = None,
    operator: str | None = None,
    remark: str | None = None,
    exchange_rate: Decimal | float | int | str = "1",
    allow_negative: bool = False,
    occurred_at: datetime | None = None,
) -> tuple[WalletTransaction, bool]:
    """记一笔账。返回 (流水, 是否新建)；重复投递返回已有流水且 created=False。"""
    direction = Direction(direction)
    biz_type = BizType(biz_type)
    amt = money(amount)
    if amt <= 0:
        raise InvalidAmount(f"金额必须大于 0（收到 {amount}）")
    if not idempotency_key:
        raise IdempotencyConflict("幂等键不能为空：所有资金接口必须支持幂等")

    # 1) 幂等：先查
    existing = session.execute(
        select(WalletTransaction).where(WalletTransaction.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.wallet_id != wallet_id or money(existing.amount) != amt or existing.biz_type != biz_type:
            raise IdempotencyConflict(
                f"幂等键 {idempotency_key} 已被另一笔业务占用"
                f"（wallet={existing.wallet_id}/{existing.amount}/{existing.biz_type}）"
            )
        return existing, False

    # 2) 锁钱包
    wallet = require_wallet(session, wallet_id, lock=True)

    signed = amt if direction is Direction.IN else -amt
    before = money(wallet.balance)
    if direction is Direction.OUT and not allow_negative:
        available = before - money(wallet.frozen)
        if available < amt:
            raise InsufficientBalance(
                f"可用余额不足：需 {amt}，可用 {available}（余额 {before}，冻结 {money(wallet.frozen)}）"
            )
    after = money(before + signed)

    last_seq = session.execute(
        select(func.coalesce(func.max(WalletTransaction.seq), 0)).where(
            WalletTransaction.wallet_id == wallet.id
        )
    ).scalar_one()

    rate = money(exchange_rate) or Decimal("1")
    tx = WalletTransaction(
        wallet_id=wallet.id,
        seq=int(last_seq) + 1,
        direction=direction,
        amount=amt,
        currency=wallet.currency,
        exchange_rate=rate,
        base_amount=money(amt * rate),
        balance_before=before,
        balance_after=after,
        biz_type=biz_type,
        ref_doctype=ref_doctype,
        ref_id=ref_id,
        biz_no=biz_no,
        voucher=voucher,
        payer=payer,
        operator=operator,
        remark=remark,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at or utcnow(),
    )
    with allow_balance_mutation(wallet.id):
        wallet.balance = after
        session.add(tx)
        session.flush()
    return tx, True


def reverse(
    session: Session,
    *,
    tx: WalletTransaction,
    operator: str | None = None,
    reason: str | None = None,
) -> WalletTransaction:
    """冲正：只能新增反向流水（红字），原流水一个字都不改。"""
    opposite = Direction.OUT if tx.direction is Direction.IN else Direction.IN
    new_tx, _ = post(
        session,
        wallet_id=tx.wallet_id,
        direction=opposite,
        amount=tx.amount,
        biz_type=tx.biz_type,
        idempotency_key=f"reverse:{tx.id}",
        ref_doctype=tx.ref_doctype,
        ref_id=tx.ref_id,
        biz_no=tx.biz_no,
        operator=operator,
        remark=f"冲正 #{tx.id}" + (f"：{reason}" if reason else ""),
        exchange_rate=tx.exchange_rate,
        allow_negative=True,
    )
    return new_tx


# ---------------------------------------------------------------- 冻结

def freeze(session: Session, wallet_id: int, amount, *, note: str | None = None) -> Wallet:
    amt = money(amount)
    if amt <= 0:
        raise InvalidAmount("冻结金额必须大于 0")
    wallet = require_wallet(session, wallet_id, lock=True)
    available = money(wallet.balance) - money(wallet.frozen)
    if available < amt:
        raise InsufficientBalance(f"可冻结余额不足：需 {amt}，可用 {available}")
    with allow_balance_mutation(wallet.id):
        wallet.frozen = money(wallet.frozen) + amt
        session.flush()
    return wallet


def unfreeze(session: Session, wallet_id: int, amount) -> Wallet:
    amt = money(amount)
    if amt <= 0:
        raise InvalidAmount("解冻金额必须大于 0")
    wallet = require_wallet(session, wallet_id, lock=True)
    if money(wallet.frozen) < amt:
        raise InvalidAmount(f"解冻金额超过冻结额：冻结 {money(wallet.frozen)}，解冻 {amt}")
    with allow_balance_mutation(wallet.id):
        wallet.frozen = money(wallet.frozen) - amt
        session.flush()
    return wallet


# ---------------------------------------------------------------- 校验

def verify_wallet_chain(session: Session, wallet_id: int) -> int:
    """校验流水链：seq 连续、余额首尾相接、末笔余额 = 钱包当前余额。返回笔数。"""
    rows: Iterable[WalletTransaction] = session.execute(
        select(WalletTransaction)
        .where(WalletTransaction.wallet_id == wallet_id)
        .order_by(WalletTransaction.seq)
    ).scalars().all()

    wallet = session.get(Wallet, wallet_id)
    if wallet is None:
        raise WalletNotFound(wallet_id)

    expected = Decimal("0.0000")
    count = 0
    for i, row in enumerate(rows, start=1):
        count = i
        if row.seq != i:
            raise LedgerChainBroken(f"钱包 {wallet_id} 流水断号：第 {i} 条 seq={row.seq}")
        if money(row.balance_before) != expected:
            raise LedgerChainBroken(
                f"钱包 {wallet_id} 第 {row.seq} 条期初 {row.balance_before} ≠ 上笔期末 {expected}"
            )
        expected = money(row.balance_after)

    if money(wallet.balance) != expected:
        raise LedgerChainBroken(
            f"钱包 {wallet_id} 余额 {money(wallet.balance)} ≠ 末笔流水期末 {expected}"
        )
    return count


def wallet_summary(session: Session, wallet_id: int) -> dict:
    wallet = require_wallet(session, wallet_id)
    tx_count = session.execute(
        select(func.count()).select_from(WalletTransaction).where(WalletTransaction.wallet_id == wallet_id)
    ).scalar_one()
    return {
        "wallet_id": wallet.id,
        "owner_type": str(wallet.owner_type),
        "owner_id": wallet.owner_id,
        "currency": wallet.currency,
        "balance": str(money(wallet.balance)),
        "frozen": str(money(wallet.frozen)),
        "available": str(wallet.available),
        "tx_count": int(tx_count),
    }


def client_total_balance(session: Session, client_id: int, currency: str | None = None) -> Decimal:
    """客户视角总余额 = 客户子钱包余额 + 名下所有广告账户余额之和。"""
    from ..models.client import AdAccount

    w_stmt = select(Wallet).where(Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client_id)
    if currency:
        w_stmt = w_stmt.where(Wallet.currency == currency)
    total = sum((money(w.balance) for w in session.execute(w_stmt).scalars()), Decimal("0"))

    a_stmt = select(AdAccount).where(AdAccount.client_id == client_id)
    if currency:
        a_stmt = a_stmt.where(AdAccount.currency == currency)
    total += sum((money(a.balance) for a in session.execute(a_stmt).scalars()), Decimal("0"))
    return money(total)
