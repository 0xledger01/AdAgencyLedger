"""问题工单（管理端）：查看客户提交的问题、回复、结单。

客户在门户 `/api/portal/tickets` 提工单；这里只做受理，不碰任何资金：

- 列表按状态 / 优先级 / 客户 / 关键词过滤，销售只能看自己名下客户；
- 处理 = 改状态 + 追加回复，回复对客户可见，写 audit_log 留痕；
- 状态机固定在 `models/enums.TICKET_NEXT_STATUSES`，前端下拉也取后端返回值；
- 结单（已解决 / 已关闭）必须至少有一条回复，避免客户问题被静默关闭。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select

from ..models.base import utcnow
from ..models.client import Client
from ..models.docs import AuditLog
from ..models.enums import (
    TICKET_NEXT_STATUSES,
    TICKET_PRIORITY_LABEL,
    TICKET_STATUS_LABEL,
    TicketStatus,
)
from ..models.user import PortalTicket, User
from ..services.rbac import Perm, scope_sales_owner
from .deps import DbSession, require_desk, require_perm

router = APIRouter(prefix="/api/desk", tags=["问题工单"], dependencies=[Depends(require_desk)])

VALID_STATUS = {str(s) for s in TicketStatus}
VALID_PRIORITY = {str(p) for p in TICKET_PRIORITY_LABEL}

# 列表顺序：待处理 → 处理中 → 已解决 → 已关闭，同状态内新的在前。
_STATUS_RANK = case(
    (PortalTicket.status == str(TicketStatus.PENDING), 0),
    (PortalTicket.status == str(TicketStatus.PROCESSING), 1),
    (PortalTicket.status == str(TicketStatus.RESOLVED), 2),
    else_=3,
)


def _iso(value) -> str | None:  # noqa: ANN001
    return value.isoformat(sep=" ", timespec="seconds") if value else None


def _status_label(code: str) -> str:
    return TICKET_STATUS_LABEL.get(code, code)


def _priority_label(code: str) -> str:
    return TICKET_PRIORITY_LABEL.get(code, code)


def _next_statuses(current: str) -> list[str]:
    allowed = TICKET_NEXT_STATUSES.get(current)
    if allowed is None:
        # 历史脏状态（不是四个标准值）也允许原样保存回复，不把工单锁死
        allowed = (current, *TICKET_NEXT_STATUSES[TicketStatus.PENDING])
    seen: list[str] = []
    for code in allowed:
        code = str(code)
        if code not in seen:
            seen.append(code)
    return seen


def _excerpt(content: str | None, size: int = 80) -> str | None:
    text = (content or "").strip().replace("\r\n", "\n")
    if not text:
        return None
    one_line = " ".join(text.split())
    return one_line[:size] + ("…" if len(one_line) > size else "")


def _scope_guard(user: User, client: Client | None) -> None:
    """销售只能看 / 处理自己名下客户的工单。"""
    owner = scope_sales_owner(user)
    if owner and (client is None or client.sales_owner != owner):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能查看自己名下客户的工单")


def _row(t: PortalTicket, client: Client | None) -> dict:
    return {
        "id": t.id,
        "client_id": t.client_id,
        "client_name": client.company_name if client else None,
        "client_no": client.biz_no if client else None,
        "title": t.title,
        "category": t.category,
        "excerpt": _excerpt(t.content),
        "status": str(t.status),
        "status_label": _status_label(str(t.status)),
        "priority": str(t.priority or "NORMAL"),
        "priority_label": _priority_label(str(t.priority or "NORMAL")),
        "replied": bool((t.reply or "").strip()),
        "handled_by": t.handled_by,
        "handled_at": _iso(t.handled_at),
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
    }


def _detail(t: PortalTicket, client: Client | None) -> dict:
    data = _row(t, client)
    data.update(
        {
            "content": t.content,
            "reply": t.reply,
            "next_statuses": _next_statuses(str(t.status)),
            "client": {
                "id": client.id,
                "company_name": client.company_name,
                "biz_no": client.biz_no,
                "contact_name": client.contact_name,
                "phone": client.phone,
                "email": client.email,
                "sales_owner": client.sales_owner,
                "ae_owner": client.ae_owner,
            }
            if client
            else None,
        }
    )
    return data


def _client_map(db, rows: list[PortalTicket]) -> dict[int, Client]:  # noqa: ANN001
    ids = {t.client_id for t in rows}
    if not ids:
        return {}
    clients = db.execute(select(Client).where(Client.id.in_(ids))).scalars().all()
    return {c.id: c for c in clients}


@router.get("/tickets", response_model=dict, summary="问题工单列表")
def list_tickets(
    db: DbSession,
    user: User = require_perm(Perm.TICKET_VIEW),
    status_f: str | None = Query(None, alias="status", description="PENDING / PROCESSING / RESOLVED / CLOSED"),
    priority: str | None = Query(None, description="LOW / NORMAL / HIGH"),
    keyword: str | None = Query(None, description="标题 / 内容；#12 按工单编号精确查"),
    client: str | None = Query(None, description="客户名称或客户编号，模糊匹配"),
    limit: int = Query(20, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    if status_f and status_f not in VALID_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知工单状态：{status_f}")
    if priority and priority not in VALID_PRIORITY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知优先级：{priority}")

    owner = scope_sales_owner(user)
    base = db.query(PortalTicket).join(Client, Client.id == PortalTicket.client_id)
    if owner:
        base = base.filter(Client.sales_owner == owner)
    if keyword:
        kw = keyword.strip()
        if kw.startswith("#") and kw[1:].isdigit():
            # 「#12」按编号精确查；裸数字仍然走全文，避免把金额里的 1 当成单号
            base = base.filter(PortalTicket.id == int(kw[1:]))
        else:
            base = base.filter(
                or_(
                    PortalTicket.title.like(f"%{kw}%"),
                    PortalTicket.content.like(f"%{kw}%"),
                )
            )
    if client:
        like = f"%{client.strip()}%"
        base = base.filter(or_(Client.company_name.like(like), Client.biz_no.like(like)))
    if priority:
        base = base.filter(PortalTicket.priority == priority)

    # 状态计数要忽略状态筛选本身，否则切到「待处理」后其它 Tab 全是 0
    count_rows = (
        base.with_entities(PortalTicket.status, func.count())
        .group_by(PortalTicket.status)
        .all()
    )
    counts = {str(code): 0 for code in VALID_STATUS}
    for code, n in count_rows:
        counts[str(code)] = int(n)

    q = base
    if status_f:
        q = q.filter(PortalTicket.status == status_f)
    total = q.count()
    rows = (
        q.order_by(_STATUS_RANK, PortalTicket.created_at.desc(), PortalTicket.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    clients = _client_map(db, rows)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": {
            "counts": counts,
            "total": sum(counts.values()),
            "pending": counts[str(TicketStatus.PENDING)],
            "processing": counts[str(TicketStatus.PROCESSING)],
        },
        "items": [_row(t, clients.get(t.client_id)) for t in rows],
    }


@router.get("/tickets/{ticket_id}", response_model=dict, summary="问题工单详情")
def ticket_detail(
    ticket_id: int, db: DbSession, user: User = require_perm(Perm.TICKET_VIEW)
) -> dict:
    t = db.get(PortalTicket, ticket_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    client = db.get(Client, t.client_id)
    _scope_guard(user, client)
    return _detail(t, client)


class HandleIn(BaseModel):
    status: str | None = Field(None, max_length=20)
    priority: str | None = Field(None, max_length=10)
    reply: str | None = Field(None, max_length=4000)


@router.post("/tickets/{ticket_id}/handle", response_model=dict, summary="受理 / 回复 / 结单")
def handle_ticket(
    ticket_id: int,
    body: HandleIn,
    db: DbSession,
    user: User = require_perm(Perm.TICKET_HANDLE),
) -> dict:
    t = db.get(PortalTicket, ticket_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    client = db.get(Client, t.client_id)
    _scope_guard(user, client)

    before = {"status": str(t.status), "priority": str(t.priority or "NORMAL")}
    new_status = (body.status or str(t.status)).strip()
    new_priority = (body.priority or str(t.priority or "NORMAL")).strip()
    if new_status not in VALID_STATUS and new_status != str(t.status):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知工单状态：{new_status}")
    if new_priority not in VALID_PRIORITY and new_priority != str(t.priority or "NORMAL"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知优先级：{new_priority}")

    if new_status != str(t.status) and new_status not in _next_statuses(str(t.status)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"工单不能从「{_status_label(str(t.status))}」直接变为「{_status_label(new_status)}」",
        )

    changed: list[str] = []
    if new_status != str(t.status):
        t.status = new_status
        changed.append("status")
    if new_priority != str(t.priority or "NORMAL"):
        t.priority = new_priority
        changed.append("priority")

    reply_text = (body.reply or "").strip()
    if reply_text:
        stamp = f"[{utcnow():%Y-%m-%d %H:%M} {user.display_name}]"
        block = f"{stamp}\n{reply_text}"
        t.reply = f"{t.reply}\n\n{block}" if (t.reply or "").strip() else block
        changed.append("reply")

    if not changed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要保存的修改")

    # 结单必须有给客户的说明；只改优先级不算处理完成
    if new_status in {str(TicketStatus.RESOLVED), str(TicketStatus.CLOSED)} and not (
        t.reply or ""
    ).strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "结单前请填写回复内容，客户会在门户看到处理结果"
        )

    t.handled_by = user.display_name
    t.handled_at = utcnow()

    db.add(
        AuditLog(
            actor=user.display_name,
            action="ticket.handle",
            doctype="portal_ticket",
            doc_id=t.id,
            before=json.dumps(before, ensure_ascii=False),
            after=json.dumps(
                {"status": str(t.status), "priority": str(t.priority), "fields": changed},
                ensure_ascii=False,
            ),
            reason=f"处理问题工单 #{t.id}：{_status_label(str(t.status))}",
        )
    )
    db.commit()
    return _detail(t, client)
