"""认证：bcrypt 密码 + JWT。

铁律 3：客户身份只从登录态反查，绝不接受前端传入 client_id。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from ..config import settings
from ..models.user import INTERNAL_ROLES, Role, User
from .errors import LedgerError


class AuthError(LedgerError):
    code = "AUTH_ERROR"


class PermissionDenied(LedgerError):
    code = "PERMISSION_DENIED"


_PLACEHOLDER_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=4)).decode()


def hash_password(plain: str) -> str:
    if not plain or len(plain) < 6:
        raise AuthError("密码至少 6 位")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def burn_password_check() -> None:
    """用户名不存在时也走一次哈希，避免时序差异泄露账号是否存在。"""
    bcrypt.checkpw(b"dummy", _PLACEHOLDER_HASH.encode())


def create_token(user: User, *, ttl_hours: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "usr": user.username,
        "role": str(user.role),
        "cid": user.client_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ttl_hours or settings.jwt_ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("登录凭证无效") from exc


def is_internal(user: User) -> bool:
    return user.role in INTERNAL_ROLES


def is_client(user: User) -> bool:
    return user.role is Role.CLIENT


def ensure_client_scope(user: User, client_id: int | None) -> int:
    """客户角色只能看自己；内部角色可指定 client_id。

    客户角色传入任何 client_id 都会被服务端强制覆盖为登录态里的值（铁律 3）。
    """
    if is_client(user):
        if user.client_id is None:
            raise PermissionDenied("客户账号未绑定客户档案")
        return user.client_id
    if client_id is None:
        raise AuthError("缺少 client_id")
    return client_id


def new_secret_key() -> str:
    return secrets.token_urlsafe(48)
