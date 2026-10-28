"""客户管理 + 入驻三级审核（D1）。

状态机（PRD 4.3）：
    待审核 --初审通过--> 待风控 --风控通过--> 待定额度 --定额--> 已开通
       |                    |
       +--驳回--> 已驳回 <--+
"""

from __future__ import annotations

import csv
import io
import secrets
import string
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.base import money, utcnow
from ..models.client import Client
from ..models.enums import CLIENT_STATUS_LABEL, ClientStatus, OwnerType
from ..models.user import ClientApplication, ClientReview, Role, User
from ..models.wallet import Wallet
from .errors import LedgerError, StateError
from .auth import hash_password


class ReviewError(LedgerError):
    code = "REVIEW_ERROR"


# 每一步允许的角色
STEP_ROLE = {
    "REVIEW": {"SALES", "ADMIN"},
    "RISK": {"RISK", "ADMIN"},
    "LIMIT": {"FINANCE", "ADMIN"},
}

CURRENT_STEP = {
    ClientStatus.PENDING_REVIEW: "REVIEW",
    ClientStatus.PENDING_RISK: "RISK",
    ClientStatus.PENDING_LIMIT: "LIMIT",
}

NEXT_ON_APPROVE = {
    ClientStatus.PENDING_REVIEW: ClientStatus.PENDING_RISK,
    ClientStatus.PENDING_RISK: ClientStatus.PENDING_LIMIT,
    ClientStatus.PENDING_LIMIT: ClientStatus.ACTIVE,
}


def list_clients(
    session: Session,
    *,
    status: str | None = None,
    sales_owner: str | None = None,
    ae_owner: str | None = None,
    source_channel: str | None = None,
    keyword: str | None = None,
    name: str | None = None,
    codes: list[str] | None = None,
    enabled: str | None = None,
    active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Client], int]:
    stmt = select(Client)
    if status:
        stmt = stmt.where(Client.status == status)
    if sales_owner:
        stmt = stmt.where(Client.sales_owner == sales_owner)
    if ae_owner:
        stmt = stmt.where(Client.ae_owner == ae_owner)
    if source_channel:
        stmt = stmt.where(Client.source_channel == source_channel)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Client.company_name.like(like) | Client.biz_no.like(like))
    if name:
        like = f"%{name}%"
        stmt = stmt.where(
            Client.company_name.like(like)
            | Client.crm_name.like(like)
            | Client.crm_code.like(like)
        )
    if codes:
        # 客户编号支持多个（逗号 / 空格分隔），同时认本地编号与 CRM 编号
        stmt = stmt.where(or_(Client.biz_no.in_(codes), Client.crm_code.in_(codes)))
    if enabled in ("true", "yes", "1"):
        stmt = stmt.where(Client.status == ClientStatus.ACTIVE)
    elif enabled in ("false", "no", "0"):
        stmt = stmt.where(Client.status == ClientStatus.DISABLED)
    elif enabled == "unopened":
        stmt = stmt.where(
            Client.status.in_(
                [
                    ClientStatus.PENDING_REVIEW,
                    ClientStatus.PENDING_RISK,
                    ClientStatus.PENDING_LIMIT,
                    ClientStatus.REJECTED,
                ]
            )
        )
    if active is not None:
        cutoff = activity_cutoff()
        if active:
            stmt = stmt.where(Client.last_active_at.is_not(None), Client.last_active_at >= cutoff)
        else:
            stmt = stmt.where(
                or_(Client.last_active_at.is_(None), Client.last_active_at < cutoff)
            )

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(stmt.order_by(Client.id.desc()).limit(limit).offset(offset)).scalars().all()
    return list(rows), int(total)


def pending_count(session: Session, *, sales_owner: str | None = None) -> dict[str, int]:
    """按状态计数。sales_owner 必须与 list_clients 用同一个过滤条件，
    否则销售看到的是全公司待审数、列表却只有自己名下，KPI 与列表对不上。"""
    stmt = select(Client.status, func.count()).group_by(Client.status)
    if sales_owner:
        stmt = stmt.where(Client.sales_owner == sales_owner)
    rows = session.execute(stmt).all()
    counts = {str(s): int(c) for s, c in rows}
    return {
        "pending_review": counts.get(ClientStatus.PENDING_REVIEW.value, 0),
        "pending_risk": counts.get(ClientStatus.PENDING_RISK.value, 0),
        "pending_limit": counts.get(ClientStatus.PENDING_LIMIT.value, 0),
        "active": counts.get(ClientStatus.ACTIVE.value, 0),
    }


