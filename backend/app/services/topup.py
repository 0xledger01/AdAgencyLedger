"""充值单：钱先进客户子钱包。财务确认到账前，一分钱都不入账。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.client import Client, ClientTopUp
from ..models.docs import AuditLog
from ..models.enums import BizType, ClientStatus, Direction, FundSourceType, OwnerType, PaymentMethod, TopUpStatus
from ..models.wallet import Wallet
from . import storage
from .errors import InvalidAmount, StateError
from .ledger import get_or_create_wallet, post, reverse


def _biz_no(prefix: str, seq: int) -> str:
    return f"{prefix}{utcnow():%Y%m%d}{seq:05d}"


def create_topup(
    session: Session,
    *,
    client_id: int,
    amount: Decimal | str | float,
    currency: str = "USD",
    payment_method: PaymentMethod | str = PaymentMethod.BANK_TRANSFER,
    payment_channel: str | None = None,
    payer: str | None = None,
    fund_source_type: FundSourceType | str = FundSourceType.SELF,
    voucher: str | None = None,
    remark: str | None = None,
    operator: str | None = None,
    exchange_rate: Decimal | str | float | None = None,
    payable_amount: Decimal | str | float | None = None,
    payable_currency: str | None = None,
    rate_source: str | None = None,
) -> ClientTopUp:
    amt = money(amount)
    if amt <= 0:
        raise InvalidAmount("充值金额必须大于 0")
    if amt != amt.quantize(Decimal("0.01")):
        raise InvalidAmount("充值金额最多支持 2 位小数")
    client = session.get(Client, client_id)
    if client is None:
        raise StateError(f"客户不存在（id={client_id}）")
    ccy = (currency or "USD").upper()
    if ccy == "CNY":
        raise StateError("系统只支持美金充值，人民币充值已下线")

    # 默认沿用「1:1、应付=到账」的历史口径（内部代录/存量调用）；
    # 门户充值会传入实时 USD/CNY 报价，把应付人民币和汇率快照一起落库。
    rate = money(exchange_rate) if exchange_rate is not None else Decimal("1")
    if rate <= 0:
        raise InvalidAmount("汇率必须大于 0")
    pay_ccy = (payable_currency or ccy).upper()
    pay = money(payable_amount) if payable_amount is not None else amt
    if pay <= 0:
        raise InvalidAmount("应付金额必须大于 0")
    if pay_ccy == ccy and rate != Decimal("1"):
        raise StateError("同币种应付必须 1:1，汇率只用于跨币种折算")

    topup = ClientTopUp(
        client_id=client_id,
        amount=amt,
        currency=ccy,
        exchange_rate=rate,
        payable_amount=pay,
        payable_currency=pay_ccy,
        received_amount=amt,
        rate_source=rate_source,
        payment_method=PaymentMethod(payment_method),
        payment_channel=payment_channel,
        payer=payer,
        fund_source_type=FundSourceType(fund_source_type),
        voucher=voucher,
        remark=remark,
        status=TopUpStatus.PENDING_CONFIRM if voucher else TopUpStatus.PENDING_PAY,
    )
    session.add(topup)
    session.flush()
    topup.biz_no = _biz_no("TP", topup.id)
    session.flush()
    return topup


def confirm_topup(
    session: Session,
    topup_id: int,
    *,
    operator: str,
    remark: str | None = None,
    no_voucher_reason: str | None = None,
) -> ClientTopUp:
    """财务核对到账 → 生成一条进账流水。幂等：重复确认不会重复入账。

    风控点（契约 P3.5 4.3）：财务是唯一的人工闸口，**无凭证不得确认到账**。
    确属线下现金等特殊情形，必须留下不少于 5 字的原因并进审计日志。
    """
    topup = session.get(ClientTopUp, topup_id, with_for_update=True)
    if topup is None:
        raise StateError(f"充值单不存在（id={topup_id}）")
    # 幂等优先：已到账直接返回，不再走凭证校验（否则历史无凭证单永远确认不了）
    if topup.status is TopUpStatus.RECEIVED:
        return topup
    if topup.status not in (TopUpStatus.PENDING_PAY, TopUpStatus.PENDING_CONFIRM):
        raise StateError(f"当前状态 {topup.status} 不允许确认到账")

    reason = (no_voucher_reason or "").strip()
    out_remark = remark or "财务确认到账"

    if not topup.voucher:
        if len(reason) < 5:
            raise StateError(
                "无凭证不得确认到账，如确属线下现金等特殊情况请填写原因（不少于 5 字）"
            )
        out_remark = f"{out_remark}｜无凭证确认：{reason}"
        session.add(
            AuditLog(
                actor=operator,
                action="CONFIRM_NO_VOUCHER",
                doctype="client_topup",
                doc_id=topup.id,
                after=f"amount={money(topup.amount)} {topup.currency}",
                reason=reason,
            )
        )
    else:
        # 指向上传接口的凭证，必须真的在磁盘上；历史遗留路径不做存在性校验
        if not storage.exists_on_disk(topup.voucher) and storage.is_file_url(topup.voucher):
            raise StateError("凭证文件已丢失，请重新上传或登记异常后再确认到账")

    wallet = get_or_create_wallet(
        session, owner_type=OwnerType.CLIENT, owner_id=topup.client_id, currency=topup.currency
    )
    assert isinstance(wallet, Wallet)
    post(
        session,
        wallet_id=wallet.id,
        direction=Direction.IN,
        amount=topup.amount,
        biz_type=BizType.TOPUP,
        idempotency_key=f"topup:{topup.id}",
        ref_doctype="client_topup",
        ref_id=topup.id,
        biz_no=topup.biz_no,
        voucher=topup.voucher,
        payer=topup.payer,
        operator=operator,
        remark=out_remark,
        exchange_rate=topup.exchange_rate,
    )
    topup.status = TopUpStatus.RECEIVED
    topup.confirmed_by = operator
    topup.confirmed_at = utcnow()
    session.flush()
    return topup


def reject_topup(session: Session, topup_id: int, *, operator: str, reason: str) -> ClientTopUp:
    topup = session.get(ClientTopUp, topup_id, with_for_update=True)
    if topup is None:
        raise StateError(f"充值单不存在（id={topup_id}）")
    # PENDING_PAY 也必须能退回：门户提交后客户放弃/超时未付的单，
    # 否则会永久挂在"待支付"，既入不了账也关不掉（F-08 死胡同）。
    if topup.status not in (TopUpStatus.PENDING_CONFIRM, TopUpStatus.PENDING_PAY):
        raise StateError(f"当前状态 {topup.status} 不允许退回")
    if not reason:
        raise StateError("退回必须填写原因")
    topup.status = TopUpStatus.RETURNED
    topup.reject_reason = reason
    topup.confirmed_by = operator
    session.flush()
    return topup


def reverse_topup(session: Session, topup_id: int, *, operator: str, reason: str) -> ClientTopUp:
    """冲正：新增红字流水，原流水不动。需双签，调用方负责校验第二签。"""
    topup = session.get(ClientTopUp, topup_id, with_for_update=True)
    if topup is None:
        raise StateError(f"充值单不存在（id={topup_id}）")
    # RECEIVED = 旧「确认到账」路径，APPROVED = 线下汇款审核通过路径
    if topup.status not in (TopUpStatus.RECEIVED, TopUpStatus.APPROVED):
        raise StateError("只有已到账 / 审核通过的充值单可以冲正")
    if not reason:
        raise StateError("冲正必须填写原因")

    wallet: Wallet = session.execute(
        select(Wallet).where(
            Wallet.owner_type == OwnerType.CLIENT,
            Wallet.owner_id == topup.client_id,
            Wallet.currency == topup.currency,
        )
    ).scalar_one()
    from ..models.wallet import WalletTransaction

    origin = session.execute(
        select(WalletTransaction)
        .where(
            WalletTransaction.idempotency_key.in_(
                [f"topup:{topup.id}", f"fund-out:{topup.id}"]
            )
        )
        .order_by(WalletTransaction.id)
    ).scalar_one_or_none()
    if origin is None:
        raise StateError("找不到原始到账流水，无法冲正")

    reverse(session, tx=origin, operator=operator, reason=reason)
    topup.status = TopUpStatus.REVERSED
    session.flush()
    return topup


def ensure_client_wallet(session: Session, client: Client, currency: str | None = None) -> Wallet:
    if client.status is not ClientStatus.ACTIVE:
        raise StateError(f"客户状态 {client.status} 不允许资金操作")
    wallet = get_or_create_wallet(
        session, owner_type=OwnerType.CLIENT, owner_id=client.id, currency=currency or client.settle_currency
    )
    assert isinstance(wallet, Wallet)
    return wallet
