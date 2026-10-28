"""开户申请状态机（客户申请 → 管理端审批）。

客户在门户提交开户申请：
    PENDING_APPROVAL --approve(auto_complete)--> OPENED
                     --approve(仅审批)--------> PENDING_SUBMIT --submit--> AGENT_REVIEWING --receipt--> OPENED
                     --reject-----------------> REJECTED

内部代客户提交（source=DESK）仍可走原来的两段式：
    PENDING_APPROVAL --approve--> PENDING_SUBMIT --submit--> AGENT_REVIEWING --receipt--> OPENED / AGENT_REJECTED

铁律 7：未审批通过的单一律不得进 RPA 队列 —— 服务端硬拦截，不靠前端隐藏按钮。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money, utcnow
from ..models.client import AdAccount, Client
from ..models.docs import AuditLog, OpenRequest
from ..models.file import FileObject
from ..models.enums import (
    ENABLED_PLATFORMS,
    OPEN_METHOD_LABEL,
    OPEN_REQUEST_SOURCE_LABEL,
    OPEN_REQUEST_STATUS_LABEL,
    PLATFORM_LABEL,
    AdAccountStatus,
    ClientStatus,
    OpenMethod,
    OpenRequestSource,
    OpenRequestStatus,
    Platform,
)
from . import rpa_mock, storage
from .errors import LedgerError, StateError

# 审批 5 项核对清单（缺一不可）
REQUIRED_CHECKS = ("subject", "industry", "bm", "duplicate", "channel")
CHECK_LABEL = {
    "subject": "主体一致性",
    "industry": "行业合规",
    "bm": "BM 归属",
    "duplicate": "重复开户",
    "channel": "渠道归属",
}

ACTIVE_STATUSES = (
    OpenRequestStatus.PENDING_APPROVAL,
    OpenRequestStatus.PENDING_SUBMIT,
    OpenRequestStatus.AGENT_REVIEWING,
)


def _lock(session: Session, request_id: int) -> OpenRequest:
    order = session.get(OpenRequest, request_id, with_for_update=True)
    if order is None:
        raise StateError(f"开户单不存在（id={request_id}）")
    return order


def _receipt_accounts(order: OpenRequest) -> list[str]:
    """从回执 JSON 里取已下户的账户 ID，坏数据不影响列表。"""
    if not order.agent_receipt:
        return []
    try:
        payload = json.loads(order.agent_receipt)
    except (TypeError, ValueError):
        return []
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        return [str(x) for x in accounts]
    acc = payload.get("account_id")
    return [str(acc)] if acc else []


def split_bc_ids(value: str | None) -> list[str]:
    """BCID 落库存逗号串，读出来统一成列表（去空、去重、保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (value or "").replace("，", ",").split(","):
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _portal_feedback(order: OpenRequest) -> str | None:
    """列表「反馈信息」：只展示回执里确实发生过的事实，解析失败不编造。"""
    if not order.agent_receipt:
        return None
    try:
        payload = json.loads(order.agent_receipt)
    except (TypeError, ValueError):
        return None
    result = str(payload.get("result") or "").upper()
    if result == "OPENED":
        accounts = payload.get("accounts")
        if isinstance(accounts, list) and accounts:
            return "已下户：" + "、".join(str(x) for x in accounts)
        return "已下户"
    if result == "AGENT_REJECTED":
        return str(payload.get("reason") or "一代拒绝")
    return None