# ---------------------------------------------------------------- 客户管理列表

ACTIVE_WINDOW_DAYS = 30
# 充值只做美金：系统不再提供人民币（CNY）
_CURRENCIES = {"USD", "HKD"}
_PWD_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "O0Il1")


def activity_cutoff() -> datetime:
    """活跃口径：最近 30 天有登录 / 有操作。集中在这里，列表与筛选共用一个阈值。"""
    from datetime import timedelta

    return utcnow() - timedelta(days=ACTIVE_WINDOW_DAYS)


def split_codes(raw: str | None) -> list[str]:
    """把「多个编号用逗号或空格分隔」洗干净，去重保序。"""
    if not raw:
        return []
    seen: list[str] = []
    for part in raw.replace("，", ",").replace(";", ",").replace("；", ",").split():
        for item in part.split(","):
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
    return seen


def generate_password(length: int = 12) -> str:
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(length))


def normalize_login(raw: str | None) -> str:
    """登录邮箱去空格并统一小写：客户手输大小写不一致也能登进门户。"""
    login = (raw or "").strip()
    return login.lower() if "@" in login else login


def default_client_password() -> str:
    """添加客户时「初始密码」留空所用的默认密码（CLIENT_DEFAULT_PASSWORD）。"""
    plain = (settings.client_default_password or "").strip()
    if len(plain) < 6:
        raise ReviewError("默认密码配置非法：CLIENT_DEFAULT_PASSWORD 至少 6 位")
    return plain


def _login_taken(session: Session, login: str, *, exclude_user_id: int | None = None) -> bool:
    stmt = select(User.id).where(func.lower(User.username) == login.lower())
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return session.execute(stmt).scalar_one_or_none() is not None


def client_rows(session: Session, clients: list[Client]) -> list[dict]:
    """列表 / 导出共用的一份行数据（金额、账本、登录邮箱、活跃口径）。"""
    if not clients:
        return []
    ids = [c.id for c in clients]

    wallets = session.execute(
        select(Wallet).where(
            Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id.in_(ids)
        )
    ).scalars().all()
    wallet_map = {(w.owner_id, w.currency): w for w in wallets}

    users = session.execute(
        select(User)
        .where(User.role == Role.CLIENT, User.client_id.in_(ids))
        .order_by(User.is_primary.desc(), User.id)
    ).scalars().all()
    user_map: dict[int, User] = {}
    for u in users:
        user_map.setdefault(u.client_id, u)  # 已按主账号优先排序

    cutoff = activity_cutoff()
    rows: list[dict] = []
    for c in clients:
        w = wallet_map.get((c.id, c.settle_currency))
        balance = money(w.balance) if w else money(0)
        frozen = money(w.frozen) if w else money(0)
        credit_limit = money(c.credit_limit)
        credit_available = credit_limit - money(c.credit_used)
        if credit_available < 0:
            credit_available = money(0)

        if c.status is ClientStatus.ACTIVE:
            enabled, enabled_label = True, "启用"
        elif c.status is ClientStatus.DISABLED:
            enabled, enabled_label = False, "停用"
        else:
            enabled, enabled_label = False, "未开通"

        active = bool(c.last_active_at and c.last_active_at >= cutoff)
        u = user_map.get(c.id)
        rows.append(
            {
                "id": c.id,
                "biz_no": c.biz_no,
                "company_name": c.company_name,
                "crm_name": c.crm_name,
                "crm_code": c.crm_code,
                "login_email": u.username if u else None,
                "login_active": bool(u.active) if u else None,
                "status": str(c.status),
                "status_label": str(CLIENT_STATUS_LABEL.get(c.status, c.status)),
                "enabled": enabled,
                "enabled_label": enabled_label,
                "active": active,
                "active_label": "活跃" if active else "不活跃",
                "last_active_at": c.last_active_at.isoformat(sep=" ", timespec="seconds") if c.last_active_at else None,
                "settle_currency": c.settle_currency,
                "source_channel": c.source_channel,
                "sales_owner": c.sales_owner,
                "ae_owner": c.ae_owner,
                "wallet_balance": str(balance),
                "wallet_frozen": str(frozen),
                "wallet_available": str(balance - frozen),
                "credit_limit": str(credit_limit),
                "credit_available": str(credit_available),
                "monthly_spend_expect": str(money(c.monthly_spend_expect)),
                "version": c.version,
                "reject_reason": c.reject_reason,
                "created_by": c.created_by,
                "created_at": c.created_at.isoformat(sep=" ", timespec="seconds"),
            }
        )
    return rows


