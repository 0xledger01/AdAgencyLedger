"""CRM 客户同步。

本系统不直连 CRM（没有凭证、也不该在后台任务里偷偷拉数据），只认一份人工导出的
`data/crm_clients.json`。文件形如：

    {"clients": [
        {"crm_code": "H269150", "crm_name": "5分钟二次验证",
         "login_email": "client1", "biz_no": "C202610270001"}
    ]}

匹配顺序：CRM 编号 → 登录邮箱 → 本地客户编号。只补/改 CRM 两个字段，
不碰余额、授信、状态，也不新建客户。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DATA_DIR
from ..models.client import Client
from ..models.user import Role, User
from .errors import LedgerError

CRM_FILE = DATA_DIR / "crm_clients.json"


class CrmError(LedgerError):
    code = "REVIEW_ERROR"


def sync_from_crm(session: Session, *, path: Path | None = None) -> dict:
    src = path or CRM_FILE
    if not src.exists():
        return {
            "ok": False,
            "configured": False,
            "synced": 0,
            "unchanged": 0,
            "unmatched": 0,
            "message": f"未配置 CRM 数据源：把导出的 {src.name} 放到 data/ 目录后再同步",
        }

    try:
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrmError(f"CRM 文件读不出来：{exc}") from exc

    records = raw.get("clients", []) if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise CrmError("CRM 文件格式不对：应为数组或 {\"clients\": [...]}")

    synced = unchanged = 0
    unmatched: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        crm_code = (rec.get("crm_code") or "").strip()
        crm_name = (rec.get("crm_name") or "").strip()
        login = (rec.get("login_email") or "").strip()
        biz_no = (rec.get("biz_no") or "").strip()
        if not (crm_code or crm_name):
            continue

        client = None
        if crm_code:
            client = session.execute(
                select(Client).where(Client.crm_code == crm_code)
            ).scalars().first()
        if client is None and biz_no:
            client = session.execute(
                select(Client).where(Client.biz_no == biz_no)
            ).scalars().first()
        if client is None and login:
            account = session.execute(
                select(User).where(User.username == login, User.role == Role.CLIENT)
            ).scalars().first()
            if account is not None:
                client = session.get(Client, account.client_id)

        if client is None:
            unmatched.append(crm_code or crm_name)
            continue

        changed = False
        if crm_code and client.crm_code != crm_code:
            client.crm_code = crm_code
            changed = True
        if crm_name and client.crm_name != crm_name:
            client.crm_name = crm_name
            changed = True
        if changed:
            synced += 1
        else:
            unchanged += 1

    session.flush()
    return {
        "ok": True,
        "configured": True,
        "synced": synced,
        "unchanged": unchanged,
        "unmatched": len(unmatched),
        "unmatched_keys": unmatched[:20],
        "message": f"同步完成：更新 {synced}，无变化 {unchanged}，未匹配 {len(unmatched)}",
    }