def serialize(order: OpenRequest, client: Client | None) -> dict:
    subject = order.subject or (client.company_name if client else None)
    source = str(order.source or OpenRequestSource.DESK)
    method = str(order.open_method or OpenMethod.AUTO)
    return {
        "id": order.id,
        "biz_no": order.biz_no,
        "client_id": order.client_id,
        "client_name": client.company_name if client else None,
        "client_code": client.biz_no if client else None,
        # 代理商：内部字段，客户侧不下发
        "channel": client.channel if client else None,
        "source": source,
        "source_label": OPEN_REQUEST_SOURCE_LABEL.get(source, source),
        "applicant": order.applicant,
        "platform": str(order.platform),
        "platform_label": PLATFORM_LABEL.get(order.platform, str(order.platform)),
        "bm_id": order.bm_id,
        "subject": subject,
        "apply_count": int(order.apply_count or 0),
        "completed_count": int(order.completed_count or 0),
        "open_method": method,
        "open_method_label": OPEN_METHOD_LABEL.get(method, method),
        "timezone": order.timezone,
        "currency": order.currency,
        "daily_limit": str(money(order.daily_limit)),
        "status": str(order.status),
        "status_label": OPEN_REQUEST_STATUS_LABEL.get(order.status, str(order.status)),
        "approver": order.approver,
        "handler": order.approver,
        "approved_at": order.approved_at.isoformat(sep=" ", timespec="seconds")
        if order.approved_at else None,
        "processed_at": (order.processed_at or order.approved_at).isoformat(sep=" ", timespec="seconds")
        if (order.processed_at or order.approved_at) else None,
        "reject_reason": order.reject_reason,
        "remark": order.remark,
        "agent_receipt": order.agent_receipt,
        "account_ids": _receipt_accounts(order),
        # 客户申请材料（后台审批要核对这些，不能只给五项勾选）
        "license_url": order.license_url,
        "credit_code": order.credit_code,
        "registered_place": order.registered_place,
        "industry": order.industry,
        "target_country": order.target_country,
        "bc_ids": split_bc_ids(order.bc_ids),
        "product_link": order.product_link,
        "feedback": _portal_feedback(order),
        "created_at": order.created_at.isoformat(sep=" ", timespec="seconds"),
    }


def serialize_portal(order: OpenRequest) -> dict:
    """客户侧白名单：渠道、成本、返点一律不下发。"""
    method = str(order.open_method or OpenMethod.AUTO)
    return {
        "id": order.id,
        "biz_no": order.biz_no,
        "platform": str(order.platform),
        "platform_label": PLATFORM_LABEL.get(order.platform, str(order.platform)),
        # 同一客户下可能有多个门户账号（如 AI404-c / AI404-p），
        # 列表的「申请人」必须回真实提交人，不能统一显示当前登录人。
        "applicant": order.applicant,
        "subject": order.subject,
        "apply_count": int(order.apply_count or 0),
        "completed_count": int(order.completed_count or 0),
        "open_method": method,
        "open_method_label": OPEN_METHOD_LABEL.get(method, method),
        "status": str(order.status),
        "status_label": OPEN_REQUEST_STATUS_LABEL.get(order.status, str(order.status)),
        "handler": order.approver,
        "reject_reason": order.reject_reason,
        "remark": order.remark,
        # 企业信息（客户自己填的，可回显）
        "license_url": order.license_url,
        "credit_code": order.credit_code,
        "registered_place": order.registered_place,
        # 账户信息（客户侧看不到渠道/成本/返点）
        "industry": order.industry,
        "target_country": order.target_country,
        "bc_ids": split_bc_ids(order.bc_ids),
        "product_link": order.product_link,
        "feedback": _portal_feedback(order),
        "account_ids": _receipt_accounts(order),
        "created_at": order.created_at.isoformat(sep=" ", timespec="seconds"),
        "processed_at": (order.processed_at or order.approved_at).isoformat(
            sep=" ", timespec="seconds"
        ) if (order.processed_at or order.approved_at) else None,
    }


def parse_status(value: str) -> OpenRequestStatus:
    try:
        return OpenRequestStatus(value)
    except ValueError as exc:
        raise StateError(f"状态取值非法：{value}") from exc


def _parse_platform(value: str) -> Platform:
    try:
        return Platform(str(value).strip().upper())
    except ValueError as exc:
        raise StateError(f"平台取值非法：{value}") from exc


