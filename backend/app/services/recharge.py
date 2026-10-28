"""D5 加款任务：发起（冻结占位）/ 执行 / 核查 / 重试 / 熔断。

铁律复述：
- 加款对象永远是具体广告账户（铁律 5）；
- 余额只能走 ledger 的 post/freeze/unfreeze（铁律 1）；
- unknown / TO_VERIFY 在人工核查前**不动账**，冻结也不释放（铁律 6）；
- 钱只能由人点出去（铁律 4）——本模块没有任何自动生成加款单的入口。
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.client import AdAccount, AgentChannel, Client
from ..models.docs import AuditLog, RechargeOrder, SystemSetting
from ..models.enums import (
    ENABLED_PLATFORMS,
    RECHARGE_STATUS_LABEL,
    BizType,
    Direction,
    OwnerType,
    Platform,
    RechargeStatus,
)
from ..models.wallet import Wallet
from . import rpa_mock
from .errors import InsufficientBalance, InvalidAmount, StateError
from .ledger import freeze, get_or_create_wallet, post, unfreeze
from .txn_records import recharge_txn_no

# 熔断：进程内缓存 + system_setting 落库（库是权威，重启可恢复）
CIRCUIT_PREFIX = "circuit:"
_CIRCUIT: dict[str, bool] = {}
# 当前只做 TikTok：默认只在面板列出 TikTok；历史遗留的熔断键仍原样列出，
# 避免把已经打开的开关藏起来造成误判。
DEFAULT_CIRCUIT_PLATFORMS = tuple(p.value for p in ENABLED_PLATFORMS)


# ------------------------------------------------------------------ 熔断

def _circuit_rows(session: Session) -> dict[str, bool]:
    rows = session.execute(
        select(SystemSetting).where(SystemSetting.key.like(f"{CIRCUIT_PREFIX}%"))
    ).scalars().all()
    return {r.key[len(CIRCUIT_PREFIX):]: str(r.value) == "1" for r in rows}


def circuit_map(session: Session) -> dict[str, bool]:
    """从库里刷新进程内缓存，返回全量开关。"""
    _CIRCUIT.clear()
    _CIRCUIT.update(_circuit_rows(session))
    return dict(_CIRCUIT)


def circuit_status(session: Session) -> list[dict]:
    state = circuit_map(session)
    platforms = list(DEFAULT_CIRCUIT_PLATFORMS)
    for p in state:
        if p not in platforms:
            platforms.append(p)
    return [{"platform": p, "enabled": bool(state.get(p, False))} for p in platforms]


def circuit_enabled(session: Session, platform: str) -> bool:
    # 以库为准：进程内字典只做缓存，避免多进程 / 重启后判断不一致
    return bool(circuit_map(session).get(platform, False))


def set_circuit(session: Session, platform: str, enabled: bool) -> dict:
    pl = (platform or "").strip().upper()
    if not pl:
        raise StateError("platform 必填")
    key = f"{CIRCUIT_PREFIX}{pl}"
    row = session.execute(
        select(SystemSetting).where(SystemSetting.key == key)
    ).scalar_one_or_none()
    value = "1" if enabled else "0"
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()
    _CIRCUIT[pl] = bool(enabled)
    return {"platform": pl, "enabled": bool(enabled), "persisted": True}


# ------------------------------------------------------------------ 序列化

def _elapsed_ms(order: RechargeOrder) -> int | None:
    if not order.receipt or order.created_at is None or order.updated_at is None:
        return None
    return int((order.updated_at - order.created_at).total_seconds() * 1000)


def serialize(order: RechargeOrder, account: AdAccount | None, client: Client | None) -> dict:
    return {
        "id": order.id,
        "biz_no": order.biz_no,
        "client_id": order.client_id,
        "client_name": client.company_name if client else None,
        "ad_account_id": order.ad_account_id,
        "account_id": account.account_id if account else None,
        "platform": str(account.platform) if account else None,
        "amount": str(money(order.amount)),
        "currency": order.currency,
        "status": str(order.status),
        "status_label": RECHARGE_STATUS_LABEL.get(order.status, str(order.status)),
        "txn_type": txn_type_of(order.status),
        "txn_type_label": TXN_TYPE_LABEL.get(txn_type_of(order.status), txn_type_of(order.status)),
        "agent": account.channel if account else None,
        "approval_no": order.approval_no,
        "serial_no": order.serial_no,
        "initiated_by": order.initiated_by,
        "retry_count": int(order.retry_count or 0),
        "receipt": order.receipt,
        # 资金总览交易明细用：到账金额只在成功时才算数，失败/撤销不编造到账。
        "credited_amount": str(money(order.amount)) if order.status is RechargeStatus.SUCCESS else None,
        "remark": order_remark(order),
        "created_at": order.created_at.isoformat(sep=" ", timespec="seconds"),
        "sync_at": order.updated_at.isoformat(sep=" ", timespec="seconds") if order.updated_at else None,
        "elapsed_ms": _elapsed_ms(order),
    }


def _receipt(order: RechargeOrder) -> dict:
    if not order.receipt:
        return {}
    try:
        data = json.loads(order.receipt)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def order_remark(order: RechargeOrder) -> str | None:
    """把回执 JSON 压成一行可读备注，供总览交易明细直接展示。

    回执是内部审计原文，不能整段丢给页面；也不能把失败单写成"成功"。
    """
    data = _receipt(order)
    if order.status is RechargeStatus.CANCELLED:
        reason = ((data.get("cancel") or {}).get("reason") or "").strip()
        return f"已作废：{reason}" if reason else "已作废：加款请求已取消"
    if order.status is RechargeStatus.REJECTED:
        note = (
            data.get("note")
            or (data.get("verify") or {}).get("reason")
            or (data.get("raw") or {}).get("message")
        )
        return f"失败：{note}" if note else "失败：媒体侧拒绝，已解冻"
    if order.status is RechargeStatus.TO_VERIFY:
        return data.get("note") or "结果未知，等待人工核查"
    if order.status is RechargeStatus.SUCCESS:
        return order.serial_no or data.get("serial_no") or "已到账"
    return None


def parse_status(value: str) -> RechargeStatus:
    try:
        return RechargeStatus(value)
    except ValueError as exc:
        raise StateError(f"状态取值非法：{value}") from exc


# ------------------------------------------------------------------ 交易类型（粗分组）
# 「状态」是单据的精确状态（5+ 种），「交易类型」是运营筛账时的粗分组：
# 先按 成功/处理中/失败/已撤销 收敛范围，再按状态细看。
# TO_VERIFY 归"处理中"：结果未知但钱还没动，仍属未完结。

TXN_TYPE_STATUS: dict[str, tuple[RechargeStatus, ...]] = {
    "SUCCESS": (RechargeStatus.SUCCESS,),
    "PENDING": (
        RechargeStatus.PENDING_EXEC,
        RechargeStatus.EXECUTING,
        RechargeStatus.AUDITING,
        RechargeStatus.TO_VERIFY,
    ),
    "FAILED": (RechargeStatus.REJECTED,),
    "CANCELLED": (RechargeStatus.CANCELLED,),
}

TXN_TYPE_LABEL = {
    "SUCCESS": "充值成功",
    "PENDING": "处理中",
    "FAILED": "执行失败",
    "CANCELLED": "已撤销",
}


def txn_type_of(status: RechargeStatus) -> str:
    for key, members in TXN_TYPE_STATUS.items():
        if status in members:
            return key
    return "UNKNOWN"


# ------------------------------------------------------------------ 查询

def list_recharges(
    session: Session,
    *,
    status_f: str | None = None,
    client_id: int | None = None,
    client_keyword: str | None = None,
    ad_account_id: int | None = None,
    platform: str | None = None,
    agent: str | None = None,
    account_keyword: str | None = None,
    txn_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    conds = []
    if status_f:
        conds.append(RechargeOrder.status == parse_status(status_f))
    if client_id:
        conds.append(RechargeOrder.client_id == client_id)
    # 客户名称 / 客户编号模糊搜：总览筛选框允许运营按口头称呼直接找
    if client_keyword and client_keyword.strip():
        kw = f"%{client_keyword.strip()}%"
        conds.append(or_(Client.company_name.like(kw), Client.biz_no.like(kw)))
    if ad_account_id:
        conds.append(RechargeOrder.ad_account_id == ad_account_id)
    # 一级代理商：AdAccount.channel 存的就是 AgentChannel.name，模糊搜
    if agent and agent.strip():
        conds.append(AdAccount.channel.like(f"%{agent.strip()}%"))
    # 广告账户 ID 是媒体侧字符串，不是自增主键，做模糊匹配
    if account_keyword and account_keyword.strip():
        conds.append(AdAccount.account_id.like(f"%{account_keyword.strip()}%"))
    if txn_type:
        members = TXN_TYPE_STATUS.get(txn_type.strip().upper())
        if members is None:
            raise StateError(f"交易类型取值非法：{txn_type}")
        conds.append(RechargeOrder.status.in_(members))
    if platform:
        conds.append(AdAccount.platform == platform.strip().upper())

    stmt = (
        select(RechargeOrder, AdAccount, Client)
        .join(AdAccount, AdAccount.id == RechargeOrder.ad_account_id)
        .join(Client, Client.id == RechargeOrder.client_id)
        .where(*conds)
    )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(RechargeOrder.created_at.desc(), RechargeOrder.id.desc())
        .limit(limit).offset(offset)
    ).all()

    sum_stmt = (
        select(RechargeOrder.status, func.count())
        .select_from(RechargeOrder)
        .join(AdAccount, AdAccount.id == RechargeOrder.ad_account_id)
        .join(Client, Client.id == RechargeOrder.client_id)
        .where(*conds)
        .group_by(RechargeOrder.status)
    )
    summary = {str(s): int(n) for s, n in session.execute(sum_stmt).all()}

    return {
        "total": int(total),
        "items": [serialize(o, a, c) for o, a, c in rows],
        "summary": summary,
    }


# ------------------------------------------------------------------ 发起

def _client_wallet(session: Session, order: RechargeOrder) -> Wallet:
    wallet = get_or_create_wallet(
        session,
        owner_type=OwnerType.CLIENT,
        owner_id=order.client_id,
        currency=order.currency,
        create=False,
    )
    if wallet is None:
        raise InsufficientBalance("客户钱包不存在，可用余额不足")
    return wallet


def create_recharge(
    session: Session,
    *,
    ad_account_id: int,
    amount: Decimal | str | int,
    request_no: str,
    remark: str | None = None,
    operator: str | None = None,
) -> tuple[RechargeOrder, bool]:
    """人工点击发起：先冻结占位，钱一分都还没出去。

    幂等键用调用方提供的 `request_no`（铁律 3：资金接口必须有请求级幂等键）。
    命中已有单据直接返回原单，**不重复冻结**。返回 (单据, 是否去重命中)。
    """
    amt = money(amount)
    if amt <= 0:
        raise InvalidAmount("加款金额必须大于 0")
    request_no = (request_no or "").strip()
    if not request_no:
        raise StateError("加款必须提供 request_no 作为幂等键")

    key = f"recharge:{request_no}"
    existing = session.execute(
        select(RechargeOrder).where(RechargeOrder.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    account = session.get(AdAccount, ad_account_id)
    if account is None:
        raise StateError(f"广告账户不存在（id={ad_account_id}）")

    wallet = get_or_create_wallet(
        session,
        owner_type=OwnerType.CLIENT,
        owner_id=account.client_id,
        currency=account.currency,
        create=False,
    )
    if wallet is None:
        raise InsufficientBalance("客户钱包不存在，可用余额不足")
    # 不足会抛 InsufficientBalance
    freeze(session, wallet.id, amt)

    # 幂等键不再依赖自增 id，insert 时就能写好；biz_no 仍按 id 生成
    order = RechargeOrder(
        client_id=account.client_id,
        ad_account_id=account.id,
        amount=amt,
        currency=account.currency,
        status=RechargeStatus.PENDING_EXEC,
        idempotency_key=key,
        initiated_by=operator,
        retry_count=0,
    )
    session.add(order)
    session.flush()

    order.biz_no = f"RC{utcnow():%Y%m%d}{order.id:05d}"
    # 交易记录页的对外编号：与单据号分开，详情/客服报号都看这个
    order.txn_no = recharge_txn_no(order)
    session.flush()
    return order, False


# ------------------------------------------------------------------ 记账

def _release_freeze(session: Session, wallet: Wallet, amount: Decimal) -> None:
    """解冻占位。已经解冻过（重复执行）就跳过，不报错。"""
    amt = money(amount)
    if amt > 0 and money(wallet.frozen) >= amt:
        unfreeze(session, wallet.id, amt)


def _agent_side(
    session: Session, order: RechargeOrder, account: AdAccount, amount: Decimal,
    operator: str | None,
) -> None:
    """一代钱包同步扣减；渠道没接入（无钱包）就跳过，不报错。"""
    if not account.channel:
        return
    agent = session.execute(
        select(AgentChannel).where(AgentChannel.name == account.channel)
    ).scalar_one_or_none()
    if agent is None:
        return
    wallet = get_or_create_wallet(
        session,
        owner_type=OwnerType.AGENT,
        owner_id=agent.id,
        currency=order.currency,
        create=False,
    )
    if wallet is None:
        return
    post(
        session,
        wallet_id=wallet.id,
        direction=Direction.OUT,
        amount=amount,
        biz_type=BizType.RECHARGE,
        idempotency_key=f"recharge-agent:{order.id}",
        ref_doctype="recharge_order",
        ref_id=order.id,
        biz_no=order.biz_no,
        operator=operator,
        remark=f"一代钱包同步扣减（{account.account_id}）",
        allow_negative=True,
    )


def _receipt_json(result: dict, **extra) -> str:
    payload = {
        "mock": True,
        "outcome": result.get("outcome"),
        "serial_no": result.get("serial_no"),
        "approval_no": result.get("approval_no"),
        "raw": result.get("raw"),
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _apply_success(
    session: Session, order: RechargeOrder, account: AdAccount, result: dict,
    operator: str | None, remark: str | None = None,
) -> None:
    """成功路径：先解冻，再出账，最后加媒体侧账户余额。"""
    amt = money(order.amount)
    wallet = _client_wallet(session, order)

    # 顺序不能反：post() 会校验 available = balance - frozen
    _release_freeze(session, wallet, amt)

    tx, created = post(
        session,
        wallet_id=wallet.id,
        direction=Direction.OUT,
        amount=amt,
        biz_type=BizType.RECHARGE,
        idempotency_key=f"recharge:{order.id}",
        ref_doctype="recharge_order",
        ref_id=order.id,
        biz_no=order.biz_no,
        operator=operator,
        remark=remark or f"加款至广告账户 {account.account_id}",
    )
    # post 幂等：重复投递时 created=False，媒体侧余额不能加第二次
    if created:
        account.balance = money(account.balance) + amt

    _agent_side(session, order, account, amt, operator)

    order.status = RechargeStatus.SUCCESS
    order.serial_no = result.get("serial_no")
    order.approval_no = result.get("approval_no")
    # exec_by 让交易记录页能显示「谁执行了这一笔」，不再一律回落到发起人
    order.receipt = _receipt_json(result, tx_id=tx.id, exec_by=operator)


# ------------------------------------------------------------------ 执行

def _lock_order(session: Session, order_id: int) -> RechargeOrder:
    order = session.get(RechargeOrder, order_id, with_for_update=True)
    if order is None:
        raise StateError(f"加款单不存在（id={order_id}）")
    return order


def audit_exec(
    session: Session, order: RechargeOrder, *, outcome: str, operator: str | None
) -> None:
    """执行加款的留痕。这是唯一真的出账的动作，事后必须能查是谁点的。"""
    session.add(
        AuditLog(
            actor=operator,
            action="recharge.exec",
            doctype="recharge_order",
            doc_id=order.id,
            after=json.dumps(
                {
                    "biz_no": order.biz_no,
                    "requested_outcome": outcome,
                    "result_status": str(order.status),
                    "amount": str(money(order.amount)),
                    "currency": order.currency,
                    "serial_no": order.serial_no,
                },
                ensure_ascii=False,
            ),
            reason=f"执行加款（outcome={outcome}）",
        )
    )
    session.flush()


def execute(
    session: Session, order_id: int, *, outcome: str = "SUCCESS", operator: str | None = None
) -> RechargeOrder:
    order = _lock_order(session, order_id)
    account = session.get(AdAccount, order.ad_account_id)
    if account is None:
        raise StateError(f"广告账户不存在（id={order.ad_account_id}）")

    if circuit_enabled(session, str(account.platform)):
        raise StateError(f"平台 {account.platform} 已熔断，暂停执行")
    if order.status is not RechargeStatus.PENDING_EXEC:
        raise StateError(f"当前状态 {order.status} 不允许执行")

    outcome = (outcome or "SUCCESS").strip().upper()
    result = rpa_mock.exec_recharge(order, outcome)

    if outcome == "SUCCESS":
        _apply_success(session, order, account, result, operator)
    elif outcome == "UNKNOWN":
        # 铁律 6：结果未知前不动账，冻结也不释放
        order.status = RechargeStatus.TO_VERIFY
        order.receipt = _receipt_json(
            result, note="unknown，等待人工核查", exec_by=operator
        )
    elif outcome == "REJECTED":
        _release_freeze(session, _client_wallet(session, order), money(order.amount))
        order.status = RechargeStatus.REJECTED
        order.receipt = _receipt_json(
            result, note="媒体侧拒绝，已解冻", exec_by=operator
        )
    else:
        raise StateError(f"不支持的执行结果：{outcome}")

    session.flush()
    return order


def resolve(
    session: Session,
    order_id: int,
    *,
    result: str,
    reason: str | None = None,
    operator: str | None = None,
) -> RechargeOrder:
    """核查 unknown：人工确认媒体后台到底到没到账。"""
    order = _lock_order(session, order_id)
    if order.status is not RechargeStatus.TO_VERIFY:
        raise StateError(f"当前状态 {order.status} 不允许核查")

    account = session.get(AdAccount, order.ad_account_id)
    if account is None:
        raise StateError(f"广告账户不存在（id={order.ad_account_id}）")

    verdict = (result or "").strip().upper()
    if verdict == "SUCCESS":
        receipt = rpa_mock.exec_recharge(order, "SUCCESS")
        _apply_success(
            session, order, account, receipt, operator,
            remark=f"核查确认已到账（{reason}）" if reason else "核查确认已到账",
        )
        order.receipt = _receipt_json(
            receipt,
            verify={"result": verdict, "reason": reason, "by": operator},
            exec_by=operator,
        )
    elif verdict == "FAILED":
        _release_freeze(session, _client_wallet(session, order), money(order.amount))
        order.status = RechargeStatus.REJECTED
        order.receipt = _receipt_json(
            {"outcome": "FAILED", "serial_no": None, "approval_no": None, "raw": None},
            verify={"result": verdict, "reason": reason, "by": operator},
            exec_by=operator,
        )
    else:
        raise StateError(f"核查结果只能是 SUCCESS / FAILED（收到 {result}）")

    session.flush()
    return order


def cancel(
    session: Session,
    order_id: int,
    *,
    reason: str | None,
    operator: str | None = None,
) -> RechargeOrder:
    """人工撤销：把冻结占位放回去。钱从没出去过，所以不记任何流水。"""
    order = _lock_order(session, order_id)
    if order.status not in (RechargeStatus.PENDING_EXEC, RechargeStatus.TO_VERIFY):
        raise StateError(f"当前状态 {order.status} 不允许撤销")
    text = (reason or "").strip()
    if not text:
        raise StateError("撤销必须填写原因")

    _release_freeze(session, _client_wallet(session, order), money(order.amount))
    order.status = RechargeStatus.CANCELLED
    order.receipt = json.dumps(
        {"mock": True, "cancel": {"reason": text, "by": operator, "mock": True}},
        ensure_ascii=False,
    )
    session.flush()
    return order


def retry(session: Session, order_id: int, *, operator: str | None = None) -> RechargeOrder:
    order = _lock_order(session, order_id)
    if order.status not in (RechargeStatus.REJECTED, RechargeStatus.TO_VERIFY):
        raise StateError(f"当前状态 {order.status} 不允许重试")

    wallet = _client_wallet(session, order)
    amt = money(order.amount)
    if money(wallet.frozen) < amt:
        freeze(session, wallet.id, amt)

    order.status = RechargeStatus.PENDING_EXEC
    order.retry_count = int(order.retry_count or 0) + 1
    session.flush()
    return order
