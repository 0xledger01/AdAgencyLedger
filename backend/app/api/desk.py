"""内部后台接口（/api/desk）。客户角色一律 403。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..models.base import money
from ..models.client import AdAccount, Client
from ..models.docs import AuditLog
from ..models.enums import (
    AD_ACCOUNT_STATUS_LABEL,
    CLIENT_STATUS_LABEL,
    CURRENCY_LABEL,
    ClientStatus,
    OwnerType,
)
from ..models.user import ClientReview, Role, User
from ..models.wallet import Wallet
from ..services import clients as svc_clients
from ..services import ledger_flow as lf
from ..services import ledger_views as lv
from ..services.errors import LedgerError, StateError
from ..services.rbac import Perm, require, scope_sales_owner
from .deps import CurrentUser, DbSession, DeskUser, require_desk, require_perm

# 整个路由组强制内部身份：客户 token 一律 403（铁律 3 / 权限红线 4）
router = APIRouter(
    prefix="/api/desk", tags=["内部后台"], dependencies=[Depends(require_desk)]
)


def _http(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ------------------------------------------------------------------ 客户

@router.get("/clients", response_model=dict, summary="客户列表")
def list_clients(
    db: DbSession,
    user: User = require_perm(Perm.CLIENT_LIST),
    status_f: str | None = Query(None, alias="status"),
    source_channel: str | None = None,
    keyword: str | None = None,
    name: str | None = Query(None, description="客户名称，模糊匹配本地名/CRM 名/CRM 编号"),
    codes: str | None = Query(None, description="客户编号，多个用逗号或空格分隔"),
    sales_owner: str | None = None,
    ae_owner: str | None = None,
    enabled: str | None = Query(None, pattern="^(true|false|unopened)$"),
    active: bool | None = None,
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    owner = scope_sales_owner(user)
    rows, total = svc_clients.list_clients(
        db, status=status_f, sales_owner=owner or sales_owner, ae_owner=ae_owner,
        source_channel=source_channel, keyword=keyword, name=name,
        codes=svc_clients.split_codes(codes), enabled=enabled, active=active,
        limit=limit, offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": svc_clients.client_rows(db, rows),
        "pending": svc_clients.pending_count(db, sales_owner=owner),
    }


def _client_filters(
    *,
    status_f: str | None,
    source_channel: str | None,
    keyword: str | None,
    name: str | None,
    codes: str | None,
    sales_owner: str | None,
    ae_owner: str | None,
    enabled: str | None,
    active: bool | None,
    user: User,
) -> dict:
    return dict(
        status=status_f, sales_owner=scope_sales_owner(user) or sales_owner,
        ae_owner=ae_owner, source_channel=source_channel, keyword=keyword,
        name=name, codes=svc_clients.split_codes(codes), enabled=enabled, active=active,
    )


@router.get("/clients/export", summary="客户列表导出（csv / xlsx）")
def export_clients(
    db: DbSession,
    user: User = require_perm(Perm.CLIENT_EXPORT),
    status_f: str | None = Query(None, alias="status"),
    source_channel: str | None = None,
    keyword: str | None = None,
    name: str | None = None,
    codes: str | None = None,
    sales_owner: str | None = None,
    ae_owner: str | None = None,
    enabled: str | None = Query(None, pattern="^(true|false|unopened)$"),
    active: bool | None = None,
    fmt: str = Query("xlsx", pattern="^(xlsx|csv)$"),
) -> Response:
    filters = _client_filters(
        status_f=status_f, source_channel=source_channel, keyword=keyword, name=name,
        codes=codes, sales_owner=sales_owner, ae_owner=ae_owner, enabled=enabled,
        active=active, user=user,
    )
    rows, _total = svc_clients.list_clients(db, limit=5000, offset=0, **filters)
    items = svc_clients.client_rows(db, rows)
    if fmt == "csv":
        content = svc_clients.export_clients_csv(items)
        media = "text/csv; charset=utf-8"
        filename = "clients.csv"
    else:
        content = svc_clients.export_clients_xlsx(items)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "clients.xlsx"
    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/clients/import", response_model=dict, summary="客户列表导入（CSV，按编号/邮箱更新）")
async def import_clients(
    db: DbSession,
    user: User = require_perm(Perm.CLIENT_IMPORT),
    file: UploadFile = File(...),
) -> dict:
    raw = await file.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件为空")
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "CSV 超过 2MB，请分批导入")
    try:
        result = svc_clients.import_clients(
            db, data=raw, operator=user, scope_owner=scope_sales_owner(user)
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.import",
            doctype="client",
            after=json.dumps(
                {"updated": result["updated"], "error_count": result["error_count"]},
                ensure_ascii=False,
            ),
            reason=f"导入客户资料：更新 {result['updated']} 行",
        )
    )
    db.commit()
    return {"filename": file.filename, **result}


class ClientCreateIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    login_email: str | None = Field(None, max_length=60)
    password: str | None = Field(None, min_length=6, max_length=128)
    contact_name: str | None = Field(None, max_length=60)
    phone: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=120)
    wechat: str | None = Field(None, max_length=60)
    crm_name: str | None = Field(None, max_length=200)
    crm_code: str | None = Field(None, max_length=60)
    sales_owner: str | None = Field(None, max_length=60)
    ae_owner: str | None = Field(None, max_length=60)
    source_channel: str | None = Field(None, max_length=40)
    settle_currency: str = "USD"
    credit_limit: Decimal | None = None
    monthly_spend_expect: Decimal | None = None
    activate: bool = True


@router.post("/clients", response_model=dict, summary="添加客户（可选同时开登录账号）")
def create_client(
    body: ClientCreateIn, db: DbSession, user: User = require_perm(Perm.CLIENT_EDIT)
) -> dict:
    if body.credit_limit is not None and body.credit_limit > 0:
        # 授信额度是财务口径，运营/销售不能自批
        from ..services.rbac import can

        if not can(user, Perm.CLIENT_LIMIT):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "授信额度只能由财务/管理员设置")
    try:
        c, account, initial_password = svc_clients.create_client(
            db, operator=user, **body.model_dump()
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.create",
            doctype="client",
            doc_id=c.id,
            after=json.dumps(
                {"biz_no": c.biz_no, "login": account.username if account else None},
                ensure_ascii=False,
            ),
            reason=f"添加客户：{c.company_name}",
        )
    )
    db.commit()
    return {
        "id": c.id,
        "biz_no": c.biz_no,
        "status": str(c.status),
        "login_email": account.username if account else None,
        "initial_password": initial_password,
    }


class ClientBatchIn(BaseModel):
    ids: list[int] = Field(min_length=1)
    sales_owner: str | None = None
    ae_owner: str | None = None
    credit_limit: Decimal | None = None
    enabled: bool | None = None


@router.post("/clients/batch", response_model=dict, summary="批量修改客户（归属 / AE / 授信 / 启用）")
def batch_clients(
    body: ClientBatchIn, db: DbSession, user: User = require_perm(Perm.CLIENT_EDIT)
) -> dict:
    from ..services.rbac import can

    if body.credit_limit is not None and not can(user, Perm.CLIENT_LIMIT):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "授信额度只能由财务/管理员修改")
    if body.enabled is not None and not can(user, Perm.CLIENT_DISABLE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "启用/停用需要 client.disable 权限")
    try:
        clients = svc_clients.batch_update_clients(
            db, ids=body.ids, operator=user,
            sales_owner=body.sales_owner, ae_owner=body.ae_owner,
            credit_limit=body.credit_limit, enabled=body.enabled,
            scope_owner=scope_sales_owner(user),
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.batch_update",
            doctype="client",
            after=json.dumps(body.model_dump(mode="json"), ensure_ascii=False),
            reason=f"批量修改 {len(clients)} 个客户",
        )
    )
    db.commit()
    return {"updated": len(clients), "ids": [c.id for c in clients]}


@router.post("/clients/sync", response_model=dict, summary="同步 CRM 客户编号/名称")
def sync_clients(db: DbSession, user: User = require_perm(Perm.CLIENT_EDIT)) -> dict:
    from ..services import crm

    try:
        result = crm.sync_from_crm(db)
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.sync",
            doctype="client",
            after=json.dumps(result, ensure_ascii=False),
            reason="同步 CRM 客户",
        )
    )
    db.commit()
    return result


class ClientUpdateIn(BaseModel):
    company_name: str | None = None
    login_email: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    wechat: str | None = None
    crm_name: str | None = None
    crm_code: str | None = None
    sales_owner: str | None = None
    ae_owner: str | None = None
    source_channel: str | None = None
    website: str | None = None
    registered_place: str | None = None
    industry: str | None = None
    target_regions: str | None = None
    monthly_spend_expect: Decimal | None = None
    credit_limit: Decimal | None = None


@router.post("/clients/{client_id}/update", response_model=dict, summary="编辑客户资料")
def update_client(
    client_id: int, body: ClientUpdateIn, db: DbSession,
    user: User = require_perm(Perm.CLIENT_EDIT),
) -> dict:
    from ..services.rbac import can

    c = db.get(Client, client_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户不存在")
    owner = scope_sales_owner(user)
    if owner and c.sales_owner != owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能编辑自己名下客户")
    if body.sales_owner is not None and owner and body.sales_owner != owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "销售不能把客户转给别人")

    data = body.model_dump(exclude_unset=True)
    credit_limit = data.pop("credit_limit", None)
    if credit_limit is not None and not can(user, Perm.CLIENT_LIMIT):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "授信额度只能由财务/管理员修改")
    try:
        c, initial_password = svc_clients.update_client(
            db, client_id=client_id, operator=user, data=data
        )
        if credit_limit is not None:
            svc_clients.set_terms(db, client_id=client_id, operator=user, credit_limit=credit_limit)
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.update",
            doctype="client",
            doc_id=c.id,
            after=json.dumps(
                {k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()},
                ensure_ascii=False,
            ),
            reason="编辑客户资料",
        )
    )
    db.commit()
    return {
        "id": c.id,
        "company_name": c.company_name,
        "login_email": data.get("login_email"),
        "initial_password": initial_password,
    }


class ResetPasswordIn(BaseModel):
    password: str | None = Field(None, min_length=6, max_length=128)


@router.post(
    "/clients/{client_id}/reset-password", response_model=dict,
    summary="重置客户门户密码（返回一次性明文）",
)
def reset_client_password(
    client_id: int, body: ResetPasswordIn, db: DbSession,
    user: User = require_perm(Perm.CLIENT_RESET_PASSWORD),
) -> dict:
    c = db.get(Client, client_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户不存在")
    try:
        account, plain = svc_clients.reset_client_password(
            db, client_id=client_id, password=body.password
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.reset_password",
            doctype="client",
            doc_id=client_id,
            after=json.dumps({"username": account.username}, ensure_ascii=False),
            reason="重置客户门户密码",
        )
    )
    db.commit()
    return {
        "client_id": client_id,
        "username": account.username,
        "password": plain,
        "note": "明文只显示这一次，请立即交给客户并建议首次登录后修改",
    }


@router.get("/clients/{client_id}", response_model=dict, summary="客户详情（多 Tab 数据）")
def client_detail(client_id: int, db: DbSession, user: User = require_perm(Perm.CLIENT_VIEW)) -> dict:
    c = db.get(Client, client_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户不存在")
    if scope_sales_owner(user) and c.sales_owner != user.display_name:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己名下客户")

    wallets = db.query(Wallet).filter(
        Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client_id
    ).all()
    accounts = db.query(AdAccount).filter(AdAccount.client_id == client_id).all()
    login = db.execute(
        select(User)
        .where(User.role == Role.CLIENT, User.client_id == client_id)
        .order_by(User.is_primary.desc(), User.id)
    ).scalars().first()

    profile = {
        "id": c.id, "biz_no": c.biz_no, "company_name": c.company_name,
        "crm_name": c.crm_name, "crm_code": c.crm_code,
        "ae_owner": c.ae_owner, "created_by": c.created_by,
        "login_email": login.username if login else None,
        "login_active": bool(login.active) if login else None,
        "last_active_at": c.last_active_at.isoformat(sep=" ", timespec="seconds") if c.last_active_at else None,
        "credit_code": c.credit_code, "registered_place": c.registered_place,
        "founded_at": c.founded_at, "contact_name": c.contact_name, "phone": c.phone,
        "email": c.email, "wechat": c.wechat, "platforms": c.platforms,
        "industry": c.industry, "target_regions": c.target_regions,
        "monthly_spend_expect": str(money(c.monthly_spend_expect)),
        "settle_currency": c.settle_currency, "source_channel": c.source_channel,
        "sales_owner": c.sales_owner, "status": str(c.status),
        "status_label": CLIENT_STATUS_LABEL.get(c.status, str(c.status)),
        "version": c.version, "reject_reason": c.reject_reason,
        "license_url": c.license_url, "idcard_url": c.idcard_url,
        "website": c.website, "qualification_url": c.qualification_url,
        "settle_intent": c.settle_intent,
        "created_at": c.created_at.isoformat(sep=" ", timespec="seconds"),
        "activated_at": c.activated_at.isoformat(sep=" ", timespec="seconds") if c.activated_at else None,
    }

    # 商务条款仅内部可见，且需相应权限
    terms = None
    from ..services.rbac import can
    if can(user, Perm.CLIENT_LIMIT):
        terms = {
            "cost_ratio": str(money(c.cost_ratio)),
            "rebate_rate": str(money(c.rebate_rate)),
            "credit_limit": str(money(c.credit_limit)),
            "credit_used": str(money(c.credit_used)),
            "payment_terms": c.payment_terms,
            "channel": c.channel,
        }

    history = [
        {
            "id": r.id, "version": r.version, "step": r.step, "action": r.action,
            "from_status": r.from_status, "to_status": r.to_status,
            "operator": r.operator, "operator_role": r.operator_role,
            "reason": r.reason,
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
        }
        for r in svc_clients.review_history(db, client_id)
    ]

    return {
        "profile": profile,
        "terms": terms,
        "wallets": [
            {"id": w.id, "currency": w.currency, "balance": str(money(w.balance)),
             "frozen": str(money(w.frozen)), "available": str(w.available)}
            for w in wallets
        ],
        "accounts": [
            {"id": a.id, "account_id": a.account_id, "platform": str(a.platform),
             "currency": a.currency, "balance": str(money(a.balance)),
             "daily_limit": str(money(a.daily_limit)), "status": str(a.status),
             "channel": a.channel}
            for a in accounts
        ],
        "reviews": history,
        "next_step": svc_clients.CURRENT_STEP.get(c.status),
    }


class ReviewIn(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(None, max_length=500)


@router.post("/clients/{client_id}/review", response_model=dict, summary="入驻审核（初审/风控/定额）")
def do_review(client_id: int, body: ReviewIn, db: DbSession, user: User = require_perm(Perm.CLIENT_AUDIT)) -> dict:
    try:
        c = svc_clients.review(
            db, client_id=client_id, action=body.action, operator=user, reason=body.reason
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.commit()
    return {"id": c.id, "status": str(c.status),
            "status_label": CLIENT_STATUS_LABEL.get(c.status, str(c.status))}


class TermsIn(BaseModel):
    cost_ratio: Decimal | None = None
    rebate_rate: Decimal | None = None
    credit_limit: Decimal | None = None
    payment_terms: int | None = None
    channel: str | None = None


@router.post("/clients/{client_id}/terms", response_model=dict, summary="设置商务条款 / 定额度")
def set_terms(client_id: int, body: TermsIn, db: DbSession, user: User = require_perm(Perm.CLIENT_LIMIT)) -> dict:
    try:
        c = svc_clients.set_terms(
            db, client_id=client_id, operator=user,
            cost_ratio=body.cost_ratio, rebate_rate=body.rebate_rate,
            credit_limit=body.credit_limit, payment_terms=body.payment_terms,
            channel=body.channel,
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.commit()
    return {"id": c.id, "cost_ratio": str(money(c.cost_ratio)),
            "rebate_rate": str(money(c.rebate_rate)),
            "credit_limit": str(money(c.credit_limit)),
            "payment_terms": c.payment_terms, "channel": c.channel}


# ------------------------------------------------------------------ D3.1 流水

@router.get("/client-ledger", response_model=dict, summary="D3.1 客户往来流水 / 全量流水")
def client_ledger(
    db: DbSession,
    user: User = require_perm(Perm.LEDGER_VIEW),
    client_id: int | None = None,
    currency: str | None = None,
    biz_type: str | None = None,
    direction: str | None = Query(None, pattern="^(IN|OUT)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    payer: str | None = None,
    operator: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    owner = scope_sales_owner(user)
    filters = dict(
        currency=currency, biz_type=biz_type, direction=direction,
        date_from=date_from, date_to=date_to, amount_min=amount_min, amount_max=amount_max,
        payer=payer, operator=operator,
    )
    if client_id:
        if owner:
            c = db.get(Client, client_id)
            if c is None or c.sales_owner != owner:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己名下客户")
        return lv.client_ledger(db, client_id=client_id, limit=limit, offset=offset, **filters)

    return lv.global_ledger(db, sales_owner=owner, limit=limit, offset=offset, **filters)


@router.get("/client-ledger/export", summary="D3.1 流水导出（csv / xlsx）")
def export_ledger(
    db: DbSession,
    user: User = require_perm(Perm.LEDGER_EXPORT),
    client_id: int | None = None,
    fmt: str = Query("xlsx", pattern="^(xlsx|csv)$"),
    currency: str | None = None,
    biz_type: str | None = None,
    direction: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    view: str = Query("client", pattern="^(client|global|spend)$"),
) -> Response:
    # 账户消耗是另一张表（spend_daily），导出走单独分支（F-17）
    if view == "spend":
        owner = scope_sales_owner(user)
        client_ids = None
        if owner:
            client_ids = db.execute(
                select(Client.id).where(Client.sales_owner == owner)
            ).scalars().all()
        data = lv.account_ledger(
            db, client_id=client_id, client_ids=client_ids,
            date_from=date_from, date_to=date_to, limit=10000, offset=0,
        )
        items = data["items"]
        spend_summary = data["summary"]["per_currency"]
        if fmt == "csv":
            content = lv.export_spend_csv(items, spend_summary)
            media = "text/csv; charset=utf-8"
            filename = "account-spend.csv"
        else:
            content = lv.export_spend_xlsx(items, spend_summary)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            filename = "account-spend.xlsx"
        return Response(
            content=content, media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    filters = dict(currency=currency, biz_type=biz_type, direction=direction,
                   date_from=date_from, date_to=date_to)
    if client_id:
        data = lv.client_ledger(db, client_id=client_id, limit=10000, offset=0, **filters)
        items = data["items"]
        summary = data["summary"]["per_currency"]
    else:
        data = lv.global_ledger(db, sales_owner=scope_sales_owner(user), limit=10000, offset=0, **filters)
        items = data["items"]
        summary = []

    if fmt == "csv":
        content = lv.export_csv(items, summary)
        media = "text/csv; charset=utf-8"
        filename = "client-ledger.csv"
    else:
        content = lv.export_xlsx(items, summary)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "client-ledger.xlsx"

    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/fund-flow", response_model=dict, summary="资金流水（台账口径：一行一笔钱）")
def fund_flow(
    db: DbSession,
    user: User = require_perm(Perm.LEDGER_VIEW),
    agent: str | None = Query(None, description="代理商名称，精确匹配（账户渠道优先，其次客户渠道）"),
    bill_no: str | None = Query(None, description="账单 ID，模糊匹配"),
    client_keyword: str | None = Query(None, description="客户名称 / 客户编号，模糊匹配"),
    txn_type: str | None = Query(None, description="交易类型：现金账户充值 / 广告账户充值 等"),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict:
    """运营台账：不拆 Tab，五个筛选 + 分页，代理商/广告账户只对账户类流水有值。"""
    try:
        return lf.fund_flow(
            db,
            agent=agent,
            bill_no=bill_no,
            client_keyword=client_keyword,
            txn_type=txn_type,
            date_from=date_from,
            date_to=date_to,
            sales_owner=scope_sales_owner(user),
            page=page,
            page_size=page_size,
        )
    except LedgerError as exc:
        raise _http(exc) from exc


@router.get("/fund-flow/export", summary="资金流水导出（csv / xlsx）")
def export_fund_flow(
    db: DbSession,
    user: User = require_perm(Perm.LEDGER_EXPORT),
    agent: str | None = None,
    bill_no: str | None = None,
    client_keyword: str | None = None,
    txn_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    fmt: str = Query("xlsx", pattern="^(xlsx|csv)$"),
) -> Response:
    try:
        data = lf.fund_flow(
            db,
            agent=agent,
            bill_no=bill_no,
            client_keyword=client_keyword,
            txn_type=txn_type,
            date_from=date_from,
            date_to=date_to,
            sales_owner=scope_sales_owner(user),
            page=1,
            page_size=10000,
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    if fmt == "csv":
        content = lf.export_csv(data["items"])
        media = "text/csv; charset=utf-8"
        filename = "fund-flow.csv"
    else:
        content = lf.export_xlsx(data["items"])
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "fund-flow.xlsx"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ReconcileIn(BaseModel):
    tx_ids: list[int] = Field(min_length=1)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    remark: str | None = None


@router.post("/client-ledger/reconcile", response_model=dict, summary="标记核销")
def reconcile(body: ReconcileIn, db: DbSession, user: User = require_perm(Perm.LEDGER_RECONCILE)) -> dict:
    try:
        result = lv.mark_reconciled(
            db, tx_ids=body.tx_ids, period=body.period, operator=user, remark=body.remark
        )
    except LedgerError as exc:
        raise _http(exc) from exc
    db.commit()
    return result


class ReverseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)
    # 冲正必须两人确认（与充值单冲正口径一致）：第二签人必填且不能是操作人本人
    second_approver: str | None = Field(None, max_length=60)


@router.post("/client-ledger/{tx_id}/reverse", response_model=dict, summary="冲正（双签，只新增反向流水）")
def reverse_tx(tx_id: int, body: ReverseIn, db: DbSession, user: User = require_perm(Perm.TOPUP_ADJUST)) -> dict:
    approver = (body.second_approver or "").strip()
    if not approver:
        raise StateError("冲正必须填写第二签人")
    if approver == user.display_name:
        raise StateError("冲正需两人确认：第二签人不能是操作人本人")
    try:
        new_tx = lv.reverse_tx(db, tx_id=tx_id, operator=user, reason=body.reason)
    except LedgerError as exc:
        raise _http(exc) from exc
    db.add(
        AuditLog(
            actor=user.display_name,
            action="ledger.reverse",
            doctype="wallet_transaction",
            doc_id=new_tx.id,
            after=json.dumps(
                {"origin_tx_id": tx_id, "amount": str(money(new_tx.amount)),
                 "second_approver": approver},
                ensure_ascii=False,
            ),
            reason=f"冲正 #{tx_id}：{body.reason}",
        )
    )
    db.commit()
    return {"id": new_tx.id, "seq": new_tx.seq, "amount": str(money(new_tx.amount)),
            "direction": str(new_tx.direction), "balance_after": str(money(new_tx.balance_after))}


@router.get("/account-ledger", response_model=dict, summary="D3.1 账户消耗流水")
def account_ledger(
    db: DbSession,
    user: User = require_perm(Perm.LEDGER_VIEW),
    client_id: int | None = None,
    ad_account_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    # 与 client-ledger 保持同一隔离口径：销售只能看自己名下客户
    owner = scope_sales_owner(user)
    if owner:
        mine = db.execute(select(Client.id).where(Client.sales_owner == owner)).scalars().all()
        if client_id:
            if client_id not in set(mine):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己名下客户")
            return lv.account_ledger(
                db, client_id=client_id, ad_account_id=ad_account_id,
                date_from=date_from, date_to=date_to, limit=limit, offset=offset,
            )
        return lv.account_ledger(
            db, client_ids=list(mine), ad_account_id=ad_account_id,
            date_from=date_from, date_to=date_to, limit=limit, offset=offset,
        )
    return lv.account_ledger(
        db, client_id=client_id, ad_account_id=ad_account_id,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )


UNASSIGNED_AGENT = "未归属渠道"


@router.get("/wallets/overview", response_model=dict, summary="资金总览（按一代 / 按客户分组）")
def wallets_overview(db: DbSession, user: User = require_perm(Perm.WALLET_VIEW)) -> dict:
    """资金总览的两个口径（业务方拍定，勿自行改回按币种一列了事）：

    1. **广告账户余额 → 按一代分组**：钱加到了哪个一代名下、每个一代还有多少。
    2. **平台钱包 → 按客户分组**：平台钱包就是客户充值进来的钱（= 客户子钱包），
       所以管理端只在这里按客户拆开看，不再单列一个「客户子钱包」区块 —— 那是同一份钱。
       一代钱包区块同理取消：它和广告账户余额口径重复，区别只是"挂在哪个一代"。

    铁律：**禁止跨币种求和**。所有合计都带 currency，USD/HKD 不叠加。
    销售只能看自己名下客户，看不到别人的池子。
    """
    from sqlalchemy import func as sa_func

    owner = scope_sales_owner(user)
    mine = select(Client.id).where(Client.sales_owner == owner) if owner else None

    # ---------------------------------------------------------- 广告账户（按一代）
    acc_stmt = select(
        AdAccount.channel,
        AdAccount.currency,
        sa_func.coalesce(sa_func.sum(AdAccount.balance), 0),
        sa_func.count(),
    )
    if mine is not None:
        acc_stmt = acc_stmt.where(AdAccount.client_id.in_(mine))
    acc_rows = db.execute(
        acc_stmt.group_by(AdAccount.channel, AdAccount.currency)
    ).all()

    by_agent = [
        {
            "agent": (ch or UNASSIGNED_AGENT),
            "currency": ccy or "USD",
            "balance": str(money(bal or 0)),
            "count": int(n),
        }
        for ch, ccy, bal, n in acc_rows
    ]
    # 一代内按金额降序，方便一眼看到钱主要压在哪个渠道
    by_agent.sort(key=lambda r: (r["agent"], r["currency"]))

    acc_total: dict[str, dict] = {}
    for r in by_agent:
        agg = acc_total.setdefault(r["currency"], {"currency": r["currency"], "balance": money(0), "count": 0})
        agg["balance"] = money(agg["balance"] + money(r["balance"]))
        agg["count"] += r["count"]

    # 逐个广告账户的卡片（资金总览主视图）：
    # 卡片的"账户"就是 AdAccount，余额是媒体侧余额，不是客户钱包负债。
    accounts_stmt = select(AdAccount, Client).join(Client, Client.id == AdAccount.client_id)
    if mine is not None:
        accounts_stmt = accounts_stmt.where(AdAccount.client_id.in_(mine))
    account_rows = db.execute(
        accounts_stmt.order_by(AdAccount.channel, Client.company_name, AdAccount.id)
    ).all()
    accounts = [
        {
            "id": a.id,
            "account_id": a.account_id,
            "client_id": c.id,
            "client_name": c.company_name,
            "platform": str(a.platform),
            "channel": a.channel or UNASSIGNED_AGENT,
            "currency": a.currency or "USD",
            "currency_label": CURRENCY_LABEL.get(a.currency or "USD", a.currency or "USD"),
            "balance": str(money(a.balance)),
            "status": str(a.status),
            "status_label": AD_ACCOUNT_STATUS_LABEL.get(a.status, str(a.status)),
            "updated_at": a.updated_at.isoformat(sep=" ", timespec="seconds") if a.updated_at else None,
        }
        for a, c in account_rows
    ]

    # ---------------------------------------------------------- 平台钱包（按客户）
    wallet_stmt = (
        select(Wallet, Client)
        .join(Client, Client.id == Wallet.owner_id)
        .where(Wallet.owner_type == OwnerType.CLIENT)
    )
    if mine is not None:
        wallet_stmt = wallet_stmt.where(Wallet.owner_id.in_(mine))
    wallet_rows = db.execute(
        wallet_stmt.order_by(Client.company_name, Wallet.currency)
    ).all()

    by_client = [
        {
            "client_id": c.id,
            "client_name": c.company_name,
            "biz_no": c.biz_no,
            "currency": w.currency,
            "balance": str(money(w.balance)),
            "frozen": str(money(w.frozen)),
            "available": str(money(w.available)),
        }
        for w, c in wallet_rows
    ]
    # 只保留有余额或有冻结的客户，避免一屏全是 0（0 余额客户仍可在客户管理里看到）
    by_client = [r for r in by_client if money(r["balance"]) != 0 or money(r["frozen"]) != 0]

    cli_total: dict[str, dict] = {}
    for r in by_client:
        agg = cli_total.setdefault(r["currency"], {"currency": r["currency"], "balance": money(0), "count": 0})
        agg["balance"] = money(agg["balance"] + money(r["balance"]))
        agg["count"] += 1

    def dump(d: dict[str, dict], key: str = "balance") -> list[dict]:
        return [d[c] for c in sorted(d)]

    return {
        "scope": "OWN" if owner else "ALL",
        "accounts": accounts,
        "ad_accounts": {
            "by_agent": by_agent,
            "total": dump(acc_total),
        },
        "client_wallets": {
            "by_client": by_client,
            "total": dump(cli_total),
        },
    }


# ------------------------------------------------------------------ 枚举字典

@router.get("/enums", response_model=dict, summary="状态/方式枚举字典（前端不自己写映射）")
def enum_dict(db: DbSession, user: DeskUser) -> dict:
    """状态筛选下拉必须来自固定枚举，不能靠"列表里见过的标签"攒出来 ——
    列表为空时下拉就会是空的，任何状态都筛不出来（F-15）。"""
    from ..models.client import AgentChannel
    from ..models.enums import (
        AD_ACCOUNT_STATUS_LABEL,
        BIZ_TYPE_LABEL,
        CLIENT_LEAD_STATUS_LABEL,
        CLIENT_STATUS_LABEL,
        ENABLED_PLATFORM_LABEL,
        FUND_ACCOUNT_TYPE_LABEL,
        FUND_OP_TYPE_LABEL,
        OPEN_COUNTRY_OPTIONS,
        OPEN_INDUSTRY_OPTIONS,
        OPEN_REGISTERED_PLACE_OPTIONS,
        OPEN_REQUEST_STATUS_LABEL,
        RECHARGE_STATUS_LABEL,
        TICKET_PRIORITY_LABEL,
        TICKET_STATUS_LABEL,
        TOPUP_STATUS_LABEL,
    )
    from ..services.recharge import TXN_TYPE_LABEL
    from ..services.txn_records import TXN_STATUS_LABEL as TXN_RECORD_STATUS_LABEL
    from ..services.txn_records import TXN_TYPE_LABEL as TXN_RECORD_TYPE_LABEL

    def dump(mapping: dict) -> list[dict]:
        return [{"code": str(k), "label": v} for k, v in mapping.items()]

    agents = db.execute(
        select(AgentChannel.name).order_by(AgentChannel.name)
    ).scalars().all()

    return {
        "client_status": dump(CLIENT_STATUS_LABEL),
        "topup_status": dump(TOPUP_STATUS_LABEL),
        "fund_account_type": dump(FUND_ACCOUNT_TYPE_LABEL),
        "fund_op_type": dump(FUND_OP_TYPE_LABEL),
        "recharge_status": dump(RECHARGE_STATUS_LABEL),
        "recharge_txn_type": dump(TXN_TYPE_LABEL),
        # 交易记录页：交易类型 / 执行口径状态（与充值单精确状态分开）
        "txn_record_type": dump(TXN_RECORD_TYPE_LABEL),
        "txn_record_status": dump(TXN_RECORD_STATUS_LABEL),
        "open_request_status": dump(OPEN_REQUEST_STATUS_LABEL),
        # 客户申请单里的企业/账户字段是代码，审批抽屉要显示中文
        "open_country": dump(OPEN_COUNTRY_OPTIONS),
        "open_industry": dump(OPEN_INDUSTRY_OPTIONS),
        "registered_place": dump(OPEN_REGISTERED_PLACE_OPTIONS),
        "biz_type": dump(BIZ_TYPE_LABEL),
        "ad_account_status": dump(AD_ACCOUNT_STATUS_LABEL),
        # 问题工单：状态筛选与处理下拉都取这里，前端不自己写中文
        "ticket_status": dump(TICKET_STATUS_LABEL),
        "ticket_priority": dump(TICKET_PRIORITY_LABEL),
        # 客户申请（销售线索）：标签页 / 跟进下拉都取这里
        "client_lead_status": dump(CLIENT_LEAD_STATUS_LABEL),
        # 充值只做美金：不再提供人民币（CNY）
        "currency": [{"code": c, "label": c} for c in ("USD", "HKD")],
        # 当前只做 TikTok：筛选/新建下拉只给 TikTok，存量历史媒体仍按数据原样展示。
        "platform": dump(ENABLED_PLATFORM_LABEL),
        "agent_channel": [{"code": a, "label": a} for a in agents],
    }


# ------------------------------------------------------------------ 客户停用

class DisableIn(BaseModel):
    disabled: bool
    reason: str = Field(min_length=1, max_length=300)


@router.post("/clients/{client_id}/disable", response_model=dict, summary="停用 / 启用客户")
def disable_client(
    client_id: int, body: DisableIn, db: DbSession,
    user: User = require_perm(Perm.CLIENT_DISABLE),
) -> dict:
    """Perm.CLIENT_DISABLE 之前定义了却没有任何路由在用：客户违约时无法停用（F-07 #8）。

    停用只改状态，不动余额；`ensure_client_wallet` 会在资金操作时拦住非 ACTIVE 客户。
    """
    c = db.get(Client, client_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户不存在")
    target = ClientStatus.DISABLED if body.disabled else ClientStatus.ACTIVE
    if c.status is target:
        return {"id": c.id, "status": str(c.status),
                "status_label": CLIENT_STATUS_LABEL.get(c.status, str(c.status))}

    before = str(c.status)
    c.status = target
    db.add(
        AuditLog(
            actor=user.display_name,
            action="client.disable" if body.disabled else "client.enable",
            doctype="client",
            doc_id=c.id,
            before=before,
            after=str(target),
            reason=body.reason,
        )
    )
    db.commit()
    return {"id": c.id, "status": str(c.status),
            "status_label": CLIENT_STATUS_LABEL.get(c.status, str(c.status))}


# ------------------------------------------------------------------ 账本全量自检

@router.post("/wallets/verify-all", response_model=dict, summary="全量钱包流水链自检")
def verify_all_wallets(db: DbSession, user: User = require_perm(Perm.WALLET_VIEW)) -> dict:
    """账本系统唯一的体检入口：逐个钱包校验 seq 连续 + balance_after 首尾相接。

    /api/dev/wallets/{id}/verify 没有鉴权也没出现在任何页面，等于没有（F-26）。
    """
    from ..services.ledger import verify_wallet_chain

    rows = db.execute(select(Wallet.id)).scalars().all()
    ok = 0
    broken: list[dict] = []
    for wid in rows:
        try:
            verify_wallet_chain(db, wid)
            ok += 1
        except LedgerError as exc:
            broken.append({"wallet_id": wid, "reason": str(exc)})
    db.add(
        AuditLog(
            actor=user.display_name,
            action="ledger.verify_all",
            doctype="wallet",
            after=json.dumps({"total": len(rows), "ok": ok, "broken": len(broken)},
                             ensure_ascii=False),
            reason="全量账本自检",
        )
    )
    db.commit()
    return {"total": len(rows), "ok": ok, "broken": len(broken), "details": broken[:20]}