def _parse_enabled_platform(value: str) -> Platform:
    """写入口专用：当前业务只做 TikTok，其余媒体一律不许新建开户单。"""
    platform = _parse_platform(value)
    if platform not in ENABLED_PLATFORMS:
        raise StateError(f"当前仅支持 TikTok 开户，平台取值非法：{value}")
    return platform


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min)


# ------------------------------------------------------------------ 查询

def list_open_requests(
    session: Session,
    *,
    status_f: str | None = None,
    client_id: int | None = None,
    platform: str | None = None,
    tab: str | None = None,
    q: str | None = None,
    biz_no: str | None = None,
    client_q: str | None = None,
    subject: str | None = None,
    handler: str | None = None,
    channel: str | None = None,
    account_id: str | None = None,
    source: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    conds = []
    if status_f:
        conds.append(OpenRequest.status == parse_status(status_f))
    if tab == "pending":
        conds.append(OpenRequest.status == OpenRequestStatus.PENDING_APPROVAL)
    if client_id:
        conds.append(OpenRequest.client_id == client_id)
    if platform:
        conds.append(OpenRequest.platform == _parse_platform(platform))
    if source:
        conds.append(OpenRequest.source == str(source).upper())
    if biz_no:
        conds.append(OpenRequest.biz_no.ilike(f"%{biz_no.strip()}%"))
    if client_q:
        pattern = f"%{client_q.strip()}%"
        conds.append(or_(Client.company_name.ilike(pattern), Client.biz_no.ilike(pattern)))
    if subject:
        conds.append(OpenRequest.subject.ilike(f"%{subject.strip()}%"))
    if handler:
        conds.append(OpenRequest.approver.ilike(f"%{handler.strip()}%"))
    if channel:
        conds.append(Client.channel == channel)
    if account_id:
        # 回执 JSON 里带账户 ID；只做文本包含，足够列表定位
        conds.append(OpenRequest.agent_receipt.ilike(f"%{account_id.strip()}%"))
    if q:
        pattern = f"%{q.strip()}%"
        conds.append(
            or_(
                OpenRequest.biz_no.ilike(pattern),
                OpenRequest.subject.ilike(pattern),
                Client.company_name.ilike(pattern),
                Client.biz_no.ilike(pattern),
            )
        )
    if date_from:
        conds.append(OpenRequest.created_at >= _day_start(date_from))
    if date_to:
        conds.append(OpenRequest.created_at < _day_start(date_to + timedelta(days=1)))

    stmt = (
        select(OpenRequest, Client)
        .outerjoin(Client, Client.id == OpenRequest.client_id)
        .where(*conds)
    )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(OpenRequest.created_at.desc(), OpenRequest.id.desc())
        .limit(limit).offset(offset)
    ).all()

    counts = {str(s): 0 for s in OpenRequestStatus}
    for s, n in session.execute(
        select(OpenRequest.status, func.count())
        .select_from(OpenRequest)
        .outerjoin(Client, Client.id == OpenRequest.client_id)
        .where(*conds)
        .group_by(OpenRequest.status)
    ).all():
        counts[str(s)] = int(n)

    # 顶部「待处理」用不受筛选影响的真实待办数
    pending_total = session.execute(
        select(func.count())
        .select_from(OpenRequest)
        .where(OpenRequest.status == OpenRequestStatus.PENDING_APPROVAL)
    ).scalar_one()

    return {
        "total": int(total),
        "items": [serialize(o, c) for o, c in rows],
        "summary": counts,
        "pending_total": int(pending_total),
        "tab": tab or "all",
    }