def _raise(msg: str) -> None:
    raise ReviewError(msg)


def create_client(
    session: Session,
    *,
    operator: User,
    company_name: str,
    login_email: str | None = None,
    password: str | None = None,
    contact_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    wechat: str | None = None,
    crm_name: str | None = None,
    crm_code: str | None = None,
    sales_owner: str | None = None,
    ae_owner: str | None = None,
    source_channel: str | None = None,
    settle_currency: str = "USD",
    credit_limit: Decimal | str | None = None,
    monthly_spend_expect: Decimal | str | None = None,
    activate: bool = True,
) -> tuple[Client, User | None, str | None]:
    """运营「添加客户」：建客户档案 + 可选门户登录账号。

    默认直接启用（运营代客建档，和客户自助入驻的三级审核是两条路），
    activate=False 时落 PENDING_REVIEW，回到审核流程。
    """
    name = (company_name or "").strip()
    if not name:
        _raise("客户名称必填")

    ccy = (settle_currency or "USD").upper()
    if ccy not in _CURRENCIES:
        _raise(f"结算币种只能是 {'/'.join(sorted(_CURRENCIES))}")

    login = normalize_login(login_email)
    initial_password: str | None = None
    if login:
        if _login_taken(session, login):
            _raise(f"登录邮箱已被占用：{login}")
        if password is not None and password != "" and len(password) < 6:
            _raise("登录密码至少 6 位")

    if operator.role is Role.SALES:
        sales_owner = operator.display_name  # 销售只能把客户挂在自己名下
        # 销售不能自己把客户设成"已开通"——那会绕过风控与财务定额
        activate = False

    if credit_limit is not None and Decimal(str(credit_limit)) < 0:
        _raise("授信额度不能为负")

    status = ClientStatus.ACTIVE if activate else ClientStatus.PENDING_REVIEW
    client = Client(
        company_name=name,
        crm_name=(crm_name or "").strip() or None,
        crm_code=(crm_code or "").strip() or None,
        contact_name=contact_name or None,
        phone=phone or None,
        email=email or None,
        wechat=wechat or None,
        sales_owner=sales_owner or None,
        ae_owner=ae_owner or None,
        source_channel=source_channel or None,
        settle_currency=ccy,
        settle_intent="PREPAID",
        credit_limit=money(credit_limit or 0),
        monthly_spend_expect=money(monthly_spend_expect or 0),
        status=status,
        activated_at=utcnow() if activate else None,
        created_by=operator.display_name,
    )
    session.add(client)
    session.flush()
    client.biz_no = f"C{utcnow():%Y%m%d}{client.id:04d}"

    account: User | None = None
    if login:
        # 留空就用系统默认密码，客户拿到「邮箱 + 默认密码」即可登录门户
        plain = (password or "").strip() or default_client_password()
        if len(plain) < 6:
            _raise("登录密码至少 6 位")
        initial_password = plain
        account = User(
            username=login,
            password_hash=hash_password(plain),
            display_name=contact_name or name,
            role=Role.CLIENT,
            client_id=client.id,
            is_primary=True,
            active=True,
        )
        session.add(account)

    session.flush()
    return client, account, initial_password


_EDITABLE_FIELDS = {
    "company_name", "contact_name", "phone", "email", "wechat",
    "crm_name", "crm_code", "sales_owner", "ae_owner", "source_channel",
    "website", "registered_place", "industry", "target_regions",
}


