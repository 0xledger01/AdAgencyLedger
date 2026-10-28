"""客户门户 · 交易记录（广告账户充值，只读）。

页面口径与内部后台一致，但只下发当前客户自己的单据：
交易编号 / 交易类型 / 广告账户ID / 交易金额 / 状态 / 反馈信息 / 操作人 / 操作时间。

铁律落点：
- `client_id` 只从登录态取（`PortalUser.client_id`），不接受 query/body 传入；
- 白名单返回：客户名 / 代理商 / 渠道 / 业务单号 / 重试次数一律不下发；
- 只读：不冻结、不记账，导出 CSV 同样只读。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response

from ..services import txn_records as txn_svc
from .deps import DbSession, PortalUser

router = APIRouter(prefix="/api/portal", tags=["客户门户 · 交易记录"])

TXN_TYPE_OPTIONS = [
    {"code": code, "label": label}
    for code, label in txn_svc.PORTAL_TXN_TYPE_LABEL.items()
]
STATUS_OPTIONS = [
    {"code": code, "label": label} for code, label in txn_svc.TXN_STATUS_LABEL.items()
]


@router.get("/transactions", response_model=dict, summary="我的交易记录（广告账户充值）")
def my_trade_records(
    db: DbSession,
    user: PortalUser,
    date_from: date | None = None,
    date_to: date | None = None,
    txn_no: str | None = Query(None, max_length=64, description="交易编号 / 业务单号，模糊匹配"),
    txn_type: str | None = Query(None, max_length=16, description="RECHARGE=广告账户充值"),
    account_keyword: str | None = Query(None, max_length=64, description="广告账户 ID，模糊匹配"),
    status_f: str | None = Query(None, alias="status", max_length=16, description="交易状态"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    data = txn_svc.list_client_transactions(
        db,
        client_id=user.client_id,
        date_from=date_from,
        date_to=date_to,
        txn_no=txn_no,
        txn_type=txn_type,
        account_keyword=account_keyword,
        status_f=status_f,
        limit=limit,
        offset=offset,
    )
    return {
        **data,
        "limit": limit,
        "offset": offset,
        "txn_type_options": TXN_TYPE_OPTIONS,
        "status_options": STATUS_OPTIONS,
    }


@router.get("/transactions/export", summary="我的交易记录导出 CSV")
def export_my_trade_records(
    db: DbSession,
    user: PortalUser,
    date_from: date | None = None,
    date_to: date | None = None,
    txn_no: str | None = None,
    txn_type: str | None = None,
    account_keyword: str | None = None,
    status_f: str | None = Query(None, alias="status"),
) -> Response:
    data = txn_svc.list_client_transactions(
        db,
        client_id=user.client_id,
        date_from=date_from,
        date_to=date_to,
        txn_no=txn_no,
        txn_type=txn_type,
        account_keyword=account_keyword,
        status_f=status_f,
        limit=10000,
        offset=0,
    )
    content = txn_svc.export_client_csv(data["items"])
    # 铁律 8 自检：客户导出文件里不能出现返点字样
    text = content.decode("utf-8-sig")
    for word in ("返点", "rebate", "佣金"):
        if word in text:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, "导出内容自检失败"
            )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="transactions.csv"'},
    )