def list_client_requests(
    session: Session,
    *,
    client_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    subject: str | None = None,
    biz_no: str | None = None,
    applicant: str | None = None,
    platform: str | None = None,
    status_f: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    base = [OpenRequest.client_id == client_id]
    if date_from:
        base.append(OpenRequest.created_at >= _day_start(date_from))
    if date_to:
        base.append(OpenRequest.created_at < _day_start(date_to + timedelta(days=1)))
    if subject:
        base.append(OpenRequest.subject.ilike(f"%{subject.strip()}%"))
    if biz_no:
        base.append(OpenRequest.biz_no.ilike(f"%{biz_no.strip()}%"))
    if applicant:
        base.append(OpenRequest.applicant.ilike(f"%{applicant.strip()}%"))

    conds = list(base)
    if platform:
        conds.append(OpenRequest.platform == _parse_platform(platform))
    if status_f:
        conds.append(OpenRequest.status == parse_status(status_f))

    total = session.execute(
        select(func.count()).select_from(OpenRequest).where(*conds)
    ).scalar_one()
    rows = session.execute(
        select(OpenRequest).where(*conds)
        .order_by(OpenRequest.created_at.desc(), OpenRequest.id.desc())
        .limit(limit).offset(offset)
    ).scalars().all()

    counts = {str(s): 0 for s in OpenRequestStatus}
    for s, n in session.execute(
        select(OpenRequest.status, func.count())
        .select_from(OpenRequest)
        # 状态计数用「除状态外」的条件，否则选了某个状态后其它状态全是 0
        .where(*base)
        .group_by(OpenRequest.status)
    ).all():
        counts[str(s)] = int(n)

    return {
        "total": int(total),
        "items": [serialize_portal(o) for o in rows],
        "summary": counts,
    }


def license_hints(
    session: Session, *, client: Client | None, file_obj: FileObject
) -> dict:
    """营业执照 / 香港 BR：真 OCR 读图带出字段，识别不出再逐级降级。

    顺序（后续每一步都只补空字段，不覆盖识别结果）：
    1. 本地 OCR 读图：营业执照 → 名称/信用代码/注册地；BR → 名称/登记证号 + 注册地 HK；
    2. 这张图就是客户档案里那张 → 用档案补 OCR 漏掉的字段；
    3. 文件名里能读出的信用代码/公司名/地区；
    4. 都不行 → ``parsed=False``，前端提示人工补充。

    绝不伪造：OCR 不可用、读图失败、关键字段缺失都会写进 ``note`` / ``warnings``。
    """
    from ..models.enums import OPEN_REGISTERED_PLACE_OPTIONS
    from . import license_ocr

    url = storage.url_of(file_obj)
    profile: dict[str, str] | None = None
    if client is not None and client.license_url and client.license_url == url:
        profile = {
            "name": client.company_name or "",
            "credit_code": client.credit_code or "",
            "registered_place": client.registered_place or "",
        }

    try:
        ocr = license_ocr.parse_file(
            storage.safe_resolve(file_obj.key), sha256=file_obj.sha256
        )
    except Exception as exc:  # noqa: BLE001 - 文件丢了/越界也不能把接口打崩
        ocr = {
            "parsed": False,
            "note": f"证照读取失败（{type(exc).__name__}），请手动填写",
            "warnings": [],
        }

    if ocr.get("parsed"):
        result = dict(ocr)
        filled: list[str] = []
        if profile:
            for key in ("name", "credit_code", "registered_place"):
                if not result.get(key) and profile.get(key):
                    result[key] = profile[key]
                    filled.append(key)
        note = result.get("note") or "已识别证照信息，请核对"
        if filled:
            note += "；未识别字段已按客户档案补齐"
        result["note"] = note
        result["source"] = "ocr"
        return result

    if profile:
        return {
            "parsed": True,
            "source": "profile",
            "doc_type": ocr.get("doc_type", "UNKNOWN"),
            "doc_type_label": ocr.get("doc_type_label", ""),
            "name": profile["name"],
            "credit_code": profile["credit_code"],
            "registered_place": profile["registered_place"],
            "warnings": ocr.get("warnings", []),
            "ocr_note": ocr.get("note", ""),
            "note": "证照文字未识别，已按客户档案带出，请核对后再提交",
        }

    import re
    from pathlib import PurePath

    stem = PurePath(file_obj.original_name or "").stem
    text = re.sub(r"[_\-—()（）\s]+", " ", stem)

    credit = ""
    m = re.search(r"[0-9A-HJ-NPQRTUWXY]{18}", text.upper())
    if m:
        credit = m.group(0)

    name = ""
    for part in text.split(" "):
        part = part.strip()
        if len(part) >= 4 and re.search(r"(公司|商行|商店|店|厂|中心|工作室|合作社|集团|企业)", part):
            name = part
            break

    place = ""
    for code, label in OPEN_REGISTERED_PLACE_OPTIONS.items():
        if label and label in text:
            place = code
            break
    if not place:
        for label, code in {
            "北京": "CN", "上海": "CN", "广东": "CN", "深圳": "CN", "广州": "CN",
            "浙江": "CN", "杭州": "CN", "江苏": "CN", "福建": "CN", "厦门": "CN",
            "四川": "CN", "成都": "CN", "山东": "CN", "湖南": "CN", "湖北": "CN",
            "河南": "CN", "河北": "CN", "安徽": "CN", "江西": "CN", "陕西": "CN",
            "重庆": "CN", "天津": "CN", "香港": "HK", "台湾": "TW", "新加坡": "SG",
        }.items():
            if label in text:
                place = code
                break

    found = bool(name or credit or place)
    ocr_note = ocr.get("note") or "未识别到证照文字"
    return {
        "parsed": found,
        "source": "filename" if found else "manual",
        "doc_type": ocr.get("doc_type", "UNKNOWN"),
        "doc_type_label": ocr.get("doc_type_label", ""),
        "name": name,
        "credit_code": credit,
        "registered_place": place,
        "warnings": ocr.get("warnings", []),
        "ocr_note": ocr_note,
        "note": (
            f"已从文件名带出部分信息，请核对（{ocr_note}）"
            if found
            else f"{ocr_note}，请手动填写企业信息"
        ),
    }


# ------------------------------------------------------------------ 提交申请

def create_open_request(
    session: Session,
    *,
    client_id: int,
    platform: str,
    bm_id: str | None = None,
    timezone: str | None = None,
    currency: str = "USD",
    daily_limit: Decimal | str | int = 0,
    subject: str | None = None,
    apply_count: int = 1,
    remark: str | None = None,
    operator: str | None = None,
    source: str = OpenRequestSource.DESK,
    license_url: str | None = None,
    credit_code: str | None = None,
    registered_place: str | None = None,
    industry: str | None = None,
    target_country: str | None = None,
    bc_ids: list[str] | None = None,
    product_link: str | None = None,
) -> OpenRequest:
    client = session.get(Client, client_id)
    if client is None:
        raise StateError(f"客户不存在（id={client_id}）")
    if apply_count < 1 or apply_count > 20:
        raise StateError("申请开户数必须在 1–20 之间")
    platform_enum = _parse_enabled_platform(platform)

    order = OpenRequest(
        client_id=client_id,
        platform=platform_enum,
        bm_id=bm_id,
        timezone=timezone,
        currency=currency or client.settle_currency,
        daily_limit=money(daily_limit),
        source=str(source),
        subject=(subject or "").strip() or None,
        apply_count=int(apply_count),
        completed_count=0,
        open_method=str(OpenMethod.AUTO),
        applicant=operator,
        remark=remark,
        license_url=license_url,
        credit_code=credit_code,
        registered_place=registered_place,
        industry=industry,
        target_country=target_country,
        bc_ids=",".join(bc_ids or []) or None,
        product_link=product_link,
        status=OpenRequestStatus.PENDING_APPROVAL,
    )
    session.add(order)
    session.flush()
    order.biz_no = f"OR{utcnow():%Y%m%d}{order.id:05d}"
    session.flush()
    return order


def create_client_application(
    session: Session,
    *,
    client_id: int,
    platform: str,
    subject: str,
    apply_count: int = 1,
    bm_id: str | None = None,
    timezone: str | None = None,
    currency: str | None = None,
    daily_limit: Decimal | str | int = 0,
    remark: str | None = None,
    operator: str | None = None,
    license_url: str | None = None,
    credit_code: str | None = None,
    registered_place: str | None = None,
    industry: str | None = None,
    target_country: str | None = None,
    bc_ids: list[str] | None = None,
    product_link: str | None = None,
) -> OpenRequest:
    """客户门户提交开户申请。客户身份只来自登录态（调用方保证）。"""
    client = session.get(Client, client_id)
    if client is None:
        raise StateError("客户档案不存在")
    if client.status is ClientStatus.DISABLED:
        raise StateError("客户已停用，不能申请开户")
    if client.status is not ClientStatus.ACTIVE:
        raise StateError("客户尚未完成入驻审核，暂不能申请开户")

    subject = (subject or "").strip()
    if not subject:
        raise StateError("开户主体不能为空")
    if len(subject) > 200:
        raise StateError("开户主体过长")
    if apply_count < 1 or apply_count > 20:
        raise StateError("申请开户数必须在 1–20 之间")
    platform_enum = _parse_enabled_platform(platform)
    if money(daily_limit) < 0:
        raise StateError("日限额不能为负数")

    # 新版开户页带营业执照。老接口（不带 license_url）保持可用，
    # 但只要带了执照，就必须是本人上传的文件，且企业信息齐全。
    if license_url:
        try:
            key = storage.key_from_url(license_url)
        except Exception as exc:  # InvalidFile
            raise StateError("营业执照必须是上传接口返回的文件地址") from exc
        obj = storage.find_by_key(session, key)
        if obj is None or obj.client_id != client_id or not storage.exists_on_disk(license_url):
            raise StateError("营业执照文件不存在或不属于当前客户")
        if not (credit_code or "").strip():
            raise StateError("请填写信用代码/登记证号")
        if not (target_country or "").strip():
            raise StateError("请选择投放国家")
        if not (industry or "").strip():
            raise StateError("请选择账户行业")
        if not (product_link or "").strip():
            raise StateError("请填写产品链接")

    bc_list = []
    for raw in bc_ids or []:
        item = str(raw).strip()
        if not item:
            continue
        if len(item) > 64:
            raise StateError("BCID 过长")
        if item not in bc_list:
            bc_list.append(item)
    if len(bc_list) > 10:
        raise StateError("绑定 BCID 最多 10 个")

    link = (product_link or "").strip()
    if link and not link.lower().startswith(("http://", "https://")):
        raise StateError("产品链接必须以 http:// 或 https:// 开头")

    in_flight = session.execute(
        select(func.count()).select_from(OpenRequest).where(
            OpenRequest.client_id == client_id,
            OpenRequest.platform == platform_enum,
            OpenRequest.subject == subject,
            OpenRequest.status.in_(ACTIVE_STATUSES),
        )
    ).scalar_one()
    if in_flight:
        raise StateError("同一主体、同一媒体已有在办的开户申请，请等待处理")

    order = create_open_request(
        session,
        client_id=client_id,
        platform=str(platform_enum),
        bm_id=bm_id,
        timezone=timezone,
        currency=currency or client.settle_currency,
        daily_limit=daily_limit,
        subject=subject,
        apply_count=apply_count,
        remark=remark,
        operator=operator,
        source=OpenRequestSource.CLIENT,
        license_url=license_url,
        credit_code=(credit_code or "").strip() or None,
        registered_place=(registered_place or "").strip() or None,
        industry=(industry or "").strip() or None,
        target_country=(target_country or "").strip() or None,
        bc_ids=bc_list,
        product_link=link or None,
    )
    session.add(
        AuditLog(
            actor=operator,
            action="APPLY",
            doctype="open_request",
            doc_id=order.id,
            reason=remark,
            after=json.dumps(
                {
                    "platform": str(platform_enum),
                    "subject": subject,
                    "apply_count": int(apply_count),
                    "target_country": target_country,
                    "industry": industry,
                    "bc_ids": bc_list,
                },
                ensure_ascii=False,
            ),
        )
    )
    session.flush()
    return order


# ------------------------------------------------------------------ 审批

def _create_ad_account(
    session: Session, order: OpenRequest, client: Client | None, account_id: str
) -> AdAccount:
    exists = session.execute(
        select(AdAccount).where(AdAccount.account_id == account_id)
    ).scalar_one_or_none()
    if exists is not None:
        raise StateError("账户 ID 已存在")
    account = AdAccount(
        account_id=account_id,
        platform=order.platform,
        bm_id=order.bm_id,
        currency=order.currency,
        client_id=order.client_id,
        channel=client.channel if client else None,
        balance=Decimal("0"),
        daily_limit=money(order.daily_limit),
        timezone=order.timezone,
        status=AdAccountStatus.NORMAL,
        opened_at=utcnow(),
    )
    session.add(account)
    session.flush()
    return account


def approve(
    session: Session,
    request_id: int,
    *,
    checks: dict | None,
    remark: str | None = None,
    operator: str | None = None,
    auto_complete: bool = False,
    account_ids: list[str] | None = None,
) -> OpenRequest:
    order = _lock(session, request_id)
    if order.status is not OpenRequestStatus.PENDING_APPROVAL:
        raise StateError(f"当前状态 {order.status} 不允许审批")

    checks = checks or {}
    missing = [k for k in REQUIRED_CHECKS if not checks.get(k)]
    if missing:
        raise StateError(f"核对清单未全部确认：{missing[0]}")

    order.approver = operator
    order.approved_at = utcnow()
    order.processed_at = utcnow()

    if auto_complete:
        # 管理端一步完成「审批 + 开户」：自动开户生成 Mock 账户，人工开户用传入的 ID
        client = session.get(Client, order.client_id)
        if client is not None and client.status is ClientStatus.DISABLED:
            raise StateError("客户已停用，不能开户")
        apply_count = int(order.apply_count or 1)
        provided = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
        if provided and len(provided) != apply_count:
            raise StateError(
                f"账户 ID 数量（{len(provided)}）与申请开户数（{apply_count}）不一致"
            )
        ids = provided or [f"AUTO-{order.biz_no}-{i}" for i in range(1, apply_count + 1)]
        if len(set(ids)) != len(ids):
            raise StateError("账户 ID 有重复")
        created_accounts = [_create_ad_account(session, order, client, acc_id) for acc_id in ids]

        order.open_method = str(OpenMethod.MANUAL if provided else OpenMethod.AUTO)
        order.completed_count = len(ids)
        order.status = OpenRequestStatus.OPENED
        order.agent_receipt = json.dumps(
            {
                "mock": not provided,
                "auto": not provided,
                "result": "OPENED",
                "accounts": ids,
                "ad_account_ids": [a.id for a in created_accounts],
                "at": utcnow().isoformat(sep=" ", timespec="seconds"),
            },
            ensure_ascii=False,
        )
    else:
        order.status = OpenRequestStatus.PENDING_SUBMIT

    # 审批备注 OpenRequest 表没有独立列，用审计日志留痕
    session.add(
        AuditLog(
            actor=operator,
            action="APPROVE",
            doctype="open_request",
            doc_id=order.id,
            reason=remark,
            after=json.dumps(
                {
                    "status": str(order.status),
                    "auto_complete": bool(auto_complete),
                    "completed_count": int(order.completed_count or 0),
                },
                ensure_ascii=False,
            ),
        )
    )
    session.flush()
    return order


def reject(
    session: Session, request_id: int, *, reason: str | None, operator: str | None = None
) -> OpenRequest:
    if not (reason or "").strip():
        raise StateError("驳回必须填写原因")
    order = _lock(session, request_id)
    if order.status is not OpenRequestStatus.PENDING_APPROVAL:
        raise StateError(f"当前状态 {order.status} 不允许驳回")

    order.status = OpenRequestStatus.REJECTED
    order.reject_reason = reason
    order.approver = operator
    order.processed_at = utcnow()
    session.add(
        AuditLog(
            actor=operator,
            action="REJECT",
            doctype="open_request",
            doc_id=order.id,
            reason=reason,
        )
    )
    session.flush()
    return order


# ------------------------------------------------------------------ 提交 RPA

def submit(session: Session, request_id: int, *, operator: str | None = None) -> OpenRequest:
    order = _lock(session, request_id)
    if order.status is not OpenRequestStatus.PENDING_SUBMIT:
        # 验收要断言这句，文案不要改
        raise StateError(f"状态 {order.status} 未审批通过，不允许提交到一代")

    order.status = OpenRequestStatus.AGENT_REVIEWING
    order.agent_receipt = rpa_mock.enqueue_open(order)
    session.flush()
    return order


def batch_submit(session: Session, ids: list[int], *, operator: str | None = None) -> dict:
    """整批不因个别失败回滚：逐单提交，失败的记 skipped。"""
    submitted: list[int] = []
    skipped: list[dict] = []
    for rid in ids:
        try:
            submit(session, rid, operator=operator)
            submitted.append(rid)
        except LedgerError as exc:
            skipped.append({"id": rid, "reason": str(exc)})
    return {"submitted": submitted, "skipped": skipped}


# ------------------------------------------------------------------ Mock 回执

def apply_receipt(
    session: Session,
    request_id: int,
    *,
    result: str,
    account_id: str | None = None,
    account_ids: list[str] | None = None,
    reason: str | None = None,
    operator: str | None = None,
) -> tuple[OpenRequest, int | None]:
    order = _lock(session, request_id)
    if order.status is not OpenRequestStatus.AGENT_REVIEWING:
        raise StateError(f"当前状态 {order.status} 不允许回执")

    verdict = (result or "").strip().upper()
    if verdict == "OPENED":
        client = session.get(Client, order.client_id)
        ids = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
        if account_id and account_id.strip():
            ids.insert(0, account_id.strip())
        remaining = max(int(order.apply_count or 1) - int(order.completed_count or 0), 0)
        if not ids and str(order.source) == str(OpenRequestSource.CLIENT):
            # 客户申请走「自动开户」，Mock RPA 回执自动补齐账户 ID
            start = int(order.completed_count or 0) + 1
            ids = [f"AUTO-{order.biz_no}-{i}" for i in range(start, start + remaining)]
        if not ids:
            raise StateError("下户回执必须提供账户 ID")
        if len(ids) > remaining:
            raise StateError(f"回执账户数（{len(ids)}）超过剩余申请开户数（{remaining}）")
        if len(set(ids)) != len(ids):
            raise StateError("账户 ID 有重复")

        created: list[AdAccount] = []
        for acc_id in ids:
            created.append(_create_ad_account(session, order, client, acc_id))

        order.completed_count = int(order.completed_count or 0) + len(ids)
        if order.completed_count >= int(order.apply_count or 1):
            order.status = OpenRequestStatus.OPENED
            order.processed_at = utcnow()
            order.open_method = str(
                OpenMethod.MANUAL if account_id or account_ids else OpenMethod.AUTO
            )
        order.agent_receipt = json.dumps(
            {
                "mock": True,
                "result": verdict,
                "accounts": ids,
                "ad_account_ids": [a.id for a in created],
                "at": utcnow().isoformat(sep=" ", timespec="seconds"),
            },
            ensure_ascii=False,
        )
        return order, created[0].id if created else None

    if verdict == "AGENT_REJECTED":
        if not (reason or "").strip():
            raise StateError("一代拒绝必须填写原因")
        order.status = OpenRequestStatus.AGENT_REJECTED
        order.reject_reason = reason
        order.processed_at = utcnow()
        order.agent_receipt = json.dumps(
            {
                "mock": True,
                "result": verdict,
                "reason": reason,
                "at": utcnow().isoformat(sep=" ", timespec="seconds"),
            },
            ensure_ascii=False,
        )
        return order, None

    raise StateError(f"回执结果只能是 OPENED / AGENT_REJECTED（收到 {result}）")
