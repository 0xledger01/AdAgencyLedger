"""API 依赖：当前用户、权限校验。"""

from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models.user import Role, User
from ..services.auth import AuthError, decode_token
from ..services.errors import LedgerError
from ..services.rbac import Perm, require


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少登录凭证")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    user = db.get(User, int(payload["sub"]))
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_perm(perm: Perm):  # noqa: ANN201
    def _dep(user: CurrentUser) -> User:
        try:
            require(user, perm)
        except LedgerError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        return user

    return Depends(_dep)


def require_desk(user: CurrentUser) -> User:
    """客户角色不得访问 /desk 任何接口。"""
    if user.role is Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "客户账号无权访问内部后台")
    return user


DeskUser = Annotated[User, Depends(require_desk)]


def require_portal(user: CurrentUser) -> User:
    if user.role is not Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该接口仅供客户门户使用")
    if user.client_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号未绑定客户档案")
    return user


PortalUser = Annotated[User, Depends(require_portal)]
