"""客户门户 · 广告账户管理（/api/portal/ad-accounts）。

页面口径（客户端）：
- 以广告账户为主表：媒体 / 账户 / 余额 / 状态 / 时区 / 开户主体 / 备注；
- 账户充值只负责「发起」：冻结客户现金钱包 → 生成待执行加款单，
  真正出账仍要运营在后台点执行（铁律 4：钱只能由人点出去）；
- 客户身份只从登录态反查（user.client_id），绝不接受前端传入 client_id；
- 白名单返回：渠道 / 成本系数 / 返点率 / 代理商一律不下发（铁律 8）。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money
from ..models.client import AdAccount, Client, SpendDaily
from ..models.docs import OpenRequest, RechargeOrder
from ..models.enums import (
    AD_ACCOUNT_STATUS_LABEL,
    ENABLED_PLATFORMS,
    OPEN_REQUEST_STATUS_LABEL,
    PLATFORM_LABEL,
    RECHARGE_STATUS_LABEL,
    AdAccountStatus,
    ClientStatus,
    OwnerType,
    Platform,
    RechargeStatus,
)
from ..models.wallet import Wallet
from ..services import openreq as svc_open
from ..services import recharge as recharge_svc
from ..services.errors import StateError
from ..services.ledger import get_or_create_wallet
from .deps import DbSession, PortalUser

router = APIRouter(prefix="/api/portal", tags=["客户门户 · 广告账户"])

# 结果未知（TO_VERIFY）也属于「还没落地」，客户侧统一按处理中提示
PENDING_RECHARGE = (
    RechargeStatus.PENDING_EXEC,
    RechargeStatus.EXECUTING,
    RechargeStatus.AUDITING,
    RechargeStatus.TO_VERIFY,
)


def _iso(value) -> str | None:  # noqa: ANN001
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _client_open_map(
    db: Session, client_id: int, accounts: list[AdAccount]
) -> dict[int, OpenRequest]:
    """广告账户 → 开户单。

    对应关系存在开户单回执 JSON 里，有两种写法：
    `ad_account_ids`（主键数组）与 `accounts`（媒体侧账户 ID 字符串）。
    只扫当前客户自己的开户单，既省一次全表解析，也不会串到别人家的单。
    """
    if not accounts:
        return {}
    by_external = {a.account_id: a.id for a in accounts}
    rows = db.execute(
        select(OpenRequest).where(
            OpenRequest.client_id == client_id,
            OpenRequest.agent_receipt.is_not(None),
        )
    ).scalars().all()
    found: dict[int, OpenRequest] = {}
    for row in rows:
        try:
            payload = json.loads(row.agent_receipt or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        matched: list[int] = []
        for raw in payload.get("ad_account_ids") or []:
            try:
                matched.append(int(raw))
            except (TypeError, ValueError):
                continue
        for raw in payload.get("accounts") or []:
            aid = by_external.get(str(raw))
            if aid is not None:
                matched.append(aid)
        for aid in matched:
            if aid in by_external.values():
                found[aid] = row
    return found


def _recharge_meta(db: Session, client_id: int) -> dict[int, dict]:
    """每个账户最近一次充值时间 + 处理中笔数（客户侧只读聚合）。"""
    meta: dict[int, dict] = {}
    for aid, last_at in db.execute(
        select(RechargeOrder.ad_account_id, func.max(RechargeOrder.created_at))
        .where(
            RechargeOrder.client_id == client_id,
            # 撤销单不算「最近充值」：钱已解冻，列表上留个时间会看成已充过
            RechargeOrder.status != RechargeStatus.CANCELLED,
        )
        .group_by(RechargeOrder.ad_account_id)
    ).all():
        meta.setdefault(int(aid), {})["last_at"] = last_at
    for aid, count in db.execute(
        select(RechargeOrder.ad_account_id, func.count())
        .where(
            RechargeOrder.client_id == client_id,
            RechargeOrder.status.in_(PENDING_RECHARGE),
        )
        .group_by(RechargeOrder.ad_account_id)
    ).all():
        meta.setdefault(int(aid), {})["pending"] = int(count)
    return meta


def _row(account: AdAccount, order: OpenRequest | None, client: Client, meta: dict) -> dict:
    """客户可见字段白名单：不含 channel / cost_ratio / rebate_rate / 代理商。"""
    code = str(account.status)
    subject = (order.subject if order and order.subject else None) or client.company_name
    return {
        "id": account.id,
        "account_id": account.account_id,
        "platform": str(account.platform),
        "platform_label": PLATFORM_LABEL.get(account.platform, str(account.platform)),
        "bm_id": account.bm_id,
        "bc_ids": svc_open.split_bc_ids(order.bc_ids) if order else [],
        "timezone": account.timezone,
        "currency": account.currency,
        "balance": str(money(account.balance)),
        "daily_limit": str(money(account.daily_limit)),
        "status": code,
        "status_label": AD_ACCOUNT_STATUS_LABEL.get(account.status, code),
        "subject": subject,
        "remark": order.remark if order else None,
        "open_request_biz_no": order.biz_no if order else None,
        "open_request_status": str(order.status) if order else None,
        "open_request_status_label": (
            OPEN_REQUEST_STATUS_LABEL.get(order.status, str(order.status)) if order else None
        ),
        "opened_at": _iso(account.opened_at or account.created_at),
        "created_at": _iso(account.created_at),
        "last_recharge_at": _iso(meta.get("last_at")),
        "pending_recharge_count": int(meta.get("pending") or 0),
    }


def _wallet_buckets(db: Session, client_id: int) -> list[dict]:
    rows = db.execute(
        select(Wallet)
        .where(Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client_id)
        .order_by(Wallet.currency)
    ).scalars().all()
    return [
        {
            "currency": w.currency,
            "balance": str(money(w.balance)),
            "frozen": str(money(w.frozen)),
            "available": str(money(w.available)),
        }
        for w in rows
    ]


def _collect(
    db: Session,
    client_id: int,
    *,
    keyword: str | None = None,
    subject: str | None = None,
    platform: str | None = None,
    status_f: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[Client, list[dict], list[AdAccount]]:
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户档案不存在")

    conds = [AdAccount.client_id == client_id]
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        conds.append(or_(AdAccount.account_id.like(kw), AdAccount.bm_id.like(kw)))
    if platform and platform.strip():
        value = platform.strip().upper()
        if value not in {str(p) for p in Platform}:
            raise StateError(f"媒体平台取值非法：{platform}")
        conds.append(AdAccount.platform == value)
    if status_f and status_f.strip():
        value = status_f.strip().upper()
        if value not in {str(s) for s in AdAccountStatus}:
            raise StateError(f"账户状态取值非法：{status_f}")
        conds.append(AdAccount.status == value)

    # 开户时间筛选：老账户没写 opened_at 时退回 created_at，避免直接筛掉
    opened = func.coalesce(AdAccount.opened_at, AdAccount.created_at)
    if date_from:
        conds.append(opened >= datetime.combine(date_from, time.min))
    if date_to:
        conds.append(opened < datetime.combine(date_to + timedelta(days=1), time.min))

    rows = db.execute(
        select(AdAccount).where(*conds).order_by(AdAccount.id.desc())
    ).scalars().all()
    orders = _client_open_map(db, client_id, rows)
    meta = _recharge_meta(db, client_id)
    items = [_row(a, orders.get(a.id), client, meta.get(a.id, {})) for a in rows]

    # 开户主体是开户单上的字段，不在 ad_account 表里；数量按客户维度很小，
    # 放在内存里过滤比给 JSON 回执建索引更稳（也避免裸 SQL）。
    if subject and subject.strip():
        needle = subject.strip().lower()
        items = [it for it in items if needle in (it["subject"] or "").lower()]
    return client, items, rows


def _summary(items: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_currency: dict[str, dict] = {}
    for it in items:
        code = it["status"]
        by_status[code] = by_status.get(code, 0) + 1
        bucket = by_currency.setdefault(
            it["currency"], {"currency": it["currency"], "balance": money(0), "count": 0}
        )
        bucket["balance"] = money(bucket["balance"] + Decimal(it["balance"]))
        bucket["count"] += 1
    return {
        "by_status": by_status,
        "by_currency": [
            {"currency": v["currency"], "balance": str(v["balance"]), "count": v["count"]}
            for v in sorted(by_currency.values(), key=lambda x: x["currency"])
        ],
    }


def _options(rows: list[AdAccount]) -> dict:
    codes = {str(a.platform) for a in rows} | {str(p) for p in ENABLED_PLATFORMS}
    ordered = [str(p) for p in ENABLED_PLATFORMS]
    ordered += sorted(c for c in codes if c not in ordered)
    return {
        "platform": [
            {"code": code, "label": PLATFORM_LABEL.get(code, code)} for code in ordered
        ],
        "status": [
            {"code": str(s), "label": AD_ACCOUNT_STATUS_LABEL.get(s, str(s))}
            for s in (AdAccountStatus.NORMAL, AdAccountStatus.LIMITED, AdAccountStatus.BANNED)
        ],
    }


@router.get("/ad-accounts", response_model=dict, summary="我的广告账户（筛选 / 分页）")
def my_ad_accounts(
    db: DbSession,
    user: PortalUser,
    keyword: str | None = Query(None, description="广告账户 ID / BC ID，模糊匹配"),
    subject: str | None = Query(None, description="开户主体，模糊匹配"),
    platform: str | None = None,
    status_f: str | None = Query(None, alias="status"),
    date_from: date | None = Query(None, description="开户开始日期"),
    date_to: date | None = Query(None, description="开户结束日期"),
    limit: int = Query(10, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    client, items, rows = _collect(
        db, user.client_id, keyword=keyword, subject=subject, platform=platform,
        status_f=status_f, date_from=date_from, date_to=date_to,
    )
    total = len(items)
    return {
        "total": total,
        "items": items[offset: offset + limit],
        "summary": _summary(items),
        "wallets": _wallet_buckets(db, user.client_id),
        "can_recharge": client.status is ClientStatus.ACTIVE,
        "options": _options(rows),
    }


@router.get("/ad-accounts/export", summary="导出我的广告账户（CSV，不含渠道/返点）")
def export_my_ad_accounts(
    db: DbSession,
    user: PortalUser,
    keyword: str | None = None,
    subject: str | None = None,
    platform: str | None = None,
    status_f: str | None = Query(None, alias="status"),
    date_from: date | None = None,
    date_to: date | None = None,
) -> Response:
    _client, items, _rows = _collect(
        db, user.client_id, keyword=keyword, subject=subject, platform=platform,
        status_f=status_f, date_from=date_from, date_to=date_to,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "广告账户", "媒体", "开户主体", "时区", "BC ID", "账户余额", "币种",
        "日限额", "状态", "开户审核", "开户单号", "开户时间", "备注",
    ])
    for it in items[:10000]:
        writer.writerow([
            it["account_id"], it["platform_label"], it["subject"] or "",
            it["timezone"] or "", "、".join(it["bc_ids"]) or it["bm_id"] or "",
            it["balance"], it["currency"], it["daily_limit"], it["status_label"],
            it["open_request_status_label"] or "",
            it["open_request_biz_no"] or "", it["opened_at"] or "", it["remark"] or "",
        ])
    body = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ad-accounts.csv"'},
    )


def _portal_recharges(db: Session, account_id: int, limit: int = 20) -> dict:
    """客户侧充值记录：只给单号/金额/状态/时间，不带回执与内部备注。"""
    rows = db.execute(
        select(RechargeOrder)
        .where(RechargeOrder.ad_account_id == account_id)
        .order_by(RechargeOrder.id.desc())
        .limit(limit)
    ).scalars().all()
    total = db.execute(
        select(func.count()).select_from(RechargeOrder).where(
            RechargeOrder.ad_account_id == account_id
        )
    ).scalar_one()
    return {
        "total": int(total),
        "items": [
            {
                "biz_no": o.biz_no,
                "amount": str(money(o.amount)),
                "currency": o.currency,
                "status": str(o.status),
                "status_label": RECHARGE_STATUS_LABEL.get(o.status, str(o.status)),
                "created_at": _iso(o.created_at),
                "updated_at": _iso(o.updated_at),
            }
            for o in rows
        ],
    }


def _spend_points(db: Session, account_id: int, days: int = 30) -> dict:
    rows = db.execute(
        select(SpendDaily.date, func.sum(SpendDaily.spend))
        .where(SpendDaily.ad_account_id == account_id)
        .group_by(SpendDaily.date)
        .order_by(SpendDaily.date.desc())
        .limit(days)
    ).all()
    points = [{"date": d.isoformat(), "spend": str(money(s))} for d, s in reversed(rows)]
    total = sum((Decimal(p["spend"]) for p in points), Decimal("0"))
    return {"points": points, "total": str(money(total))}


@router.get("/ad-accounts/{account_id}", response_model=dict, summary="我的广告账户详情")
def my_ad_account_detail(account_id: int, db: DbSession, user: PortalUser) -> dict:
    account = db.get(AdAccount, account_id)
    # 别人的账户一律按不存在处理，避免枚举出账户是否存在
    if account is None or account.client_id != user.client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "广告账户不存在")
    client = db.get(Client, user.client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户档案不存在")

    order = _client_open_map(db, user.client_id, [account]).get(account.id)
    meta = _recharge_meta(db, user.client_id).get(account.id, {})
    wallet = get_or_create_wallet(
        db,
        owner_type=OwnerType.CLIENT,
        owner_id=user.client_id,
        currency=account.currency,
        create=False,
    )
    spend = _spend_points(db, account.id)
    return {
        "account": _row(account, order, client, meta),
        "wallet": (
            {
                "currency": wallet.currency,
                "balance": str(money(wallet.balance)),
                "frozen": str(money(wallet.frozen)),
                "available": str(money(wallet.available)),
            }
            if wallet
            else None
        ),
        "spend": {
            "currency": account.currency,
            "total": spend["total"],
            "points": spend["points"],
        },
        "recharges": _portal_recharges(db, account.id),
        "can_recharge": (
            client.status is ClientStatus.ACTIVE
            and account.status is not AdAccountStatus.BANNED
        ),
    }


class AccountRechargeIn(BaseModel):
    amount: Decimal = Field(gt=0)
    # 铁律 3：资金接口必须有请求级幂等键，前端每次点击生成一个
    request_no: str = Field(min_length=6, max_length=64)


@router.post(
    "/ad-accounts/{account_id}/recharge",
    response_model=dict,
    summary="发起广告账户充值（冻结现金钱包，待运营执行）",
)
def recharge_my_ad_account(
    account_id: int, body: AccountRechargeIn, db: DbSession, user: PortalUser
) -> dict:
    account = db.get(AdAccount, account_id)
    if account is None or account.client_id != user.client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "广告账户不存在")

    client = db.get(Client, user.client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户档案不存在")
    if client.status is not ClientStatus.ACTIVE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "客户未开通或已停用，暂不能发起充值"
        )
    if account.status is AdAccountStatus.BANNED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "账户已封停，不能充值")
    if recharge_svc.circuit_enabled(db, str(account.platform)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "该媒体平台暂停加款，请稍后再试或联系客户经理"
        )

    # 只发起：create_recharge 会冻结客户现金钱包并落 PENDING_EXEC 单，
    # 真正出账要运营在后台点执行；这里绝不调用 execute()。
    order, deduped = recharge_svc.create_recharge(
        db,
        ad_account_id=account.id,
        amount=body.amount,
        request_no=f"portal-{body.request_no.strip()}",
        operator=user.display_name,
    )
    wallet = get_or_create_wallet(
        db,
        owner_type=OwnerType.CLIENT,
        owner_id=user.client_id,
        currency=order.currency,
        create=False,
    )
    available = money(wallet.available) if wallet else Decimal("0")
    db.commit()
    return {
        "id": order.id,
        "biz_no": order.biz_no,
        "account_id": account.account_id,
        "amount": str(money(order.amount)),
        "currency": order.currency,
        "status": str(order.status),
        "status_label": RECHARGE_STATUS_LABEL.get(order.status, str(order.status)),
        "wallet_available": str(available),
        "deduped": bool(deduped),
        "message": (
            "该请求已提交过，返回原单（没有重复冻结）"
            if deduped
            else "充值申请已提交，金额已冻结，待运营执行到账"
        ),
    }