def update_client(
    session: Session,
    *,
    client_id: int,
    operator: User,
    data: dict,
) -> tuple[Client, str | None]:
    """编辑客户基础资料。金额 / 授信走 set_terms，登录邮箱单独校验唯一性。"""
    client = session.get(Client, client_id, with_for_update=True)
    if client is None:
        _raise(f"客户不存在（id={client_id}）")

    if "company_name" in data:
        name = (data.get("company_name") or "").strip()
        if not name:
            _raise("客户名称不能为空")
        client.company_name = name

    for key in _EDITABLE_FIELDS:
        if key in data:
            value = data[key]
            if key == "sales_owner" and operator.role is Role.SALES:
                value = operator.display_name
            if isinstance(value, str):
                value = value.strip() or None
            setattr(client, key, value)

    if "monthly_spend_expect" in data and data["monthly_spend_expect"] is not None:
        expect = Decimal(str(data["monthly_spend_expect"]))
        if expect < 0:
            _raise("月消耗预期不能为负")
        client.monthly_spend_expect = money(expect)

    login = data.get("login_email")
    generated: str | None = None
    if login is not None:
        login = normalize_login(str(login))
        account = session.execute(
            select(User)
            .where(User.role == Role.CLIENT, User.client_id == client.id)
            .order_by(User.is_primary.desc(), User.id)
        ).scalars().first()
        if not login:
            _raise("登录邮箱不能为空（如需停用账号请在批量修改里停用）")
        if account is None:
            generated = default_client_password()
            account = User(
                username=login,
                password_hash=hash_password(generated),
                display_name=client.contact_name or client.company_name,
                role=Role.CLIENT,
                client_id=client.id,
                is_primary=True,
                active=True,
            )
            session.add(account)
        elif account.username != login:
            if _login_taken(session, login, exclude_user_id=account.id):
                _raise(f"登录邮箱已被占用：{login}")
            account.username = login

    session.flush()
    return client, generated


def reset_client_password(
    session: Session, *, client_id: int, password: str | None = None
) -> tuple[User, str]:
    """重置客户门户密码。返回 (账号, 新密码)。

    留空重置回系统默认密码：客户忘了密码时，运营点一下就能告诉客户
    「邮箱 + 默认密码」，不必再抄一串随机字符。密码只以 bcrypt 落库。
    """
    client = session.get(Client, client_id)
    if client is None:
        _raise(f"客户不存在（id={client_id}）")
    account = session.execute(
        select(User)
        .where(User.role == Role.CLIENT, User.client_id == client_id)
        .order_by(User.is_primary.desc(), User.id)
    ).scalars().first()
    if account is None:
        _raise("该客户还没有登录账号，请先在编辑里补登录邮箱")
    plain = (password or "").strip() or default_client_password()
    if len(plain) < 6:
        _raise("密码至少 6 位")
    account.password_hash = hash_password(plain)
    account.active = True
    session.flush()
    return account, plain


def batch_update_clients(
    session: Session,
    *,
    ids: list[int],
    operator: User,
    sales_owner: str | None = None,
    ae_owner: str | None = None,
    credit_limit: Decimal | str | None = None,
    enabled: bool | None = None,
    scope_owner: str | None = None,
) -> list[Client]:
    """批量修改：归属人 / 负责 AE / 授信额度 / 启用状态。逐条校验销售数据范围。"""
    if not ids:
        _raise("请先选择客户")
    if credit_limit is not None and Decimal(str(credit_limit)) < 0:
        _raise("授信额度不能为负")

    clients = session.execute(
        select(Client).where(Client.id.in_(ids)).with_for_update()
    ).scalars().all()
    found = {c.id for c in clients}
    missing = [i for i in ids if i not in found]
    if missing:
        _raise(f"客户不存在：{missing}")
    if scope_owner:
        outside = [c.id for c in clients if c.sales_owner != scope_owner]
        if outside:
            _raise(f"只能修改自己名下客户（越界：{outside}）")

    for c in clients:
        if sales_owner is not None:
            c.sales_owner = operator.display_name if operator.role is Role.SALES else (sales_owner.strip() or None)
        if ae_owner is not None:
            c.ae_owner = ae_owner.strip() or None
        if credit_limit is not None:
            c.credit_limit = money(credit_limit)
        if enabled is not None:
            c.status = ClientStatus.ACTIVE if enabled else ClientStatus.DISABLED
            if enabled and c.activated_at is None:
                c.activated_at = utcnow()

    session.flush()
    return list(clients)


