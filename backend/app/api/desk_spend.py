"""D6 消耗（契约第 6 节 + 报表改版）：批量导入预校验 + 多维度报表 / 列表 / 导出。

消耗只扣广告账户，绝不动客户钱包（DESIGN 5）。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from ..models.user import User
from ..services import spend as svc_spend
from ..services.rbac import Perm, scope_sales_owner
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["消耗报表"], dependencies=[Depends(require_desk)])


class SpendImportIn(BaseModel):
    dry_run: bool = False
    rows: list[dict] = []


@router.post("/spend/import", response_model=dict, summary="批量导入消耗（dry_run 只预校验）")
def import_spend(
    body: SpendImportIn, db: DbSession, user: User = require_perm(Perm.SPEND_IMPORT)
) -> dict:
    report = svc_spend.import_spend(db, body.rows, dry_run=body.dry_run)
    if not body.dry_run:
        db.commit()
    return report


@router.get("/spend", response_model=dict, summary="消耗明细列表")
def list_spend(
    db: DbSession,
    user: User = require_perm(Perm.SPEND_IMPORT),
    date_from: date | None = None,
    date_to: date | None = None,
    client_id: int | None = None,
    account_id: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return svc_spend.list_spend(
        db, date_from=date_from, date_to=date_to, client_id=client_id,
        account_id=account_id, limit=limit, offset=offset,
    )


@router.get("/spend/report", response_model=dict, summary="消耗报表（客户/账户/销售/代理商）")
def spend_report(
    db: DbSession,
    user: User = require_perm(Perm.SPEND_IMPORT),
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: str = Query("client"),
    mode: str = Query("summary"),
    top_metric: str = Query("total"),
    client_keyword: str | None = None,
    sales_owner: str | None = None,
    platform: str | None = None,
    currency: str | None = None,
    top_limit: int = Query(10, ge=1, le=50),
) -> dict:
    """一次只算一个币种：跨币种求和无业务含义。

    `currencies` 是当前筛选条件下的可选币种，前端用它做下拉；
    未传 `currency` 时取消耗最大的币种。
    """
    today = date.today()
    to = date_to or today
    frm = date_from or to.replace(day=1)
    return svc_spend.spend_report(
        db,
        date_from=frm,
        date_to=to,
        group_by=group_by,
        mode=mode,
        top_metric=top_metric,
        client_keyword=client_keyword,
        sales_owner=sales_owner,
        platform=platform,
        currency=currency,
        sales_scope=scope_sales_owner(user),
        top_limit=top_limit,
    )


@router.get("/spend/report/export", summary="消耗报表导出（CSV，Excel 可直接打开）")
def export_spend_report(
    db: DbSession,
    user: User = require_perm(Perm.SPEND_IMPORT),
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: str = Query("client"),
    mode: str = Query("summary"),
    top_metric: str = Query("total"),
    client_keyword: str | None = None,
    sales_owner: str | None = None,
    platform: str | None = None,
    currency: str | None = None,
) -> Response:
    today = date.today()
    to = date_to or today
    frm = date_from or to.replace(day=1)
    report = svc_spend.spend_report(
        db,
        date_from=frm,
        date_to=to,
        group_by=group_by,
        mode=mode,
        top_metric=top_metric,
        client_keyword=client_keyword,
        sales_owner=sales_owner,
        platform=platform,
        currency=currency,
        sales_scope=scope_sales_owner(user),
    )
    body = "\ufeff" + svc_spend.report_csv(report)
    filename = f"spend-report-{frm:%Y%m%d}-{to:%Y%m%d}.csv"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
