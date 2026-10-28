"""广告账户交易记录（只读投影）。

页面口径（CRM「交易记录」）：交易编号 / 交易类型 / 账户ID / 交易金额 /
操作备注 / 状态 / 反馈信息 / 操作人 / 操作时间。

同一份只读投影供两端使用：
- 内部后台 `list_transactions`：全量字段，含客户、代理商、重试次数；
- 客户门户 `list_client_transactions`：按登录态 client_id 过滤，白名单字段，
  反馈只给结论，不下发客户名 / 代理商 / 渠道 / 业务单号。

不变式：
- 本模块**不新增表、不写账、不冻结**，只把 `recharge_order` 投影成交易流水；
- 交易编号 `txn_no` 在加款单创建时落库（ADTR + 日期 + 6 位序号），
  与业务单号 `biz_no`（RC…）分开：客服报号用前者，单据主键用后者；
- 交易状态是执行口径（待开始 / 处理中 / 已完成 / 失败 / 待核查 / 已撤销），
  与充值单精确状态一一映射，不另存一份状态；
- 广告账户退款（ADTK）目前没有业务入口，所以交易类型只下发"广告账户充值"，
  不提供永远筛不到数据的选项。退款流程落地后在这里扩 `TXN_TYPE_LABEL` 与查询条件。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.client import AdAccount, Client
from ..models.docs import RechargeOrder
from ..models.enums import RechargeStatus
from .anonymize import display_name
from .errors import StateError

TXN_PREFIX = "ADTR"

# 交易类型（当前只有广告账户充值）
TXN_TYPE_LABEL: dict[str, str] = {
    "RECHARGE": "广告账户充值",
}

# 充值单精确状态 → 交易执行状态
_STATUS_OF_RECHARGE: dict[RechargeStatus, str] = {
    RechargeStatus.PENDING_EXEC: "PENDING",
    RechargeStatus.EXECUTING: "RUNNING",
    RechargeStatus.AUDITING: "RUNNING",
    RechargeStatus.SUCCESS: "DONE",
    RechargeStatus.REJECTED: "FAILED",
    RechargeStatus.TO_VERIFY: "VERIFY",
    RechargeStatus.CANCELLED: "CANCELLED",
}

# 下拉顺序就是运营看账的顺序：未开始 → 在途 → 待核查 → 结束态
TXN_STATUS_LABEL: dict[str, str] = {
    "PENDING": "待开始",
    "RUNNING": "处理中",
    "VERIFY": "待核查",
    "DONE": "已完成",
    "FAILED": "失败",
    "CANCELLED": "已撤销",
}

_RECHARGE_OF_STATUS: dict[str, tuple[RechargeStatus, ...]] = {
    code: tuple(s for s, c in _STATUS_OF_RECHARGE.items() if c == code)
    for code in TXN_STATUS_LABEL
}


def recharge_txn_no(order: RechargeOrder) -> str:
    """交易编号：ADTR + 创建日期 + 6 位序号。同一条单据的号码稳定不变。"""
    stamp = order.created_at or utcnow()
    return f"{TXN_PREFIX}{stamp:%Y%m%d}{int(order.id):06d}"


def _receipt(order: RechargeOrder) -> dict[str, Any]:
    if not order.receipt:
        return {}
    try:
        data = json.loads(order.receipt)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _feedback(order: RechargeOrder) -> str | None:
    """反馈信息：媒体侧回执压成一行。没结束的单子不编造结果。"""
    data = _receipt(order)
    status = order.status
    if status is RechargeStatus.SUCCESS:
        return order.serial_no or data.get("serial_no") or "已到账"
    if status is RechargeStatus.REJECTED:
        note = (
            data.get("note")
            or (data.get("verify") or {}).get("reason")
            or (data.get("raw") or {}).get("message")
        )
        return str(note) if note else "媒体侧拒绝，已解冻"
    if status is RechargeStatus.TO_VERIFY:
        return str(data.get("note") or "结果未知，等待人工核查")
    if status is RechargeStatus.CANCELLED:
        reason = str((data.get("cancel") or {}).get("reason") or "").strip()
        return f"已作废：{reason}" if reason else "已作废：加款请求已取消"
    return None


def _operator(order: RechargeOrder) -> str | None:
    """操作人：优先取最后一次动作的执行人，老单据回落到发起人。"""
    data = _receipt(order)
    return (
        (data.get("cancel") or {}).get("by")
        or (data.get("verify") or {}).get("by")
        or data.get("exec_by")
        or order.initiated_by
    )


def _dt(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def serialize(
    order: RechargeOrder, account: AdAccount | None, client: Client | None
) -> dict:
    status_code = _STATUS_OF_RECHARGE.get(order.status, "PENDING")
    return {
        "id": order.id,
        "txn_no": order.txn_no or recharge_txn_no(order),
        "biz_no": order.biz_no,
        "txn_type": "RECHARGE",
        "txn_type_label": TXN_TYPE_LABEL["RECHARGE"],
        "client_id": order.client_id,
        "client_name": client.company_name if client else None,
        "ad_account_id": order.ad_account_id,
        "account_id": account.account_id if account else None,
        "platform": str(account.platform) if account else None,
        "agent": account.channel if account else None,
        "amount": str(money(order.amount)),
        "currency": order.currency,
        "remark": f"重试 {int(order.retry_count)} 次" if order.retry_count else None,
        "status": status_code,
        "status_label": TXN_STATUS_LABEL.get(status_code, status_code),
        "status_raw": str(order.status),
        "feedback": _feedback(order),
        "operator": _operator(order),
        "occurred_at": _dt(order.updated_at or order.created_at),
    }


def list_transactions(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    txn_no: str | None = None,
    txn_type: str | None = None,
    account_keyword: str | None = None,
    status_f: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    conds: list = []

    if txn_type and txn_type.strip():
        code = txn_type.strip().upper()
        if code not in TXN_TYPE_LABEL:
            raise StateError(f"交易类型取值非法：{txn_type}")
        # 当前只有充值单；退款类型接入时在这里补自己的来源与条件

    if status_f and status_f.strip():
        code = status_f.strip().upper()
        members = _RECHARGE_OF_STATUS.get(code)
        if members is None:
            raise StateError(f"交易状态取值非法：{status_f}")
        conds.append(RechargeOrder.status.in_(members))

    if txn_no and txn_no.strip():
        kw = f"%{txn_no.strip()}%"
        # 客服可能拿交易编号，也可能拿业务单号来查，两个都认
        conds.append(or_(RechargeOrder.txn_no.like(kw), RechargeOrder.biz_no.like(kw)))

    if account_keyword and account_keyword.strip():
        conds.append(AdAccount.account_id.like(f"%{account_keyword.strip()}%"))

    # 按「操作时间」筛，和页面上显示的那一列同口径
    if date_from:
        conds.append(RechargeOrder.updated_at >= datetime.combine(date_from, time.min))
    if date_to:
        conds.append(RechargeOrder.updated_at <= datetime.combine(date_to, time.max))

    stmt = (
        select(RechargeOrder, AdAccount, Client)
        .join(AdAccount, AdAccount.id == RechargeOrder.ad_account_id)
        .join(Client, Client.id == RechargeOrder.client_id)
        .where(*conds)
    )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(RechargeOrder.updated_at.desc(), RechargeOrder.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "total": int(total),
        "items": [serialize(order, account, client) for order, account, client in rows],
    }


EXPORT_COLUMNS = [
    ("txn_no", "交易编号"),
    ("txn_type_label", "交易类型"),
    ("account_id", "广告账户ID"),
    ("amount", "交易金额"),
    ("currency", "币种"),
    ("remark", "操作备注"),
    ("status_label", "状态"),
    ("feedback", "反馈信息"),
    ("operator", "操作人"),
    ("occurred_at", "操作时间"),
]


def export_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in EXPORT_COLUMNS])
    for row in items:
        writer.writerow(
            [row.get(key) if row.get(key) is not None else "" for key, _label in EXPORT_COLUMNS]
        )
    # BOM：Excel 直接按 UTF-8 打开，中文不乱码
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ 客户门户口径
# 客户端只看自己的广告账户充值：交易编号、账户、金额、状态、反馈、操作人、时间。
# 白名单返回：不带客户名、代理商、渠道、业务单号、重试次数等内部字段。

PORTAL_TXN_TYPE_LABEL: dict[str, str] = {
    "RECHARGE": "广告账户充值",
}

# 客户端反馈只给结论，不透传内部回执原文（里面可能有渠道报错、内部备注）。
_CLIENT_FEEDBACK: dict[RechargeStatus, str] = {
    RechargeStatus.SUCCESS: "已到账",
    RechargeStatus.REJECTED: "充值失败，冻结金额已退回可用余额",
    RechargeStatus.TO_VERIFY: "到账结果确认中，请稍候",
    RechargeStatus.CANCELLED: "已撤销，冻结金额已退回可用余额",
}


def _client_feedback(order: RechargeOrder) -> str | None:
    return _CLIENT_FEEDBACK.get(order.status)


def serialize_client(order: RechargeOrder, account: AdAccount | None) -> dict:
    """门户交易记录行：只下发客户端需要的字段。"""
    status_code = _STATUS_OF_RECHARGE.get(order.status, "PENDING")
    return {
        "id": order.id,
        "txn_no": order.txn_no or recharge_txn_no(order),
        "txn_type": "RECHARGE",
        "txn_type_label": PORTAL_TXN_TYPE_LABEL["RECHARGE"],
        "account_id": account.account_id if account else None,
        "amount": str(money(order.amount)),
        "currency": order.currency,
        "status": status_code,
        "status_label": TXN_STATUS_LABEL.get(status_code, status_code),
        "feedback": _client_feedback(order),
        "operator": display_name(_operator(order)),
        "occurred_at": _dt(order.updated_at or order.created_at),
    }


def list_client_transactions(
    session: Session,
    *,
    client_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    txn_no: str | None = None,
    txn_type: str | None = None,
    account_keyword: str | None = None,
    status_f: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """客户侧交易记录：client_id 只由登录态传入，绝不接受前端参数。"""
    if txn_type and txn_type.strip():
        code = txn_type.strip().upper()
        if code not in PORTAL_TXN_TYPE_LABEL:
            raise StateError(f"交易类型取值非法：{txn_type}")

    # 双重隔离：单据属于该客户，账户也必须属于该客户（防脏数据串号）
    conds: list = [
        RechargeOrder.client_id == client_id,
        AdAccount.client_id == client_id,
    ]

    if status_f and status_f.strip():
        code = status_f.strip().upper()
        members = _RECHARGE_OF_STATUS.get(code)
        if members is None:
            raise StateError(f"交易状态取值非法：{status_f}")
        conds.append(RechargeOrder.status.in_(members))

    if txn_no and txn_no.strip():
        kw = f"%{txn_no.strip()}%"
        # 客户手上可能只有交易编号；业务单号也认，但不回传
        conds.append(or_(RechargeOrder.txn_no.like(kw), RechargeOrder.biz_no.like(kw)))

    if account_keyword and account_keyword.strip():
        conds.append(AdAccount.account_id.like(f"%{account_keyword.strip()}%"))

    if date_from:
        conds.append(RechargeOrder.updated_at >= datetime.combine(date_from, time.min))
    if date_to:
        conds.append(RechargeOrder.updated_at <= datetime.combine(date_to, time.max))

    stmt = (
        select(RechargeOrder, AdAccount)
        .join(AdAccount, AdAccount.id == RechargeOrder.ad_account_id)
        .where(*conds)
    )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(RechargeOrder.updated_at.desc(), RechargeOrder.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "total": int(total),
        "items": [serialize_client(order, account) for order, account in rows],
    }


CLIENT_EXPORT_COLUMNS = [
    ("txn_no", "交易编号"),
    ("txn_type_label", "交易类型"),
    ("account_id", "广告账户ID"),
    ("amount", "交易金额"),
    ("currency", "币种"),
    ("status_label", "状态"),
    ("feedback", "反馈信息"),
    ("operator", "操作人"),
    ("occurred_at", "操作时间"),
]


def export_client_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in CLIENT_EXPORT_COLUMNS])
    for row in items:
        writer.writerow(
            [
                row.get(key) if row.get(key) is not None else ""
                for key, _label in CLIENT_EXPORT_COLUMNS
            ]
        )
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
