"""客户门户接口（/api/portal）。

铁律 3：客户身份只从登录态反查，绝不接受前端传入 client_id。
铁律 8：返点相关字段与流水一律不下发。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..models.base import money
from ..models.client import AdAccount, Client, ClientTopUp, SpendDaily
from ..models.docs import OpenRequest
from ..models.enums import (
    CLIENT_STATUS_LABEL,
    ENABLED_PLATFORM_LABEL,
    FUND_ACCOUNT_TYPE_LABEL,
    FUND_OP_TYPE_LABEL,
    OPEN_COUNTRY_OPTIONS,
    OPEN_INDUSTRY_OPTIONS,
    OPEN_REGISTERED_PLACE_OPTIONS,
    OPEN_REQUEST_STATUS_LABEL,
    PAYMENT_CHANNEL_LABEL,
    PAYMENT_CHANNEL_METHOD,
    PAYMENT_METHOD_LABEL,
    BizType,
    ClientStatus,
    FundAccountType,
    FundOpType,
    OwnerType,
    PaymentChannel,
    PaymentMethod,
    TOPUP_STATUS_LABEL,
    TopUpStatus,
)
from ..models.file import FileObject
from ..models.wallet import Wallet
from ..services import bcid as svc_bcid
from ..services import clients as svc_clients
from ..services import fx as svc_fx
from ..services import ledger_views as lv
from ..services import openreq as svc_open
from ..services import storage
from ..services.errors import LedgerError, RateChanged, RateUnavailable
from .deps import DbSession, PortalUser

router = APIRouter(prefix="/api/portal", tags=["客户门户"])


# ------------------------------------------------------------------ 我的概况

@router.get("/profile", response_model=dict, summary="我的档案与审核进度")
def my_profile(db: DbSession, user: PortalUser) -> dict:
    c = db.get(Client, user.client_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户档案不存在")

    history = svc_clients.review_history(db, c.id)
    return {
        "biz_no": c.biz_no,
        "company_name": c.company_name,
        "contact_name": c.contact_name,
        "phone": c.phone,
        "email": c.email,
        "settle_currency": c.settle_currency,
        "status": str(c.status),
        "status_label": CLIENT_STATUS_LABEL.get(c.status, str(c.status)),
        "current_step": svc_clients.CURRENT_STEP.get(c.status),
        "reject_reason": c.reject_reason,
        "version": c.version,
        "progress": [
            {"key": "SUBMIT", "label": "提交申请", "done": True},
            {"key": "REVIEW", "label": "销售初审", "done": _step_done(c.status, "REVIEW")},
            {"key": "RISK", "label": "风控审核", "done": _step_done(c.status, "RISK")},
            {"key": "LIMIT", "label": "财务定额", "done": _step_done(c.status, "LIMIT")},
        ],
        "timeline": [
            {
                "step": r.step, "action": r.action, "operator": r.operator,
                "reason": r.reason,
                "at": r.created_at.isoformat(sep=" ", timespec="seconds"),
            }
            for r in history
        ],
    }


_STEP_ORDER = ["PENDING_REVIEW", "PENDING_RISK", "PENDING_LIMIT", "ACTIVE"]
_STEP_INDEX = {"REVIEW": 0, "RISK": 1, "LIMIT": 2}


def _step_done(client_status, step: str) -> bool:
    if client_status == "ACTIVE":
        return True
    order = [str(s) for s in _STEP_ORDER]
    if str(client_status) not in order:
        return False
    return order.index(str(client_status)) > _STEP_INDEX[step]


# ------------------------------------------------------------------ 入驻申请

class ApplicationIn(BaseModel):
    company_name: str | None = None
    credit_code: str | None = None
    registered_place: str | None = None
    founded_at: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    phone: str | None = None
    email: str | None = None
    wechat: str | None = None
    platforms: str | None = None
    industry: str | None = None
    target_regions: str | None = None
    monthly_spend_expect: Decimal | None = None
    settle_currency: str | None = None
    license_url: str | None = None
    idcard_url: str | None = None
    website: str | None = None
    qualification_url: str | None = None
    settle_intent: str | None = Field(None, pattern="^(PREPAID|CREDIT)$")
    submit: bool = False


@router.post("/onboarding", response_model=dict, summary="入驻申请（保存草稿 / 提交）")
def save_onboarding(body: ApplicationIn, db: DbSession, user: PortalUser) -> dict:
    payload = body.model_dump(exclude={"submit"}, exclude_none=True)
    try:
        app_obj = svc_clients.upsert_application(
            db, client_id=user.client_id, payload=payload, submit=body.submit
        )
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {"version": app_obj.version, "is_draft": app_obj.is_draft,
            "submitted_at": app_obj.submitted_at.isoformat(sep=" ", timespec="seconds")
            if app_obj.submitted_at else None}


@router.post("/onboarding/resubmit", response_model=dict, summary="驳回后修改重提")
def resubmit(body: ApplicationIn, db: DbSession, user: PortalUser) -> dict:
    payload = body.model_dump(exclude={"submit"}, exclude_none=True)
    try:
        c = svc_clients.resubmit(db, client_id=user.client_id, operator=user, changes=payload)
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {"version": c.version, "status": str(c.status)}


# ------------------------------------------------------------------ 开户申请

class OpenApplyIn(BaseModel):
    platform: str = Field(min_length=2, max_length=16)
    subject: str = Field(min_length=1, max_length=200)
    apply_count: int = Field(1, ge=1, le=20)
    bm_id: str | None = Field(None, max_length=64)
    timezone: str | None = Field(None, max_length=60)
    currency: str | None = Field(None, max_length=8)
    daily_limit: Decimal = Field(Decimal("0"), ge=0)
    remark: str | None = Field(None, max_length=500)
    # 开户申请页（CRM 原型）：企业信息 + 账户信息。老调用方不传也能用。
    license_url: str | None = Field(None, max_length=300)
    credit_code: str | None = Field(None, max_length=64)
    registered_place: str | None = Field(None, max_length=120)
    industry: str | None = Field(None, max_length=60)
    target_country: str | None = Field(None, max_length=60)
    bc_ids: list[str] = Field(default_factory=list, max_length=10)
    product_link: str | None = Field(None, max_length=500)


@router.get("/open-requests", response_model=dict, summary="我的开户申请")
def my_open_requests(
    db: DbSession,
    user: PortalUser,
    date_from: date | None = None,
    date_to: date | None = None,
    subject: str | None = None,
    biz_no: str | None = None,
    applicant: str | None = None,
    platform: str | None = None,
    status_f: str | None = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    data = svc_open.list_client_requests(
        db,
        client_id=user.client_id,
        date_from=date_from,
        date_to=date_to,
        subject=subject,
        biz_no=biz_no,
        applicant=applicant,
        platform=platform,
        status_f=status_f,
        limit=limit,
        offset=offset,
    )
    c = db.get(Client, user.client_id)
    data["company_name"] = c.company_name if c else ""
    data["default_subject"] = c.company_name if c else ""
    data["settle_currency"] = c.settle_currency if c else "USD"
    data["can_apply"] = bool(c and c.status is ClientStatus.ACTIVE)
    if c and c.status is ClientStatus.DISABLED:
        data["apply_blocked_reason"] = "客户已停用，不能申请开户"
    elif c and c.status is not ClientStatus.ACTIVE:
        data["apply_blocked_reason"] = "完成入驻审核后才能申请开户"
    else:
        data["apply_blocked_reason"] = None

    # 历史营业执照：只列本人上传的 LICENSE 文件，客户之间不串。
    files = db.execute(
        select(FileObject)
        .where(FileObject.client_id == user.client_id, FileObject.biz_type == "LICENSE")
        .order_by(FileObject.id.desc())
    ).scalars().all()
    licenses: list[dict] = []
    seen: set[str] = set()
    for f in files:
        url = storage.url_of(f)
        if url in seen:
            continue
        seen.add(url)
        licenses.append(
            {
                "url": url,
                "original_name": f.original_name,
                "created_at": f.created_at.isoformat(sep=" ", timespec="seconds"),
            }
        )
    data["licenses"] = licenses

    # BCID 选项来自「BC 管理」里手工添加的绑定 + 广告账户/开户单归纳出的历史绑定。
    # 默认预选该项；没有设默认时退回第一个，保持老行为。
    bc_options = svc_bcid.options(db, user.client_id)
    default_bc = svc_bcid.default_bcid(db, user.client_id)
    data["bc_options"] = bc_options
    data["default_bc_ids"] = [default_bc] if default_bc else bc_options[:1]

    data["options"] = {
        # 当前只做 TikTok：开户页媒体选项只下发 TikTok，历史媒体不再可选。
        "platform": [
            {"code": str(k), "label": v} for k, v in ENABLED_PLATFORM_LABEL.items()
        ],
        "country": [
            {"code": k, "label": v}
            for k, v in sorted(OPEN_COUNTRY_OPTIONS.items(), key=lambda kv: kv[1])
        ],
        "industry": [{"code": k, "label": v} for k, v in OPEN_INDUSTRY_OPTIONS.items()],
        "registered_place": [
            {"code": k, "label": v} for k, v in OPEN_REGISTERED_PLACE_OPTIONS.items()
        ],
        "status": [
            {"code": str(k), "label": v} for k, v in OPEN_REQUEST_STATUS_LABEL.items()
        ],
    }
    return data


class ParseLicenseIn(BaseModel):
    file_key: str = Field(min_length=1, max_length=300)


@router.post(
    "/open-requests/parse-license",
    response_model=dict,
    summary="营业执照/BR 自动识别（本地 OCR 带出名称、证号、注册地；失败不伪造）",
)
def parse_license(body: ParseLicenseIn, db: DbSession, user: PortalUser) -> dict:
    key = body.file_key.strip()
    if key.startswith(storage.FILE_URL_PREFIX):
        key = key[len(storage.FILE_URL_PREFIX):]
    obj = storage.find_by_key(db, key)
    if obj is None or obj.client_id != user.client_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权解析该文件")
    return svc_open.license_hints(
        db, client=db.get(Client, user.client_id), file_obj=obj
    )


@router.post("/open-requests", response_model=dict, summary="提交开户申请（待管理端审批）")
def apply_open_request(body: OpenApplyIn, db: DbSession, user: PortalUser) -> dict:
    try:
        order = svc_open.create_client_application(
            db,
            client_id=user.client_id,
            platform=body.platform,
            subject=body.subject,
            apply_count=body.apply_count,
            bm_id=body.bm_id,
            timezone=body.timezone,
            currency=body.currency,
            daily_limit=body.daily_limit,
            remark=body.remark,
            operator=user.display_name,
            license_url=body.license_url,
            credit_code=body.credit_code,
            registered_place=body.registered_place,
            industry=body.industry,
            target_country=body.target_country,
            bc_ids=body.bc_ids,
            product_link=body.product_link,
        )
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    item = svc_open.serialize_portal(order)
    item["message"] = "开户申请已提交，等待管理端审批"
    return item


# ------------------------------------------------------------------ 我的广告账户

@router.get("/accounts", response_model=dict, summary="我的广告账户")
def my_accounts(db: DbSession, user: PortalUser) -> dict:
    rows = db.query(AdAccount).filter(AdAccount.client_id == user.client_id).all()
    return {
        "items": [
            {
                "id": a.id, "account_id": a.account_id, "platform": str(a.platform),
                "currency": a.currency,
                "balance": str(money(a.balance)) if a.balance is not None else None,
                "daily_limit": str(money(a.daily_limit)),
                "status": str(a.status),
                # 渠道 / 成本系数 / 返点率 一律不下发
            }
            for a in rows
        ]
    }


@router.get("/accounts/{account_id}/spend", response_model=dict, summary="消耗曲线（近 N 天）")
def account_spend(
    account_id: int, db: DbSession, user: PortalUser, days: int = Query(30, le=90)
) -> dict:
    acc = db.get(AdAccount, account_id)
    if acc is None or acc.client_id != user.client_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该账户")

    rows = (
        db.query(SpendDaily.date, func.sum(SpendDaily.spend))
        .filter(SpendDaily.ad_account_id == account_id)
        .group_by(SpendDaily.date)
        .order_by(SpendDaily.date.desc())
        .limit(days)
        .all()
    )
    points = [{"date": d.isoformat(), "spend": str(money(s))} for d, s in reversed(rows)]
    total = sum((Decimal(p["spend"]) for p in points), Decimal("0"))
    return {"account_id": acc.account_id, "currency": acc.currency,
            "points": points, "total": str(money(total))}


# ------------------------------------------------------------------ 我的钱包

@router.get("/wallet", response_model=dict, summary="我的钱包（返点不可见）")
def my_wallet(db: DbSession, user: PortalUser) -> dict:
    """资金总账户 = 现金可用 + 授信可用。

    广告账户余额是媒体侧预算，不是客户可提现的钱，所以不计入资金总账户，
    只作为现金账户卡的辅助信息下发（与内部客户管理口径一致）。
    """
    c = db.get(Client, user.client_id)
    wallets = db.query(Wallet).filter(
        Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == user.client_id
    ).all()
    accounts = db.query(AdAccount).filter(AdAccount.client_id == user.client_id).all()
    settle = (c.settle_currency if c and c.settle_currency else "USD") or "USD"

    per_currency: dict[str, dict] = {}

    def _bucket(ccy: str) -> dict:
        return per_currency.setdefault(
            ccy,
            {"currency": ccy, "wallet": Decimal("0"), "frozen": Decimal("0"),
             "ad_accounts": Decimal("0"),
             "credit_limit": Decimal("0"), "credit_used": Decimal("0")},
        )

    # 主币种即使没有钱包也要有卡片，避免客户第一次充值前看到空白
    _bucket(settle)
    for w in wallets:
        b = _bucket(w.currency)
        b["wallet"] += money(w.balance)
        # 冻结额是加款单执行中的占位，对客户不可用，必须从"可用"里扣掉
        b["frozen"] += money(w.frozen)
    for a in accounts:
        _bucket(a.currency)["ad_accounts"] += money(a.balance)

    credit_limit = money(c.credit_limit) if c else Decimal("0")
    credit_used = money(c.credit_used) if c else Decimal("0")
    credit_available = max(money(credit_limit - credit_used), Decimal("0"))
    primary = _bucket(settle)
    primary["credit_limit"] = credit_limit
    primary["credit_used"] = credit_used

    items = []
    for v in per_currency.values():
        cash_available = money(v["wallet"] - v["frozen"])
        ccy_credit_limit = money(v["credit_limit"])
        ccy_credit_used = money(v["credit_used"])
        ccy_credit_available = max(money(ccy_credit_limit - ccy_credit_used), Decimal("0"))
        items.append(
            {
                "currency": v["currency"],
                "wallet_balance": str(money(v["wallet"])),
                "frozen": str(money(v["frozen"])),
                "cash_available": str(cash_available),
                "ad_accounts_balance": str(money(v["ad_accounts"])),
                "credit_limit": str(ccy_credit_limit),
                "credit_used": str(ccy_credit_used),
                "credit_available": str(ccy_credit_available),
                # 资金总账户只算现金 + 授信，广告账户余额不计入
                "total_available": str(money(cash_available + ccy_credit_available)),
            }
        )
    # 结算币种永远排第一，页面卡片读 items[0]
    items.sort(key=lambda it: 0 if it["currency"] == settle else 1)

    return {
        "company_name": c.company_name if c else "",
        "settle_currency": settle,
        "items": items,
        "credit": {
            "currency": settle,
            "limit": str(credit_limit),
            "used": str(credit_used),
            "available": str(credit_available),
        },
        "can_topup": bool(c and c.status is ClientStatus.ACTIVE),
        "can_repay": credit_used > 0,
    }


# 客户侧交易类型：只列客户能看到的动作，中文也由后端给（契约第 2 节）。
PORTAL_BIZ_LABEL = {
    BizType.TOPUP.value: "现金充值",
    BizType.RECHARGE.value: "广告账户充值",
    BizType.REFUND.value: "退款",
    BizType.ADJUST.value: "调账",
}
PORTAL_BIZ_OPTIONS = [
    {"code": code, "label": label} for code, label in PORTAL_BIZ_LABEL.items()
]
PORTAL_TX_STATUS_OPTIONS = [
    {"code": "DONE", "label": "已完成"},
    {"code": "REVERSED", "label": "已冲正"},
]


@router.get("/wallet/transactions", response_model=dict, summary="我的流水（返点已过滤）")
def my_transactions(
    db: DbSession,
    user: PortalUser,
    date_from: date | None = None,
    date_to: date | None = None,
    direction: str | None = Query(None, pattern="^(IN|OUT)$"),
    biz_no: str | None = Query(None, max_length=64),
    biz_type: str | None = Query(None, max_length=16),
    status_f: str | None = Query(None, alias="status", pattern="^(DONE|REVERSED)$"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    data = lv.client_ledger(
        db, client_id=user.client_id, date_from=date_from, date_to=date_to,
        direction=direction, biz_no=biz_no, biz_type=biz_type, status=status_f,
        hide_rebate=True, limit=limit, offset=offset,
    )
    # 白名单再削一遍，双保险
    safe_items = [
        {
            "id": it["id"],
            "biz_no": it["biz_no"] or f"TX{it['id']:08d}",
            "biz_type": it["biz_type"],
            "biz_type_label": PORTAL_BIZ_LABEL.get(
                it["biz_type"], it["biz_type_label"]
            ),
            "occurred_at": it["occurred_at"],
            "direction": it["direction"],
            "amount": it["amount"],
            "currency": it["currency"],
            "balance_before": it["balance_before"],
            "balance_after": it["balance_after"],
            "remark": it["remark"],
            "status": "REVERSED" if it["reversed"] else "DONE",
            "status_label": "已冲正" if it["reversed"] else "已完成",
            "feedback": it.get("reverse_remark") or None,
            "operator": it["operator"],
        }
        for it in data["items"]
    ]
    return {
        "total": data["total"],
        "items": safe_items,
        "summary": data["summary"],
        "biz_options": PORTAL_BIZ_OPTIONS,
        "status_options": PORTAL_TX_STATUS_OPTIONS,
    }


@router.get("/wallet/transactions/export", summary="流水导出 CSV（不含返点）")
def export_my_transactions(
    db: DbSession,
    user: PortalUser,
    date_from: date | None = None,
    date_to: date | None = None,
    biz_no: str | None = None,
    biz_type: str | None = None,
    status_f: str | None = Query(None, alias="status", pattern="^(DONE|REVERSED)$"),
) -> Response:
    data = lv.client_ledger(
        db, client_id=user.client_id, date_from=date_from, date_to=date_to,
        biz_no=biz_no, biz_type=biz_type, status=status_f,
        hide_rebate=True, limit=10000, offset=0,
    )
    # 汇总按币种算期初/期末，带了明细筛选时不再混进 CSV，免得两套口径打架
    summary = None if (biz_no or biz_type or status_f) else data["summary"]["per_currency"]
    content = lv.export_csv(data["items"], summary)
    # 客户导出文件里不能出现"返点"二字（铁律 8 自检）
    text = content.decode("utf-8-sig")
    for word in ("返点", "rebate", "佣金"):
        if word in text:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "导出内容自检失败")
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="wallet-transactions.csv"'},
    )


# ------------------------------------------------------------------ 充值记录

# 门户收款渠道。线上通道还没接支付回调，先不上架，只留线下汇款。
PORTAL_PAYMENT_CHANNELS = [
    {"code": "OFFLINE", "label": "线下汇款", "enabled": True, "note": "转账后上传回执，财务确认到账"},
]
PORTAL_ENABLED_CHANNELS = {c["code"] for c in PORTAL_PAYMENT_CHANNELS if c["enabled"]}


@router.get("/topup-quote", response_model=dict, summary="充值报价：美元到账需付多少人民币")
def topup_quote(
    db: DbSession,
    user: PortalUser,
    amount: Decimal = Query(..., gt=0, description="到账美元金额"),
) -> dict:
    """给门户充值弹层用的实时报价。

    只读报价，不落单、不动账；报价会写进服务端缓存，下单时再次取价并校验漂移。
    """
    try:
        quote = svc_fx.get_quote(db)
        payload = svc_fx.quote_payload(quote, amount)
    except RateUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()  # 只提交汇率缓存
    return payload


@router.get("/topups", response_model=dict, summary="我的充值记录")
def my_topups(db: DbSession, user: PortalUser, limit: int = Query(50, le=200)) -> dict:
    rows = (
        db.query(ClientTopUp)
        .filter(ClientTopUp.client_id == user.client_id)
        .order_by(ClientTopUp.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "channels": PORTAL_PAYMENT_CHANNELS,
        "items": [
            {
                "biz_no": t.biz_no,
                "amount": str(money(t.amount)),
                "currency": t.currency,
                "payable_amount": str(money(t.payable_amount)),
                "payable_currency": t.payable_currency or "USD",
                "received_amount": str(
                    money(t.received_amount) if money(t.received_amount) > 0 else money(t.amount)
                ),
                "exchange_rate": str(money(t.exchange_rate) or Decimal("1")),
                "rate_source": t.rate_source,
                "fund_account_type": str(t.fund_account_type or FundAccountType.CASH),
                "fund_account_type_label": FUND_ACCOUNT_TYPE_LABEL.get(
                    t.fund_account_type or FundAccountType.CASH,
                    str(t.fund_account_type or FundAccountType.CASH),
                ),
                "op_type": str(t.op_type or FundOpType.INCREASE),
                "op_type_label": FUND_OP_TYPE_LABEL.get(
                    t.op_type or FundOpType.INCREASE,
                    str(t.op_type or FundOpType.INCREASE),
                ),
                "payment_method": str(t.payment_method),
                # 契约 §2：中文只由后端给，前端不写映射表
                "payment_method_label": PAYMENT_METHOD_LABEL.get(
                    t.payment_method, str(t.payment_method)
                ),
                "payment_channel": t.payment_channel,
                "payment_channel_label": (
                    PAYMENT_CHANNEL_LABEL.get(t.payment_channel)
                    if t.payment_channel
                    else PAYMENT_METHOD_LABEL.get(t.payment_method, str(t.payment_method))
                ),
                "status": str(t.status),
                "status_label": TOPUP_STATUS_LABEL.get(t.status, str(t.status)),
                "reject_reason": t.reject_reason,
                "voucher": t.voucher,
                "voucher_url": t.voucher if storage.is_file_url(t.voucher) else None,
                "has_voucher": bool(t.voucher),
                # PENDING_PAY 的死状态出口：前端据此显示「补传凭证」
                "can_attach_voucher": t.status is TopUpStatus.PENDING_PAY,
                "created_at": t.created_at.isoformat(sep=" ", timespec="seconds"),
                "confirmed_at": t.confirmed_at.isoformat(sep=" ", timespec="seconds")
                if t.confirmed_at else None,
            }
            for t in rows
        ]
    }


def _check_voucher_owned(db: DbSession, user: PortalUser, voucher: str | None) -> None:
    """凭证只接受上传接口返回的地址，且必须是自己传的 —— 挡住任意路径注入。"""
    if not voucher:
        return
    from ..models.file import FileObject

    if not storage.is_file_url(voucher):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "凭证必须是上传接口返回的文件地址（/api/files/...）",
        )
    obj = storage.find_by_key(db, storage.key_from_url(voucher))
    if obj is None or obj.client_id != user.client_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "凭证文件不存在或不属于当前客户")


class TopUpCreateIn(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="USD", max_length=8)
    payment_channel: str = Field(default=str(PaymentChannel.OFFLINE), max_length=20)
    payer: str | None = None
    voucher: str | None = None
    remark: str | None = Field(None, max_length=200)
    # 客户端看到的报价汇率；服务端取价后比对，漂移过大要求重新确认
    expected_rate: Decimal | None = Field(None, gt=0)


@router.post("/topups", response_model=dict, summary="提交充值（待财务确认）")
def create_my_topup(body: TopUpCreateIn, db: DbSession, user: PortalUser) -> dict:
    from ..models.enums import PaymentChannel as _PC
    from ..services.topup import create_topup

    try:
        channel = _PC(body.payment_channel)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "支付方式取值非法") from exc
    if channel.value not in PORTAL_ENABLED_CHANNELS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{PAYMENT_CHANNEL_LABEL.get(channel, channel)} 暂未开通，请选择线下汇款",
        )

    _check_voucher_owned(db, user, body.voucher)

    ccy = (body.currency or "USD").upper()
    if ccy != "USD":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "门户充值只支持 USD 到账，人民币请按报价折算后线下转账"
        )

    try:
        usd = svc_fx.validate_amount_usd(body.amount)
        quote = svc_fx.resolve_order_quote(db, usd, body.expected_rate)
        t = create_topup(
            db, client_id=user.client_id, amount=usd, currency=ccy,
            payment_method=PAYMENT_CHANNEL_METHOD[channel], payment_channel=channel.value,
            payer=body.payer or user.display_name,
            voucher=body.voucher, remark=body.remark, operator=user.display_name,
            exchange_rate=quote.rate,
            payable_amount=svc_fx.payable_cny(usd, quote.rate),
            payable_currency="CNY",
            rate_source=svc_fx.source_label(quote),
        )
    except RateChanged:
        # 交给全局 LedgerError 处理器：响应体要带 code=RATE_CHANGED 给前端识别
        raise
    except RateUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {
        "biz_no": t.biz_no,
        "status": str(t.status),
        "amount": str(money(t.amount)),
        "payable_amount": str(money(t.payable_amount)),
        "payable_currency": t.payable_currency,
        "exchange_rate": str(money(t.exchange_rate)),
        "rate_source": t.rate_source,
        "status_label": TOPUP_STATUS_LABEL.get(t.status, str(t.status)),
        "payment_channel": t.payment_channel,
        "payment_channel_label": PAYMENT_CHANNEL_LABEL.get(channel, channel.value),
        "message": "已受理，待财务确认到账后入账",
    }


class VoucherIn(BaseModel):
    voucher: str = Field(min_length=1, max_length=300)


@router.post("/topups/{biz_no}/voucher", response_model=dict, summary="补传凭证（待支付 → 待确认）")
def attach_voucher(biz_no: str, body: VoucherIn, db: DbSession, user: PortalUser) -> dict:
    """PENDING_PAY 的死状态出口：客户补上传凭证后进入待财务确认。"""
    t = (
        db.query(ClientTopUp)
        .filter(ClientTopUp.biz_no == biz_no, ClientTopUp.client_id == user.client_id)
        .one_or_none()
    )
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "充值单不存在")

    if not storage.is_file_url(body.voucher):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "凭证必须是上传接口返回的文件地址（/api/files/...）"
        )
    obj = storage.find_by_key(db, storage.key_from_url(body.voucher))
    if obj is None or obj.client_id != user.client_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "凭证文件不存在或不属于当前客户")

    if t.status is not TopUpStatus.PENDING_PAY:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"当前状态 {TOPUP_STATUS_LABEL.get(t.status, t.status)} 不允许补传凭证",
        )

    t.voucher = body.voucher
    t.status = TopUpStatus.PENDING_CONFIRM
    db.commit()
    return {
        "biz_no": t.biz_no,
        "status": str(t.status),
        "status_label": TOPUP_STATUS_LABEL.get(t.status, str(t.status)),
    }


# ------------------------------------------------------------------ 授信还款

class CreditRepayIn(BaseModel):
    amount: Decimal = Field(gt=0)
    voucher: str | None = None
    remark: str | None = Field(None, max_length=200)


@router.post("/credit-repayments", response_model=dict, summary="授信还款申请（财务审核后恢复额度）")
def create_credit_repayment(body: CreditRepayIn, db: DbSession, user: PortalUser) -> dict:
    """授信还款只落申请单，不动钱；财务审核通过后 `client.credit_used` 才会减少。

    复用线下汇款申请单（授信账户 + 加款 = 还款），不另起一张表。
    """
    from ..services import fund_app

    c = db.get(Client, user.client_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "客户档案不存在")
    used = money(c.credit_used)
    if used <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前没有已用授信，无需还款")
    amt = money(body.amount)
    if amt > used:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"还款金额 {amt} 超过已用授信 {used}，请核对后再提交",
        )
    _check_voucher_owned(db, user, body.voucher)

    try:
        app = fund_app.create_application(
            db,
            client_id=user.client_id,
            fund_account_type=FundAccountType.CREDIT,
            op_type=FundOpType.INCREASE,
            amount=amt,
            currency=c.settle_currency or "USD",
            payer=user.display_name,
            voucher=body.voucher,
            remark=body.remark,
            operator=user.display_name,
        )
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {
        "biz_no": app.biz_no,
        "status": str(app.status),
        "status_label": TOPUP_STATUS_LABEL.get(app.status, str(app.status)),
        "amount": str(money(app.amount)),
        "currency": app.currency,
        "message": "还款申请已提交，财务审核通过后恢复授信额度",
    }


# ------------------------------------------------------------------ 工单

class TicketIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: str | None = None
    content: str | None = None


@router.post("/tickets", response_model=dict, summary="提交工单")
def create_ticket(body: TicketIn, db: DbSession, user: PortalUser) -> dict:
    from ..models.user import PortalTicket

    t = PortalTicket(
        client_id=user.client_id, title=body.title,
        category=body.category, content=body.content,
    )
    db.add(t)
    db.commit()
    return {"id": t.id, "status": t.status, "message": "已提交"}


@router.get("/tickets", response_model=dict, summary="我的工单")
def my_tickets(
    db: DbSession, user: PortalUser, limit: int = Query(50, le=200)
) -> dict:
    """只有提交没有查询，客户无法确认问题是否被受理（F-07 #6）。"""
    from ..models.user import PortalTicket

    rows = (
        db.query(PortalTicket)
        .filter(PortalTicket.client_id == user.client_id)
        .order_by(PortalTicket.id.desc())
        .limit(limit)
        .all()
    )
    return {"items": [_ticket_row(t) for t in rows]}


