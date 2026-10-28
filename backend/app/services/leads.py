"""客户申请（销售线索）。

门户登录页的「开户申请」只收基础联系方式，落到 `client_lead`：

- 不在这一步创建 `Client` / `User`，未验证主体不进入客户列表、不产生登录账号；
- 销售在管理端认领 → 联系 → 标记已联系 / 已转客户 / 已关闭；
- 同一条线索按手机 / 微信 / 邮箱去重，避免访客反复点提交刷屏。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.client import ClientLead
from ..models.docs import AuditLog
from ..models.enums import (
    CLIENT_LEAD_NEXT_STATUSES,
    CLIENT_LEAD_STATUS_LABEL,
    ENABLED_PLATFORMS,
    PLATFORM_LABEL,
    ClientLeadStatus,
    Platform,
)
from .errors import StateError

SOURCE_PORTAL = "PORTAL_SELF"
SOURCE_LABEL = {
    SOURCE_PORTAL: "门户自助申请",
}

_DEDUPE_WINDOW = timedelta(days=7)


def _clean(value: str | None, limit: int) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > limit:
        raise StateError(f"内容过长（最多 {limit} 个字符）")
    return text


def _platform(value: str | None) -> Platform:
    code = (value or Platform.TIKTOK).strip().upper()
    allowed = {str(p) for p in ENABLED_PLATFORMS}
    if code not in allowed:
        raise StateError("当前仅支持 TikTok 开户")
    return Platform(code)


def _status(value: str | None) -> ClientLeadStatus:
    try:
        return ClientLeadStatus(str(value).strip().upper())
    except ValueError as exc:
        raise StateError(f"未知的申请状态：{value}") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _day_start(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time())


def create_application(
    session: Session,
    *,
    company_name: str,
    contact_name: str,
    phone: str | None = None,
    wechat: str | None = None,
    email: str | None = None,
    platform: str | None = None,
    monthly_budget_expect: Decimal | str | int | None = 0,
    remark: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[ClientLead, bool]:
    """创建一条公开申请。返回 (线索, 是否新建)；重复提交返回原线索。"""
    company = _clean(company_name, 200)
    contact = _clean(contact_name, 60)
    phone = _clean(phone, 30)
    wechat = _clean(wechat, 60)
    email = _clean(email, 120)
    remark = _clean(remark, 1000)

    if not company:
        raise StateError("请填写公司名称")
    if not contact:
        raise StateError("请填写联系人")
    if not (phone or wechat or email):
        raise StateError("请至少填写手机号、微信号或邮箱中的一项，方便销售联系你")
    if email and "@" not in email:
        raise StateError("邮箱格式不正确")

    budget = money(monthly_budget_expect or 0)
    if budget < 0:
        raise StateError("预计月消耗不能为负数")

    platform_value = _platform(platform)

    # 同一联系方式 7 天内只留一条，重复提交直接返回原线索（对外仍是“已收到”）。
    identity = [cond for cond in (
        ClientLead.phone == phone if phone else None,
        ClientLead.wechat == wechat if wechat else None,
        ClientLead.email == email if email else None,
    ) if cond is not None]
    if identity:
        existing = session.execute(
            select(ClientLead)
            .where(
                or_(*identity),
                ClientLead.status != str(ClientLeadStatus.CLOSED),
                ClientLead.created_at >= utcnow() - _DEDUPE_WINDOW,
            )
            .order_by(ClientLead.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    lead = ClientLead(
        company_name=company,
        contact_name=contact,
        phone=phone,
        wechat=wechat,
        email=email,
        platform=str(platform_value),
        monthly_budget_expect=budget,
        remark=remark,
        source=SOURCE_PORTAL,
        status=str(ClientLeadStatus.NEW),
        source_ip=(source_ip or "")[:64] or None,
        user_agent=(user_agent or "")[:300] or None,
    )
    session.add(lead)
    session.flush()
    lead.biz_no = f"LD{utcnow():%Y%m%d}{lead.id:05d}"
    session.flush()
    return lead, True


def serialize(lead: ClientLead) -> dict:
    status = str(lead.status)
    return {
        "id": lead.id,
        "biz_no": lead.biz_no,
        "company_name": lead.company_name,
        "contact_name": lead.contact_name,
        "phone": lead.phone,
        "wechat": lead.wechat,
        "email": lead.email,
        "platform": str(lead.platform),
        "platform_label": PLATFORM_LABEL.get(lead.platform, str(lead.platform)),
        "monthly_budget_expect": str(money(lead.monthly_budget_expect)),
        "remark": lead.remark,
        "source": lead.source,
        "source_label": SOURCE_LABEL.get(lead.source, lead.source),
        "status": status,
        "status_label": CLIENT_LEAD_STATUS_LABEL.get(status, status),
        "next_statuses": [str(s) for s in CLIENT_LEAD_NEXT_STATUSES.get(status, (status,))],
        "owner": lead.owner,
        "note": lead.note,
        "close_reason": lead.close_reason,
        "contacted_at": _iso(lead.contacted_at),
        "created_at": _iso(lead.created_at),
        "updated_at": _iso(lead.updated_at),
    }


def list_leads(
    session: Session,
    *,
    status_f: str | None = None,
    keyword: str | None = None,
    platform: str | None = None,
    owner: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sales_owner: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    base = []
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        base.append(
            or_(
                ClientLead.company_name.ilike(pattern),
                ClientLead.contact_name.ilike(pattern),
                ClientLead.phone.ilike(pattern),
                ClientLead.wechat.ilike(pattern),
                ClientLead.email.ilike(pattern),
                ClientLead.biz_no.ilike(pattern),
            )
        )
    if platform:
        base.append(ClientLead.platform == str(_platform(platform)))
    if owner:
        base.append(ClientLead.owner == owner.strip())
    if date_from:
        base.append(ClientLead.created_at >= _day_start(date_from))
    if date_to:
        base.append(ClientLead.created_at < _day_start(date_to + timedelta(days=1)))
    # 销售只能看到自己认领的 + 还没人认领的；运营 / 管理员看全部。
    if sales_owner:
        base.append(
            or_(ClientLead.owner == sales_owner, ClientLead.owner.is_(None))
        )

    counts = {str(s): 0 for s in ClientLeadStatus}
    for code, n in session.execute(
        select(ClientLead.status, func.count())
        .select_from(ClientLead)
        .where(*base)
        .group_by(ClientLead.status)
    ).all():
        counts[str(code)] = int(n)

    conds = list(base)
    if status_f:
        conds.append(ClientLead.status == str(_status(status_f)))

    total = session.execute(
        select(func.count()).select_from(ClientLead).where(*conds)
    ).scalar_one()
    rows = session.execute(
        select(ClientLead)
        .where(*conds)
        .order_by(ClientLead.created_at.desc(), ClientLead.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "summary": {
            "counts": counts,
            "total": sum(counts.values()),
            "new": counts[str(ClientLeadStatus.NEW)],
        },
        "items": [serialize(lead) for lead in rows],
    }


def get_lead(session: Session, lead_id: int) -> ClientLead:
    lead = session.get(ClientLead, lead_id)
    if lead is None:
        raise StateError("客户申请不存在")
    return lead


def update_lead(
    session: Session,
    lead: ClientLead,
    *,
    status: str | None = None,
    owner: str | None = None,
    note: str | None = None,
    close_reason: str | None = None,
    actor: str,
) -> ClientLead:
    before = {
        "status": str(lead.status),
        "owner": lead.owner,
        "note": lead.note,
        "close_reason": lead.close_reason,
    }

    if status is not None:
        target = _status(status)
        allowed = {str(s) for s in CLIENT_LEAD_NEXT_STATUSES.get(str(lead.status), ())}
        if str(target) not in allowed:
            current_label = CLIENT_LEAD_STATUS_LABEL.get(lead.status, lead.status)
            target_label = CLIENT_LEAD_STATUS_LABEL.get(str(target), str(target))
            raise StateError(f"申请不能从「{current_label}」直接变为「{target_label}」")
        lead.status = str(target)
        if target is ClientLeadStatus.CONTACTED and lead.contacted_at is None:
            lead.contacted_at = utcnow()
        if target is ClientLeadStatus.CLOSED:
            lead.close_reason = _clean(close_reason, 300) or lead.close_reason
        if target is not ClientLeadStatus.CLOSED:
            lead.close_reason = None

    if owner is not None:
        lead.owner = _clean(owner, 60)
    # 认领动作：进入已联系 / 已转客户时没有跟进人就记到当前操作人
    if str(lead.status) in {str(ClientLeadStatus.CONTACTED), str(ClientLeadStatus.CONVERTED)}:
        if not lead.owner:
            lead.owner = actor

    if note is not None:
        lead.note = _clean(note, 2000)

    session.add(
        AuditLog(
            actor=actor,
            action="client_lead.update",
            doctype="client_lead",
            doc_id=lead.id,
            before=json.dumps(before, ensure_ascii=False),
            after=json.dumps(
                {
                    "status": str(lead.status),
                    "owner": lead.owner,
                    "note": lead.note,
                    "close_reason": lead.close_reason,
                },
                ensure_ascii=False,
            ),
            reason="更新客户申请",
        )
    )
    session.flush()
    return lead


def public_options() -> dict:
    return {
        "platform": [
            {"code": str(p), "label": PLATFORM_LABEL.get(p, str(p))}
            for p in ENABLED_PLATFORMS
        ],
        "budget_presets": [
            {"code": "5000", "label": "5,000 美元以内"},
            {"code": "20000", "label": "5,000 - 20,000 美元"},
            {"code": "50000", "label": "20,000 - 50,000 美元"},
            {"code": "100000", "label": "50,000 美元以上"},
        ],
    }