def import_clients(
    session: Session, *, data: bytes, operator: User, scope_owner: str | None = None
) -> dict:
    """CSV 导入客户资料：按 客户编号 / 登录邮箱 匹配已有客户，只更新不新建。

    新建客户要落登录账号和初始密码，必须走「添加客户」逐条确认，
    所以导入遇到未知客户只报错，不静默建号。
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ReviewError("文件编码无法识别，请存成 UTF-8 CSV") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ReviewError("CSV 没有表头")

    header = {(h or "").strip(): h for h in reader.fieldnames}

    def pick(row: dict, *names: str) -> str | None:
        for n in names:
            key = header.get(n)
            if key is not None:
                v = (row.get(key) or "").strip()
                if v:
                    return v
        return None

    if not any(n in header for n in ("客户编号", "登录邮箱", "客户名称")):
        raise ReviewError("CSV 至少需要 客户编号 / 登录邮箱 / 客户名称 之一")

    updated = 0
    errors: list[dict] = []
    for line_no, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        code = pick(row, "客户编号", "biz_no")
        login = pick(row, "登录邮箱", "login_email")
        company = pick(row, "客户名称", "company_name")

        client = None
        if code:
            client = session.execute(
                select(Client).where(or_(Client.biz_no == code, Client.crm_code == code))
            ).scalars().first()
        if client is None and login:
            account = session.execute(
                select(User).where(
                    func.lower(User.username) == login.lower(), User.role == Role.CLIENT
                )
            ).scalars().first()
            if account is not None:
                client = session.get(Client, account.client_id)
        if client is None and company:
            client = session.execute(
                select(Client).where(Client.company_name == company)
            ).scalars().first()

        if client is None:
            errors.append({"line": line_no, "reason": f"未找到客户：{code or login or company}"})
            continue
        if scope_owner and client.sales_owner != scope_owner:
            errors.append({"line": line_no, "reason": f"无权修改客户 #{client.id}（不在本人名下）"})
            continue

        crm_name = pick(row, "CRM客户", "crm_name")
        crm_code = pick(row, "CRM编号", "crm_code")
        sales = pick(row, "负责销售", "sales_owner")
        ae = pick(row, "负责AE", "ae_owner")
        credit = pick(row, "授信额度", "credit_limit")

        # 先把金额解析完再落字段：解析失败整行跳过，不能半行生效
        credit_value = None
        try:
            if credit:
                credit_value = Decimal(credit.replace(",", ""))
                if credit_value < 0:
                    raise ValueError("授信额度不能为负")
        except (ValueError, ArithmeticError) as exc:
            errors.append({"line": line_no, "reason": f"授信额度无法解析：{exc}"})
            continue

        if company:
            client.company_name = company
        if crm_name:
            client.crm_name = crm_name
        if crm_code:
            client.crm_code = crm_code
        if sales:
            client.sales_owner = operator.display_name if operator.role is Role.SALES else sales
        if ae:
            client.ae_owner = ae
        if credit_value is not None:
            client.credit_limit = money(credit_value)
        updated += 1

    session.flush()
    return {"updated": updated, "errors": errors[:50], "error_count": len(errors)}


CLIENT_EXPORT_COLUMNS = [
    ("id", "ID"),
    ("biz_no", "客户编号"),
    ("login_email", "登录邮箱"),
    ("company_name", "客户名称"),
    ("crm_name", "CRM客户"),
    ("crm_code", "CRM编号"),
    ("sales_owner", "负责销售"),
    ("ae_owner", "负责AE"),
    ("wallet_available", "可用金额"),
    ("wallet_frozen", "冻结金额"),
    ("credit_limit", "授信额度"),
    ("credit_available", "授信余额"),
    ("wallet_balance", "现金余额"),
    ("settle_currency", "币种"),
    ("enabled_label", "启用状态"),
    ("active_label", "活跃状态"),
    ("created_by", "创建人"),
    ("created_at", "创建时间"),
]


def export_clients_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in CLIENT_EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([row.get(key, "") or "" for key, _label in CLIENT_EXPORT_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


def export_clients_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "客户列表"
    ws.append([label for _key, label in CLIENT_EXPORT_COLUMNS])
    for row in rows:
        ws.append([row.get(key, "") or "" for key, _label in CLIENT_EXPORT_COLUMNS])
    for col, width in zip("ABCDEFGHIJKLMNOPQR", (8, 18, 24, 30, 24, 12, 12, 12, 14, 14, 14, 14, 14, 8, 10, 10, 14, 20)):
        ws.column_dimensions[col].width = width
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()

def review(
    session: Session,
    *,
    client_id: int,
    action: str,
    operator: User,
    reason: str | None = None,
) -> Client:
    """执行一步审核。步骤由客户当前状态推导，不接受前端指定。"""
    action = action.upper()
    if action not in ("APPROVE", "REJECT"):
        raise ReviewError("action 只能是 APPROVE / REJECT")

    client = session.get(Client, client_id, with_for_update=True)
    if client is None:
        raise ReviewError(f"客户不存在（id={client_id}）")

    step = CURRENT_STEP.get(client.status)
    if step is None:
        raise StateError(f"当前状态 {client.status} 不在审核流程中，无法审核")

    if operator.role.value not in STEP_ROLE[step]:
        raise ReviewError(
            f"角色 {operator.role.value} 无权执行 {step} 步骤（需 {sorted(STEP_ROLE[step])}）"
        )

    if action == "REJECT" and not (reason and reason.strip()):
        raise ReviewError("驳回必须填写原因")

    from_status = client.status
    if action == "APPROVE":
        to_status = NEXT_ON_APPROVE[from_status]
    else:
        to_status = ClientStatus.REJECTED

    client.status = to_status
    if to_status is ClientStatus.ACTIVE:
        client.activated_at = utcnow()
    if action == "REJECT":
        client.reject_reason = reason

    session.add(
        ClientReview(
            client_id=client.id,
            version=client.version,
            step=step,
            action=action,
            from_status=from_status.value,
            to_status=to_status.value,
            operator=operator.display_name,
            operator_role=operator.role.value,
            reason=reason,
        )
    )
    session.flush()
    return client


def set_terms(
    session: Session,
    *,
    client_id: int,
    operator: User,
    cost_ratio: Decimal | None = None,
    rebate_rate: Decimal | None = None,
    credit_limit: Decimal | None = None,
    payment_terms: int | None = None,
    channel: str | None = None,
) -> Client:
    """商务条款（客户不可见字段）。定额度也在此，是审核最后一步的内容。"""
    client = session.get(Client, client_id, with_for_update=True)
    if client is None:
        raise ReviewError(f"客户不存在（id={client_id}）")

    if cost_ratio is not None:
        if not (0 < Decimal(str(cost_ratio)) <= 1):
            raise ReviewError("成本系数必须在 (0, 1] 区间")
        client.cost_ratio = money(cost_ratio)
    if rebate_rate is not None:
        if not (0 <= Decimal(str(rebate_rate)) < 1):
            raise ReviewError("返点率必须在 [0, 1) 区间")
        client.rebate_rate = money(rebate_rate)
    if credit_limit is not None:
        if Decimal(str(credit_limit)) < 0:
            raise ReviewError("授信额度不能为负")
        client.credit_limit = money(credit_limit)
    if payment_terms is not None:
        if payment_terms < 0:
            raise ReviewError("账期不能为负")
        client.payment_terms = payment_terms
    if channel is not None:
        client.channel = channel

    session.flush()
    return client


def resubmit(session: Session, *, client_id: int, operator: User, changes: dict) -> Client:
    """驳回后客户修改重提：版本 +1，历史申请单保留。"""
    client = session.get(Client, client_id, with_for_update=True)
    if client is None:
        raise ReviewError(f"客户不存在（id={client_id}）")
    if client.status is not ClientStatus.REJECTED:
        raise StateError(f"只有已驳回的客户可以重提（当前 {client.status}）")

    allowed = {
        "company_name", "credit_code", "registered_place", "founded_at",
        "contact_name", "phone", "email", "wechat",
        "platforms", "industry", "target_regions", "monthly_spend_expect",
        "settle_currency", "license_url", "idcard_url", "website",
        "qualification_url", "settle_intent",
    }
    for key, value in (changes or {}).items():
        if key in allowed and value is not None:
            setattr(client, key, value)

    client.version += 1
    client.status = ClientStatus.PENDING_REVIEW
    client.reject_reason = None

    session.add(
        ClientReview(
            client_id=client.id,
            version=client.version,
            step="RESUBMIT",
            action="RESUBMIT",
            from_status=ClientStatus.REJECTED.value,
            to_status=ClientStatus.PENDING_REVIEW.value,
            operator=operator.display_name,
            operator_role=operator.role.value,
            reason="客户修改后重新提交",
        )
    )
    session.flush()
    return client


def review_history(session: Session, client_id: int) -> list[ClientReview]:
    return list(
        session.execute(
            select(ClientReview)
            .where(ClientReview.client_id == client_id)
            .order_by(ClientReview.id.desc())
        ).scalars()
    )


def upsert_application(
    session: Session, *, client_id: int, payload: dict, submit: bool = False
) -> ClientApplication:
    """入驻申请：分步保存草稿，提交后不可改（重提走 resubmit 生成新版本）。"""
    client = session.get(Client, client_id)
    if client is None:
        raise ReviewError(f"客户不存在（id={client_id}）")

    app_obj = session.execute(
        select(ClientApplication)
        .where(ClientApplication.client_id == client_id, ClientApplication.version == client.version)
        .order_by(ClientApplication.id.desc())
    ).scalars().first()

    if app_obj is None:
        app_obj = ClientApplication(
            client_id=client_id,
            version=client.version,
            company_name=payload.get("company_name") or client.company_name,
            is_draft=True,
        )
        session.add(app_obj)

    if not app_obj.is_draft:
        raise StateError("申请已提交，如需修改请等待审核结果或驳回后重提")

    for key in (
        "company_name", "credit_code", "registered_place", "founded_at",
        "contact_name", "contact_title", "phone", "email", "wechat",
        "platforms", "industry", "target_regions", "settle_currency",
        "license_url", "idcard_url", "website", "qualification_url", "settle_intent",
    ):
        if key in payload and payload[key] is not None:
            setattr(app_obj, key, payload[key])
    if "monthly_spend_expect" in payload and payload["monthly_spend_expect"] is not None:
        app_obj.monthly_spend_expect = money(payload["monthly_spend_expect"])

    if submit:
        missing = _missing_required(app_obj)
        if missing:
            raise ReviewError("必填项未完成：" + "、".join(missing))
        app_obj.is_draft = False
        app_obj.submitted_at = utcnow()
        # 同步到客户档案，后台列表立即可见
        for key in (
            "company_name", "credit_code", "registered_place", "founded_at",
            "contact_name", "phone", "email", "wechat",
            "platforms", "industry", "target_regions", "settle_currency",
            "license_url", "idcard_url", "website", "qualification_url", "settle_intent",
        ):
            value = getattr(app_obj, key, None)
            if value is not None and hasattr(client, key):
                setattr(client, key, value)
        client.monthly_spend_expect = app_obj.monthly_spend_expect
        if client.status in (ClientStatus.PENDING_REVIEW, ClientStatus.REJECTED):
            client.status = ClientStatus.PENDING_REVIEW
            client.reject_reason = None

    session.flush()
    return app_obj


_REQUIRED_FIELDS = [
    ("company_name", "公司名称"),
    ("credit_code", "统一社会信用代码"),
    ("registered_place", "注册地"),
    ("founded_at", "成立时间"),
    ("contact_name", "联系人姓名"),
    ("phone", "联系电话"),
    ("email", "联系邮箱"),
    ("platforms", "投放平台"),
    ("industry", "投放行业"),
    ("target_regions", "目标地区"),
    ("settle_currency", "结算币种"),
    ("license_url", "营业执照"),
    ("idcard_url", "法人身份证"),
    ("settle_intent", "结算意向"),
]


def _missing_required(app_obj: ClientApplication) -> list[str]:
    missing = []
    for field, label in _REQUIRED_FIELDS:
        value = getattr(app_obj, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(label)
    return missing
