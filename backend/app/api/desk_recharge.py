"""D5 加款任务（契约第 5 节）：列表 / 发起（冻结）/ Mock 三态执行 / 核查 / 重试 / 熔断。

路由只做参数与权限，状态机与记账全部在 services/recharge.py。
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..models.base import money, utcnow
from ..models.client import AdAccount, Client
from ..models.docs import RechargeOrder
from ..models.enums import RECHARGE_STATUS_LABEL, OwnerType
from ..models.user import User
from ..models.wallet import Wallet
from ..services import recharge as svc
from ..services import txn_records as txn_svc
from ..services.rbac import Perm
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["加款任务"], dependencies=[Depends(require_desk)])


def _wallet(db, client_id: int, currency: str) -> Wallet | None:  # noqa: ANN001
    return db.execute(
        select(Wallet).where(
            Wallet.owner_type == OwnerType.CLIENT,
            Wallet.owner_id == client_id,
            Wallet.currency == currency,
        )
    ).scalar_one_or_none()


def _item(db, order: RechargeOrder) -> dict:  # noqa: ANN001
    return svc.serialize(
        order,
        db.get(AdAccount, order.ad_account_id),
        db.get(Client, order.client_id),
    )


@router.get("/recharges", response_model=dict, summary="加款任务列表")
def list_recharges(
    db: DbSession,
    user: User = require_perm(Perm.RECHARGE_VIEW),
    status_f: str | None = Query(None, alias="status"),
    client_id: int | None = None,
    client_keyword: str | None = Query(None, description="客户名称/编号模糊匹配"),
    ad_account_id: int | None = None,
    platform: str | None = None,
    agent: str | None = Query(None, description="一级代理商名称，模糊匹配"),
    account_keyword: str | None = Query(None, description="广告账户 ID，模糊匹配"),
    txn_type: str | None = Query(None, pattern="^(SUCCESS|PENDING|FAILED|CANCELLED)$"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return svc.list_recharges(
        db, status_f=status_f, client_id=client_id, ad_account_id=ad_account_id,
        client_keyword=client_keyword,
        platform=platform, agent=agent, account_keyword=account_keyword,
        txn_type=txn_type, limit=limit, offset=offset,
    )


@router.get("/recharges/export", summary="导出加款交易明细（CSV）")
def export_recharges(
    db: DbSession,
    user: User = require_perm(Perm.RECHARGE_VIEW),
    status_f: str | None = Query(None, alias="status"),
    client_id: int | None = None,
    client_keyword: str | None = None,
    ad_account_id: int | None = None,
    platform: str | None = None,
    agent: str | None = None,
    account_keyword: str | None = None,
    txn_type: str | None = None,
) -> Response:
    """导出与页面同一套筛选条件下的全部交易明细。

    只读查询，不动账。限额 10000 行，超出时请在筛选里收窄范围。
    """
    data = svc.list_recharges(
        db, status_f=status_f, client_id=client_id, client_keyword=client_keyword,
        ad_account_id=ad_account_id, platform=platform, agent=agent,
        account_keyword=account_keyword, txn_type=txn_type, limit=10000, offset=0,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ID", "单号", "代理商", "客户名称", "广告账户ID", "交易类型",
        "交易金额", "币种", "到账金额", "备注", "状态", "交易时间", "同步时间",
    ])
    for r in data["items"]:
        writer.writerow([
            r["id"], r["biz_no"] or "", r["agent"] or "", r["client_name"] or "",
            r["account_id"] or "", "广告账户充值",
            r["amount"], r["currency"], r["credited_amount"] or "",
            r["remark"] or "", r["status_label"], r["created_at"], r["sync_at"] or "",
        ])
    # BOM 让 Excel 直接按 UTF-8 打开，不出现乱码
    body = ("\ufeff" + buf.getvalue()).encode("utf-8")
    filename = f"recharges-{utcnow():%Y%m%d%H%M%S}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- 交易记录（只读）
# 独立于「充值记录」：那边是操作台（执行 / 核查 / 撤销），这边是只读交易流水，
# 列按 CRM 原型给（交易编号 / 交易类型 / 账户ID / 金额 / 备注 / 状态 / 反馈 /
# 操作人 / 操作时间），不提供任何动账按钮。

@router.get("/transactions", response_model=dict, summary="广告账户交易记录（只读）")
def list_transactions(
    db: DbSession,
    user: User = require_perm(Perm.RECHARGE_VIEW),
    date_from: date | None = None,
    date_to: date | None = None,
    txn_no: str | None = Query(None, description="交易编号 / 业务单号，模糊匹配"),
    txn_type: str | None = Query(None, description="RECHARGE=广告账户充值"),
    account_keyword: str | None = Query(None, description="广告账户 ID，模糊匹配"),
    status_f: str | None = Query(None, alias="status", description="交易状态"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return txn_svc.list_transactions(
        db,
        date_from=date_from,
        date_to=date_to,
        txn_no=txn_no,
        txn_type=txn_type,
        account_keyword=account_keyword,
        status_f=status_f,
        limit=limit,
        offset=offset,
    )


@router.get("/transactions/export", summary="导出交易记录 CSV")
def export_transactions(
    db: DbSession,
    user: User = require_perm(Perm.RECHARGE_VIEW),
    date_from: date | None = None,
    date_to: date | None = None,
    txn_no: str | None = None,
    txn_type: str | None = None,
    account_keyword: str | None = None,
    status_f: str | None = Query(None, alias="status"),
) -> Response:
    data = txn_svc.list_transactions(
        db,
        date_from=date_from,
        date_to=date_to,
        txn_no=txn_no,
        txn_type=txn_type,
        account_keyword=account_keyword,
        status_f=status_f,
        limit=10000,
        offset=0,
    )
    filename = f"transactions-{utcnow():%Y%m%d%H%M%S}.csv"
    return Response(
        content=txn_svc.export_csv(data["items"]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# 注意：/recharges/circuit 必须在 /recharges/{order_id}/... 之前注册
@router.get("/recharges/circuit", response_model=dict, summary="熔断开关列表")
def get_circuit(db: DbSession, user: User = require_perm(Perm.RECHARGE_VIEW)) -> dict:
    return {"items": svc.circuit_status(db)}


class CircuitIn(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    enabled: bool


@router.post("/recharges/circuit", response_model=dict, summary="设置熔断开关")
def set_circuit(
    body: CircuitIn, db: DbSession, user: User = require_perm(Perm.RECHARGE_RESOLVE)
) -> dict:
    result = svc.set_circuit(db, body.platform, body.enabled)
    db.commit()
    return result


class RechargeIn(BaseModel):
    ad_account_id: int
    amount: Decimal
    # 铁律 3：资金接口必须有请求级幂等键，前端每次点击生成一个
    request_no: str = Field(min_length=6, max_length=60)
    remark: str | None = Field(None, max_length=500)


@router.post("/recharges", response_model=dict, summary="人工发起加款（先冻结，request_no 幂等）")
def create_recharge(
    body: RechargeIn, db: DbSession, user: User = require_perm(Perm.RECHARGE_VIEW)
) -> dict:
    order, deduped = svc.create_recharge(
        db,
        ad_account_id=body.ad_account_id,
        amount=body.amount,
        request_no=body.request_no,
        remark=body.remark,
        operator=user.display_name,
    )
    item = _item(db, order)
    db.commit()
    return {**item, "deduped": bool(deduped)}


class ExecIn(BaseModel):
    outcome: Literal["SUCCESS", "UNKNOWN", "REJECTED"] = "SUCCESS"


@router.post("/recharges/{order_id}/exec", response_model=dict, summary="Mock RPA 执行（三态）")
def exec_recharge(
    order_id: int, body: ExecIn, db: DbSession,
    user: User = require_perm(Perm.RECHARGE_EXEC),
) -> dict:
    order = svc.execute(db, order_id, outcome=body.outcome, operator=user.display_name)
    account = db.get(AdAccount, order.ad_account_id)
    wallet = _wallet(db, order.client_id, order.currency)
    # 这是全系统唯一"真的把冻结的钱打出去"的动作，必须留痕：谁、何时、哪个 outcome
    svc.audit_exec(db, order, outcome=body.outcome, operator=user.display_name)
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "status_label": RECHARGE_STATUS_LABEL.get(order.status, str(order.status)),
        "serial_no": order.serial_no,
        "balance": str(money(wallet.balance)) if wallet else None,
        "account_balance": str(money(account.balance)) if account else None,
    }


class ResolveIn(BaseModel):
    result: Literal["SUCCESS", "FAILED"]
    reason: str | None = Field(None, max_length=300)


@router.post("/recharges/{order_id}/resolve", response_model=dict, summary="核查 unknown")
def resolve_recharge(
    order_id: int, body: ResolveIn, db: DbSession,
    user: User = require_perm(Perm.RECHARGE_RESOLVE),
) -> dict:
    order = svc.resolve(
        db, order_id, result=body.result, reason=body.reason, operator=user.display_name
    )
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "status_label": RECHARGE_STATUS_LABEL.get(order.status, str(order.status)),
    }


@router.post("/recharges/{order_id}/retry", response_model=dict, summary="重试失败/待核查单据")
def retry_recharge(
    order_id: int, db: DbSession, user: User = require_perm(Perm.RECHARGE_RESOLVE)
) -> dict:
    order = svc.retry(db, order_id, operator=user.display_name)
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "retry_count": int(order.retry_count or 0),
    }


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=300)


@router.post("/recharges/{order_id}/cancel", response_model=dict, summary="撤销加款单（释放冻结）")
def cancel_recharge(
    order_id: int, body: CancelIn, db: DbSession,
    user: User = require_perm(Perm.RECHARGE_RESOLVE),
) -> dict:
    order = svc.cancel(
        db, order_id, reason=body.reason, operator=user.display_name
    )
    text = (body.reason or "").strip()
    db.commit()
    return {
        "id": order.id,
        "status": str(order.status),
        "status_label": RECHARGE_STATUS_LABEL.get(order.status, str(order.status)),
        "reason": text,
    }
