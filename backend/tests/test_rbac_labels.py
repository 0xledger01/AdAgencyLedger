"""权限中文名：每个权限码都要有且只有一条中文，界面上不再露英文码。"""

from __future__ import annotations

import pytest

from backend.app.models.user import Role, User
from backend.app.services.auth import PermissionDenied
from backend.app.services.rbac import (
    MATRIX,
    PERM_LABEL,
    Perm,
    perm_labels_for,
    perm_name,
    perm_title,
    perms_for,
    require,
)


def make_user(role: Role, active: bool = True) -> User:
    return User(
        username=f"u_{role}".lower(),
        password_hash="x",
        display_name="测试",
        role=role,
        active=active,
    )


def test_every_perm_has_chinese_label() -> None:
    missing = [p.value for p in Perm if p not in PERM_LABEL]
    assert missing == []
    assert set(PERM_LABEL) == set(Perm)


def test_labels_are_chinese_and_unique() -> None:
    labels = list(PERM_LABEL.values())
    assert len(labels) == len(set(labels)), "同一句中文权限名不能挂在两个权限码上"
    for perm, label in PERM_LABEL.items():
        assert label.strip() == label and label, f"{perm} 中文名为空"
        assert not any("a" <= ch.lower() <= "z" for ch in label), (
            f"{perm} 的中文名里混了英文：{label}"
        )


def test_perm_labels_follow_same_matrix_as_perms() -> None:
    for role in Role:
        user = make_user(role)
        codes = perms_for(user)
        labels = perm_labels_for(user)
        assert list(labels) == codes
        assert set(labels.values()) == {PERM_LABEL[Perm(c)] for c in codes}


def test_inactive_user_has_no_perm_labels() -> None:
    assert perm_labels_for(make_user(Role.ADMIN, active=False)) == {}
    assert perms_for(make_user(Role.ADMIN, active=False)) == []


def test_perm_name_and_title_degrade_gracefully() -> None:
    assert perm_name(Perm.RECHARGE_EXEC) == "执行加款"
    assert perm_title(Perm.RECHARGE_EXEC) == "执行加款（recharge.exec）"
    assert perm_name("unknown.perm") == "unknown.perm"


def test_permission_denied_message_is_chinese_first() -> None:
    user = make_user(Role.FINANCE)
    with pytest.raises(PermissionDenied) as exc:
        require(user, Perm.RECHARGE_EXEC)
    msg = str(exc.value)
    assert "财务" in msg
    assert "执行加款" in msg
    assert "recharge.exec" in msg


def test_matrix_only_uses_declared_perms() -> None:
    # 反向兜底：矩阵里出现未登记的权限码时，中文名与前端都会缺一块。
    assert set(MATRIX) == set(Perm)
