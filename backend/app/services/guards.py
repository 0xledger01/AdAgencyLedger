"""铁律守卫。

铁律 1：钱包是唯一账本，余额只能由 WalletTransaction 产生，禁止直接改 balance。
铁律 8 / D3.1：流水不可修改、不可删除。

两道防线：
1. ORM 事件 —— 捕获应用层的 `wallet.balance = x` 直改；
2. 数据库触发器 —— 连裸 SQL 的 UPDATE/DELETE 都拦掉（见 db.py）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from sqlalchemy import event, inspect
from sqlalchemy.orm import Mapper

from ..models.wallet import Wallet, WalletTransaction
from .errors import ImmutableViolation

# 只有账本内核能拿到这把钥匙
_ledger_scope: ContextVar[frozenset[int] | None] = ContextVar("ledger_scope", default=None)


@contextmanager
def allow_balance_mutation(*wallet_ids: int | None) -> Iterator[None]:
    current = set(_ledger_scope.get() or ())
    current.update(w for w in wallet_ids if w is not None)
    token = _ledger_scope.set(frozenset(current))
    try:
        yield
    finally:
        _ledger_scope.reset(token)


def _is_allowed(wallet_id: int | None) -> bool:
    scope = _ledger_scope.get()
    return bool(scope) and wallet_id in scope


@event.listens_for(Wallet, "before_update")
def _guard_wallet_update(_mapper: Mapper, _conn, target: Wallet) -> None:  # noqa: ANN001
    state = inspect(target)
    touched = [
        name
        for name in ("balance", "frozen")
        if name in state.committed_state or state.attrs[name].history.has_changes()
    ]
    if touched and not _is_allowed(target.id):
        raise ImmutableViolation(
            f"钱包余额只能通过账本流水变动，禁止直接修改 {touched}（wallet_id={target.id}）",
            code="DIRECT_BALANCE_MUTATION",
        )


@event.listens_for(Wallet, "before_insert")
def _guard_wallet_insert(_mapper: Mapper, _conn, target: Wallet) -> None:  # noqa: ANN001
    if target.balance and target.balance != 0:
        raise ImmutableViolation(
            "新建钱包余额必须为 0，初始资金必须走账本流水", code="DIRECT_BALANCE_MUTATION"
        )
    if target.frozen and target.frozen != 0:
        raise ImmutableViolation("新建钱包冻结额必须为 0", code="DIRECT_BALANCE_MUTATION")


@event.listens_for(Wallet, "before_delete")
def _guard_wallet_delete(_mapper: Mapper, _conn, target: Wallet) -> None:  # noqa: ANN001
    raise ImmutableViolation("钱包不可删除（有账可查是底线）", code="WALLET_NOT_DELETABLE")


@event.listens_for(WalletTransaction, "before_update")
def _guard_tx_update(_mapper: Mapper, _conn, target: WalletTransaction) -> None:  # noqa: ANN001
    raise ImmutableViolation(
        f"流水不可修改，纠错只能冲正（tx_id={target.id}）", code="LEDGER_IMMUTABLE"
    )


@event.listens_for(WalletTransaction, "before_delete")
def _guard_tx_delete(_mapper: Mapper, _conn, target: WalletTransaction) -> None:  # noqa: ANN001
    raise ImmutableViolation(
        f"流水不可删除，纠错只能冲正（tx_id={target.id}）", code="LEDGER_IMMUTABLE"
    )
