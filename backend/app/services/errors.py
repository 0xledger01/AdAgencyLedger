from __future__ import annotations


class LedgerError(Exception):
    """账本相关异常基类。"""

    code = "LEDGER_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class WalletNotFound(LedgerError):
    code = "WALLET_NOT_FOUND"


class InvalidAmount(LedgerError):
    code = "INVALID_AMOUNT"


class InsufficientBalance(LedgerError):
    code = "INSUFFICIENT_BALANCE"


class LedgerChainBroken(LedgerError):
    code = "LEDGER_CHAIN_BROKEN"


class IdempotencyConflict(LedgerError):
    code = "IDEMPOTENCY_CONFLICT"


class ImmutableViolation(LedgerError):
    """流水被修改 / 删除，或余额被绕过账本直接改。"""

    code = "IMMUTABLE_VIOLATION"


class StateError(LedgerError):
    """单据状态不允许该操作。"""

    code = "STATE_ERROR"


class InvalidFile(LedgerError):
    """上传文件不合规：空文件、超限、类型不在白名单、内容与声明不符、路径越界。"""

    code = "INVALID_FILE"


class RateChanged(LedgerError):
    """报价汇率与提交时汇率漂移过大，必须让客户按新汇率重新确认。"""

    code = "RATE_CHANGED"


class RateUnavailable(LedgerError):
    """实时汇率不可用且本地缓存过期，宁可停单也不拿旧汇率收钱。"""

    code = "FX_UNAVAILABLE"
