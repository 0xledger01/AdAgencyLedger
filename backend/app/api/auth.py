"""认证接口：内部登录 / 门户登录 / 当前身份。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..models.base import utcnow
from ..models.client import Client
from ..models.user import ROLE_LABEL, Role, User
from ..services.auth import (
    burn_password_check,
    create_token,
    verify_password,
)
from ..services.rbac import perm_labels_for, perms_for
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=128)


class LoginOut(BaseModel):
    token: str
    user_id: int
    username: str
    display_name: str
    role: str
    role_label: str
    client_id: int | None
    is_primary: bool
    perms: list[str] = []
    # 权限码 → 中文权限名。界面上只展示中文，权限码留给代码判定。
    perm_labels: dict[str, str] = Field(default_factory=dict)


def _do_login(db, body: LoginIn, *, portal: bool) -> LoginOut:  # noqa: ANN001
    # 门户用邮箱登录：去掉首尾空格并按不区分大小写匹配，
    # 否则客户把邮箱打成 Client@Example.com 就登不进来。
    username = body.username.strip()
    user = db.execute(
        select(User).where(func.lower(User.username) == username.lower())
    ).scalars().first()
    if user is None:
        burn_password_check()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用")

    if portal and user.role is not Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内部员工请使用后台登录入口")
    if not portal and user.role is Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "客户请使用门户登录入口")

    user.last_login_at = utcnow()
    # 客户活跃口径：门户登录即算活跃，客户管理列表的「活跃状态」用它判定
    if user.role is Role.CLIENT and user.client_id:
        client = db.get(Client, user.client_id)
        if client is not None:
            client.last_active_at = user.last_login_at
    db.flush()

    return LoginOut(
        token=create_token(user),
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=str(user.role),
        role_label=ROLE_LABEL.get(user.role, str(user.role)),
        client_id=user.client_id,
        is_primary=user.is_primary,
        perms=perms_for(user),
        perm_labels=perm_labels_for(user),
    )


@router.post("/login", response_model=LoginOut, summary="内部后台登录")
def login(body: LoginIn, db: DbSession) -> LoginOut:
    return _do_login(db, body, portal=False)


@router.post("/portal/login", response_model=LoginOut, summary="客户门户登录")
def portal_login(body: LoginIn, db: DbSession) -> LoginOut:
    return _do_login(db, body, portal=True)


@router.get("/me", response_model=LoginOut, summary="当前登录身份（含权限码）")
def me(user: CurrentUser) -> LoginOut:
    return LoginOut(
        token="",
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=str(user.role),
        role_label=ROLE_LABEL.get(user.role, str(user.role)),
        client_id=user.client_id,
        is_primary=user.is_primary,
        perms=perms_for(user),
        perm_labels=perm_labels_for(user),
    )
