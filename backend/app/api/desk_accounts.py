"""账户管理：广告账户列表 / 详情 / 导出。

页面从"加款任务"改为"账户管理"后的只读入口：
- 列表按账户维度展示余额、时区、开户主体、代理商、授权状态；
- 详情带客户档案、客户钱包、开户单与最近充值记录；
- 充值动作仍然走 `desk_recharge.py` 的冻结 → 执行链路，本模块不碰钱。
"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func as sa_func
from sqlalchemy import or_, select

from ..models.base import money, utcnow
from ..models.client import AdAccount, Client
from ..models.docs import OpenRequest, RechargeOrder
from ..models.enums import (
    AD_ACCOUNT_STATUS_LABEL,
    CLIENT_STATUS_LABEL,
    OPEN_REQUEST_STATUS_LABEL,
    PLATFORM_LABEL,
    AdAccountStatus,
    OwnerType,
    Platform,
    RechargeStatus,
)
from ..models.user import User
from ..models.wallet import Wallet
from ..services import recharge as recharge_svc
from ..services.errors import StateError
from ..services.rbac import Perm, can, scope_sales_owner
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["账户管理"], dependencies=[Depends(require_desk)])

UNASSIGNED_AGENT = "未归属渠道"


def _iso(value) -> str | None:  # noqa: ANN001
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _open_request_map(  # noqa: ANN001
    db,
    account_ids: set[int],
    external_ids: dict[str, int] | None = None,
) -> dict[int, OpenRequest]:
    """开户单 → 广告账户的对应关系存在开户单回执 JSON 里。

    回执有两种写法：`ad_account_ids`（收据 / 新审批）与 `accounts`（一步开户的老数据），
    后者是媒体侧 account_id 字符串，需要再用账户表换成主键。
    """
    if not account_ids:
        return {}
    by_external = external_ids or {}
    rows = db.execute(
        select(OpenRequest).where(OpenRequest.agent_receipt.is_not(None))
    ).scalars().all()
    found: dict[int, OpenRequest] = {}
    for row in rows:
        try:
            data = json.loads(row.agent_receipt or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        matched: list[int] = []
        for raw in data.get("ad_account_ids") or []:
            try:
                matched.append(int(raw))
            except (TypeError, ValueError):
                continue
        for raw in data.get("accounts") or []:
            aid = by_external.get(str(raw))
            if aid is not None:
                matched.append(aid)
        for aid in matched:
            if aid in account_ids:
                # 同一账户只可能由一张开户单产生；后写入的覆盖旧值即可
                found[aid] = row
    return found


def _item(account: AdAccount, client: Client, order: OpenRequest | None) -> dict:
    status_code = str(account.status)
    authorized = account.status is not AdAccountStatus.BANNED
    return {
        "id": account.id,
        "account_id": account.account_id,
        "platform": str(account.platform),
        "platform_label": PLATFORM_LABEL.get(account.platform, str(account.platform)),
        "bm_id": account.bm_id,
        "timezone": account.timezone,
        "currency": account.currency,
        "balance": str(money(account.balance)),
        "daily_limit": str(money(account.daily_limit)),
        "status": status_code,
        "status_label": AD_ACCOUNT_STATUS_LABEL.get(account.status, status_code),
        "authorized": authorized,
        "authorized_label": "是" if authorized else "否",
        "channel": account.channel or UNASSIGNED_AGENT,
        "client_id": client.id,
        "client_name": client.company_name,
        "client_biz_no": client.biz_no,
        "subject": (order.subject if order and order.subject else client.registered_place),
        "open_request_biz_no": order.biz_no if order else None,
        "open_request_status": str(order.status) if order else None,
        "open_request_status_label": (
            OPEN_REQUEST_STATUS_LABEL.get(order.status, str(order.status)) if order else None
        ),
        "remark": order.remark if order else None,
        "opened_at": _iso(account.opened_at),
        "created_at": _iso(account.created_at),
        "updated_at": _iso(account.updated_at),
    }


def _filters(
    *,
    keyword: str | None,
    client_keyword: str | None,
    agent: str | None,
    platform: str | None,
    status_f: str | None,
) -> list:
    conds = []
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        conds.append(or_(AdAccount.account_id.like(kw), AdAccount.bm_id.like(kw)))
    if client_keyword and client_keyword.strip():
        kw = f"%{client_keyword.strip()}%"
        conds.append(
            or_(Client.company_name.like(kw), Client.biz_no.like(kw), Client.crm_code.like(kw))
        )
    if agent and agent.strip():
        if agent.strip() == UNASSIGNED_AGENT:
            conds.append(AdAccount.channel.is_(None))
        else:
            conds.append(AdAccount.channel.like(f"%{agent.strip()}%"))
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
    return conds


@router.get("/ad-accounts", response_model=dict, summary="账户管理：广告账户列表")
def list_ad_accounts(
    db: DbSession,
    user: User = require_perm(Perm.AD_ACCOUNT_VIEW),
    keyword: str | None = Query(None, description="广告账户 ID / BC ID，模糊匹配"),
    client_keyword: str | None = Query(None, description="客户名称/编号，模糊匹配"),
    agent: str | None = Query(None, description="一级代理商，模糊匹配"),
    platform: str | None = None,
    status_f: str | None = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    conds = _filters(
        keyword=keyword, client_keyword=client_keyword, agent=agent,
        platform=platform, status_f=status_f,
    )
    owner = scope_sales_owner(user)
    if owner:
        conds.append(Client.sales_owner == owner)

    stmt = select(AdAccount, Client).join(Client, Client.id == AdAccount.client_id).where(*conds)
    total = db.execute(select(sa_func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(AdAccount.id.desc()).limit(limit).offset(offset)
    ).all()
    orders = _open_request_map(
        db, {a.id for a, _c in rows}, {a.account_id: a.id for a, _c in rows}
    )

    # 余额按币种分开合计：铁律禁止跨币种求和
    cur_rows = db.execute(
        select(AdAccount.currency, sa_func.coalesce(sa_func.sum(AdAccount.balance), 0), sa_func.count())
        .join(Client, Client.id == AdAccount.client_id)
        .where(*conds)
        .group_by(AdAccount.currency)
    ).all()
    status_rows = db.execute(
        select(AdAccount.status, sa_func.count())
        .join(Client, Client.id == AdAccount.client_id)
        .where(*conds)
        .group_by(AdAccount.status)
    ).all()

    return {
        "total": int(total),
        "items": [_item(a, c, orders.get(a.id)) for a, c in rows],
        "summary": {
            "by_status": {str(s): int(n) for s, n in status_rows},
            "by_currency": [
                {"currency": ccy or "USD", "balance": str(money(bal or 0)), "count": int(n)}
                for ccy, bal, n in cur_rows
            ],
        },
    }


@router.get("/ad-accounts/export", summary="导出广告账户列表（CSV）")
def export_ad_accounts(
    db: DbSession,
    user: User = require_perm(Perm.AD_ACCOUNT_VIEW),
    keyword: str | None = None,
    client_keyword: str | None = None,
    agent: str | None = None,
    platform: str | None = None,
    status_f: str | None = Query(None, alias="status"),
) -> Response:
    conds = _filters(
        keyword=keyword, client_keyword=client_keyword, agent=agent,
        platform=platform, status_f=status_f,
    )
    owner = scope_sales_owner(user)
    if owner:
        conds.append(Client.sales_owner == owner)
    rows = db.execute(
        select(AdAccount, Client)
        .join(Client, Client.id == AdAccount.client_id)
        .where(*conds)
        .order_by(AdAccount.id.desc())
        .limit(10000)
    ).all()
    orders = _open_request_map(
        db, {a.id for a, _c in rows}, {a.account_id: a.id for a, _c in rows}
    )
    items = [_item(a, c, orders.get(a.id)) for a, c in rows]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ID", "广告账户", "时区", "申请单号", "客户名称", "代理商", "媒体平台",
        "开户主体", "BC ID", "账户余额", "币种", "状态", "授权状态", "开户时间", "备注",
    ])
    for r in items:
        writer.writerow([
            r["id"], r["account_id"], r["timezone"] or "", r["open_request_biz_no"] or "",
            r["client_name"], r["channel"], r["platform_label"], r["subject"] or "",
            r["bm_id"] or "", r["balance"], r["currency"], r["status_label"],
            r["authorized_label"], r["opened_at"] or "", r["remark"] or "",
        ])
    body = ("\ufeff" + buf.getvalue()).encode("utf-8")
    filename = f"ad-accounts-{utcnow():%Y%m%d%H%M%S}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ad-accounts/{account_id}", response_model=dict, summary="账户管理：广告账户详情")
def ad_account_detail(
    account_id: int,
    db: DbSession,
    user: User = require_perm(Perm.AD_ACCOUNT_VIEW),
) -> dict:
    account = db.get(AdAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "广告账户不存在")
    client = db.get(Client, account.client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户档案不存在")
    owner = scope_sales_owner(user)
    if owner and client.sales_owner != owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己名下客户的广告账户")

    order = _open_request_map(
        db, {account.id}, {account.account_id: account.id}
    ).get(account.id)
    wallets = db.execute(
        select(Wallet)
        .where(Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client.id)
        .order_by(Wallet.currency)
    ).scalars().all()

    # 充值记录：没有 recharge.view 的角色（如销售）只看账户本身，不泄露资金动作
    recharges = {"total": 0, "items": [], "summary": {}}
    summary: dict = {}
    if can(user, Perm.RECHARGE_VIEW):
        recharges = recharge_svc.list_recharges(db, ad_account_id=account.id, limit=20)
        agg = db.execute(
            select(
                RechargeOrder.status,
                RechargeOrder.currency,
                sa_func.count(),
                sa_func.sum(RechargeOrder.amount),
            )
            .where(RechargeOrder.ad_account_id == account.id)
            .group_by(RechargeOrder.status, RechargeOrder.currency)
        ).all()
        by_currency: dict[str, dict] = {}
        by_status: dict[str, int] = {}
        for st, ccy, n, amount in agg:
            code = str(st)
            by_status[code] = by_status.get(code, 0) + int(n)
            if st == RechargeStatus.SUCCESS:
                bucket = by_currency.setdefault(
                    ccy or account.currency, {"currency": ccy or account.currency, "amount": money(0), "count": 0}
                )
                bucket["amount"] = money(bucket["amount"] + money(amount or 0))
                bucket["count"] += int(n)
        last_success = db.execute(
            select(sa_func.max(RechargeOrder.updated_at)).where(
                RechargeOrder.ad_account_id == account.id,
                RechargeOrder.status == RechargeStatus.SUCCESS,
            )
        ).scalar_one_or_none()
        summary = {
            "total": int(sum(by_status.values())),
            "by_status": by_status,
            "success_by_currency": [
                {"currency": v["currency"], "amount": str(v["amount"]), "count": v["count"]}
                for v in sorted(by_currency.values(), key=lambda x: x["currency"])
            ],
            "last_success_at": _iso(last_success),
        }

    return {
        "account": _item(account, client, order),
        "client": {
            "id": client.id,
            "biz_no": client.biz_no,
            "company_name": client.company_name,
            "status": str(client.status),
            "status_label": CLIENT_STATUS_LABEL.get(client.status, str(client.status)),
            "contact_name": client.contact_name,
            "phone": client.phone,
            "email": client.email,
            "industry": client.industry,
            "registered_place": client.registered_place,
            "sales_owner": client.sales_owner,
            "ae_owner": client.ae_owner,
            "channel": client.channel,
            "settle_currency": client.settle_currency,
        },
        "wallets": [
            {
                "id": w.id,
                "currency": w.currency,
                "balance": str(money(w.balance)),
                "frozen": str(money(w.frozen)),
                "available": str(w.available),
            }
            for w in wallets
        ],
        "open_request": (
            {
                "id": order.id,
                "biz_no": order.biz_no,
                "status": str(order.status),
                "status_label": OPEN_REQUEST_STATUS_LABEL.get(order.status, str(order.status)),
                "subject": order.subject,
                "timezone": order.timezone,
                "bm_id": order.bm_id,
                "applicant": order.applicant,
                "apply_count": order.apply_count,
                "completed_count": order.completed_count,
                "remark": order.remark,
                "approved_at": _iso(order.approved_at),
                "processed_at": _iso(order.processed_at),
            }
            if order
            else None
        ),
        "recharges": recharges,
        "recharge_summary": summary,
    }
