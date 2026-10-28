"""客户 BCID 绑定：历史归纳、手工添加、默认项与开户预选。

「历史绑定」不是一张快照表，而是从既有事实推出来的：
广告账户 `bm_id`、开户单 `bm_id` / `bc_ids`。读取列表时做一次幂等对账，
把新出现的历史 BCID 补进 `client_bcid`，已经存在的行不改来源、不改默认项。

「手工添加」是运营/销售显式管理的绑定：同一个号如果原来是历史行，
就升级成 MANUAL；删除只删绑定，不动广告账户和开户单上的任何字段。

删除留墓碑（`is_deleted`）：历史归纳只补「从来没出现过」的号，已删除的号
不会在刷新时自己回来；要恢复只能手工重新添加（会复活同一行并标记 MANUAL）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import case as sa_case
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.base import utcnow
from ..models.client import AdAccount, ClientBcid
from ..models.docs import OpenRequest
from .errors import StateError
from .openreq import split_bc_ids

BCID_SOURCE_LABEL = {"MANUAL": "手工添加", "HISTORY": "历史绑定"}
BCID_SOURCE_DETAIL_LABEL = {
    "AD_ACCOUNT": "广告账户",
    "OPEN_REQUEST": "开户单",
    "MANUAL": "手工添加",
}

# TikTok BCID 是纯数字，但演示/历史数据里出现过 BM-xxx 形式；
# 这里只挡空白、中文和明显超长的输入，不做「必须纯数字」的硬校验。
_BCID_RE = re.compile(r"^[0-9A-Za-z._\-]{4,64}$")


def normalize(raw: str | None) -> str:
    return (raw or "").strip()


def validate(raw: str | None) -> str:
    value = normalize(raw)
    if not value:
        raise StateError("BCID 不能为空")
    if not _BCID_RE.match(value):
        raise StateError("BCID 只能是数字/字母（可含 - _ .），长度 4-64 位")
    return value


@dataclass
class HistoryBcid:
    bcid: str
    detail: str


def gather_history(session: Session, client_id: int) -> dict[str, HistoryBcid]:
    """从广告账户与开户单归纳出该客户的历史 BCID（不落库）。"""
    found: dict[str, HistoryBcid] = {}

    for (bm,) in session.execute(
        select(AdAccount.bm_id).where(
            AdAccount.client_id == client_id, AdAccount.bm_id.is_not(None)
        )
    ).all():
        value = normalize(bm)
        if value and value not in found:
            found[value] = HistoryBcid(value, "AD_ACCOUNT")

    for bm, bc_ids in session.execute(
        select(OpenRequest.bm_id, OpenRequest.bc_ids).where(
            OpenRequest.client_id == client_id
        )
    ).all():
        value = normalize(bm)
        if value and value not in found:
            found[value] = HistoryBcid(value, "OPEN_REQUEST")
        for item in split_bc_ids(bc_ids):
            value = normalize(item)
            if value and value not in found:
                found[value] = HistoryBcid(value, "OPEN_REQUEST")
    return found


def sync_history(session: Session, client_id: int) -> int:
    """把新出现的历史 BCID 补进绑定表，返回新增条数（幂等）。

    `existing` 故意包含墓碑行：用户删过的号不再自动补回，否则「删除」按钮
    刷新一次就失效。真正要恢复走 `add_manual()`。
    """
    history = gather_history(session, client_id)
    if not history:
        return 0
    existing = set(
        session.execute(
            select(ClientBcid.bcid).where(ClientBcid.client_id == client_id)
        ).scalars().all()
    )
    created = 0
    for bcid, item in history.items():
        if bcid in existing:
            continue
        session.add(
            ClientBcid(
                client_id=client_id,
                bcid=bcid,
                source="HISTORY",
                source_detail=item.detail,
                is_default=False,
                created_by=None,
            )
        )
        created += 1
    if created:
        session.flush()
    return created


def _order_clause():  # noqa: ANN202
    """默认项置顶，其次手工添加（新的在前），历史绑定按创建顺序。"""
    return (
        ClientBcid.is_default.desc(),
        sa_case((ClientBcid.source == "MANUAL", 0), else_=1),
        # 手工行倒序：刚添加的那条落在第 1 页，不会被翻页藏起来
        sa_case((ClientBcid.source == "MANUAL", -ClientBcid.id), else_=ClientBcid.id).asc(),
    )


def serialize(row: ClientBcid) -> dict:
    return {
        "id": row.id,
        "client_id": row.client_id,
        "bcid": row.bcid,
        "source": row.source,
        "source_label": BCID_SOURCE_LABEL.get(row.source, row.source),
        "source_detail": row.source_detail,
        "source_detail_label": BCID_SOURCE_DETAIL_LABEL.get(row.source_detail or "", None),
        "is_default": bool(row.is_default),
        "is_deleted": bool(row.is_deleted),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds")
        if row.created_at
        else None,
    }


def list_page(
    session: Session,
    client_id: int,
    *,
    keyword: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[ClientBcid], int]:
    conds = [ClientBcid.client_id == client_id, ClientBcid.is_deleted.is_(False)]
    if keyword and keyword.strip():
        conds.append(ClientBcid.bcid.like(f"%{keyword.strip()}%"))
    total = session.execute(
        select(sa_func.count()).select_from(ClientBcid).where(*conds)
    ).scalar_one()
    rows = session.execute(
        select(ClientBcid)
        .where(*conds)
        .order_by(*_order_clause())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return list(rows), int(total)


def summary(session: Session, client_id: int) -> dict:
    rows = session.execute(
        select(ClientBcid.source, sa_func.count())
        .where(ClientBcid.client_id == client_id, ClientBcid.is_deleted.is_(False))
        .group_by(ClientBcid.source)
    ).all()
    by_source = {str(src): int(n) for src, n in rows}
    default_row = session.execute(
        select(ClientBcid).where(
            ClientBcid.client_id == client_id,
            ClientBcid.is_default.is_(True),
            ClientBcid.is_deleted.is_(False),
        )
    ).scalars().first()
    return {
        "manual": by_source.get("MANUAL", 0),
        "history": by_source.get("HISTORY", 0),
        "default_bcid": default_row.bcid if default_row else None,
    }


def get_owned(session: Session, client_id: int, bcid_id: int) -> ClientBcid:
    row = session.get(ClientBcid, bcid_id)
    if row is None or row.client_id != client_id or row.is_deleted:
        raise StateError("BCID 绑定不存在")
    return row


def clear_default(session: Session, client_id: int, *, exclude_id: int | None = None) -> None:
    rows = session.execute(
        select(ClientBcid).where(
            ClientBcid.client_id == client_id,
            ClientBcid.is_default.is_(True),
            ClientBcid.is_deleted.is_(False),
        )
    ).scalars().all()
    for row in rows:
        if exclude_id is not None and row.id == exclude_id:
            continue
        row.is_default = False


def add_manual(
    session: Session,
    client_id: int,
    *,
    bcid: str,
    operator: str | None,
    set_default: bool = False,
) -> tuple[ClientBcid, bool, bool]:
    """手工添加绑定，返回 (行, 是否新建, 是否复活墓碑)。

    已存在则升级为 MANUAL（不重复插入）；同号之前被删过则复活同一行，
    不会新建重复行（`uq_client_bcid` 兜底）。
    """
    value = validate(bcid)
    row = session.execute(
        select(ClientBcid).where(
            ClientBcid.client_id == client_id, ClientBcid.bcid == value
        )
    ).scalars().first()
    created = row is None
    revived = bool(row is not None and row.is_deleted)
    if row is None:
        row = ClientBcid(
            client_id=client_id,
            bcid=value,
            source="MANUAL",
            source_detail="MANUAL",
            is_default=False,
            created_by=operator,
        )
        session.add(row)
        session.flush()
    else:
        if revived:
            row.is_deleted = False
            row.deleted_at = None
        row.source = "MANUAL"
        row.source_detail = "MANUAL"
        if not row.created_by:
            row.created_by = operator
    if set_default:
        clear_default(session, client_id, exclude_id=row.id)
        row.is_default = True
    session.flush()
    return row, created, revived


def set_default(
    session: Session, client_id: int, bcid_id: int, *, is_default: bool
) -> ClientBcid:
    row = get_owned(session, client_id, bcid_id)
    if is_default:
        clear_default(session, client_id, exclude_id=row.id)
        row.is_default = True
    else:
        row.is_default = False
    session.flush()
    return row


def delete(session: Session, client_id: int, bcid_id: int) -> ClientBcid:
    """软删除绑定：留墓碑，历史归纳不再补回；广告账户/开户单不动。"""
    row = get_owned(session, client_id, bcid_id)
    row.is_deleted = True
    row.deleted_at = utcnow()
    row.is_default = False
    session.flush()
    return row


def default_bcid(session: Session, client_id: int) -> str | None:
    return session.execute(
        select(ClientBcid.bcid).where(
            ClientBcid.client_id == client_id,
            ClientBcid.is_default.is_(True),
            ClientBcid.is_deleted.is_(False),
        )
    ).scalars().first()


def options(session: Session, client_id: int) -> list[str]:
    """开户页的 BCID 选项：默认项置顶，然后是手工添加/历史绑定，去重保序。

    只读：把库里的绑定与当前仍能推导出的历史合并，不在这里落库，
    免得门户的 GET 产生副作用。
    """
    rows = session.execute(
        select(ClientBcid)
        .where(ClientBcid.client_id == client_id, ClientBcid.is_deleted.is_(False))
        .order_by(*_order_clause())
    ).scalars().all()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.bcid in seen:
            continue
        seen.add(row.bcid)
        out.append(row.bcid)
    # 墓碑行同样不能从历史里漏回来
    deleted = set(
        session.execute(
            select(ClientBcid.bcid).where(
                ClientBcid.client_id == client_id, ClientBcid.is_deleted.is_(True)
            )
        ).scalars().all()
    )
    for bcid in sorted(gather_history(session, client_id)):
        if bcid in deleted:
            continue
        if bcid in seen:
            continue
        seen.add(bcid)
        out.append(bcid)
    return out