def _ticket_row(t) -> dict:  # noqa: ANN001
    """客户可见字段白名单：不带 priority / handled_by 等内部信息。"""
    from ..models.enums import TICKET_STATUS_LABEL

    return {
        "id": t.id,
        "title": t.title,
        "category": t.category,
        "content": t.content,
        "status": t.status,
        "status_label": TICKET_STATUS_LABEL.get(t.status, t.status),
        # 处理回复对客户可见；多次回复按时间追加在同一字段里
        "reply": t.reply,
        "handled_at": t.handled_at.isoformat(sep=" ", timespec="seconds")
        if t.handled_at else None,
        "updated_at": t.updated_at.isoformat(sep=" ", timespec="seconds")
        if t.updated_at else None,
        "created_at": t.created_at.isoformat(sep=" ", timespec="seconds")
        if t.created_at else None,
    }


@router.get("/tickets/{ticket_id}", response_model=dict, summary="我的工单详情")
def my_ticket_detail(ticket_id: int, db: DbSession, user: PortalUser) -> dict:
    """单条工单详情：只能看自己客户名下的单，别人的单一律按不存在处理。"""
    from ..models.user import PortalTicket

    t = db.get(PortalTicket, ticket_id)
    if t is None or t.client_id != user.client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    return _ticket_row(t)
