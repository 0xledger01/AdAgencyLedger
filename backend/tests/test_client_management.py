"""客户管理（P6）：列表字段 / 筛选 / 添加 / 编辑 / 批量 / 重置密码 / 导入导出 / CRM 同步。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.config import settings
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import Client, ClientStatus, OwnerType, Role, User
from backend.app.models.base import money
from backend.app.services import crm
from backend.app.services.auth import create_token
from backend.app.services.ledger import get_or_create_wallet, post


@pytest.fixture()
def env(tmp_path):  # noqa: ANN001, ANN201
    url = f"sqlite:///{(tmp_path / 'clients.db').as_posix()}"
    engine = build_engine(url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    session = factory()
    owner = Client(
        company_name="某某公司", settle_currency="USD", status=ClientStatus.ACTIVE,
        sales_owner="李四", ae_owner="褚十五", crm_name="某某公司", crm_code="H269151",
        created_by="李四", credit_limit=money("5000"), credit_used=money("1200"),
    )
    other = Client(
        company_name="别家客户", settle_currency="USD", status=ClientStatus.ACTIVE,
        sales_owner="陈十四", ae_owner="卫十六", crm_name="别家", crm_code="H269152",
    )
    pending = Client(
        company_name="待审客户", settle_currency="USD", status=ClientStatus.PENDING_REVIEW,
    )
    session.add_all([owner, other, pending])
    session.flush()
    for i, c in enumerate((owner, other, pending), start=1):
        c.biz_no = f"C20261027000{i}"

    wallet = get_or_create_wallet(
        session, owner_type=OwnerType.CLIENT, owner_id=owner.id, currency="USD"
    )
    post(
        session, wallet_id=wallet.id, direction="IN", amount="800", biz_type="TOPUP",
        idempotency_key="test:p6:fund", operator="seed",
    )

    users = {}
    for username, display, role in [
        ("admin", "张三", Role.ADMIN),
        ("sales01", "李四", Role.SALES),
        ("ops01", "钱七", Role.OPS),
        ("finance01", "赵六", Role.FINANCE),
    ]:
        u = User(
            username=username, password_hash="not-used", display_name=display, role=role
        )
        session.add(u)
        users[username] = u
    session.flush()
    tokens = {name: create_token(u) for name, u in users.items()}
    ids = {"owner": owner.id, "other": other.id, "pending": pending.id}
    session.commit()
    session.close()

    def _override():  # noqa: ANN202
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    http = TestClient(app)
    try:
        yield http, tokens, ids, factory
    finally:
        app.dependency_overrides.clear()


def h(tokens, who: str) -> dict:
    return {"Authorization": f"Bearer {tokens[who]}"}


def test_list_rows_carry_management_columns(env):  # noqa: ANN001
    http, tokens, ids, _factory = env
    r = http.get("/api/desk/clients", headers=h(tokens, "admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    row = next(i for i in body["items"] if i["id"] == ids["owner"])
    assert row["crm_code"] == "H269151"
    assert row["ae_owner"] == "褚十五"
    assert row["created_by"] == "李四"
    assert row["wallet_balance"] == "800.0000"
    assert row["wallet_available"] == "800.0000"
    assert row["credit_limit"] == "5000.0000"
    assert row["credit_available"] == "3800.0000"
    assert row["enabled_label"] == "启用"
    assert row["active_label"] == "不活跃"  # 没登录过
    assert row["login_email"] is None


def test_filters_name_codes_owner_ae_enabled(env):  # noqa: ANN001
    http, tokens, ids, _factory = env
    r = http.get("/api/desk/clients?name=某某", headers=h(tokens, "admin"))
    assert [i["id"] for i in r.json()["items"]] == [ids["owner"]]

    # 多个编号：逗号 / 空格混排
    codes = f"C202610270001, C202610270002 H269152"
    r = http.get(f"/api/desk/clients?codes={codes}", headers=h(tokens, "admin"))
    assert {i["id"] for i in r.json()["items"]} == {ids["owner"], ids["other"]}

    r = http.get("/api/desk/clients?ae_owner=卫十六", headers=h(tokens, "admin"))
    assert [i["id"] for i in r.json()["items"]] == [ids["other"]]

    r = http.get("/api/desk/clients?enabled=unopened", headers=h(tokens, "admin"))
    assert [i["id"] for i in r.json()["items"]] == [ids["pending"]]

    r = http.get("/api/desk/clients?enabled=true", headers=h(tokens, "admin"))
    assert ids["pending"] not in {i["id"] for i in r.json()["items"]}

    r = http.get("/api/desk/clients?active=false", headers=h(tokens, "admin"))
    assert len(r.json()["items"]) == 3


def test_sales_scope_and_no_credit(env):  # noqa: ANN001
    http, tokens, ids, _factory = env
    r = http.get("/api/desk/clients", headers=h(tokens, "sales01"))
    assert {i["id"] for i in r.json()["items"]} == {ids["owner"]}

    r = http.post(
        "/api/desk/clients",
        headers=h(tokens, "sales01"),
        json={"company_name": "销售新客", "login_email": "sale-new@example.com",
              "credit_limit": "1000"},
    )
    assert r.status_code == 403, r.text

    # 销售建客：sales_owner 强制为本人
    r = http.post(
        "/api/desk/clients",
        headers=h(tokens, "sales01"),
        json={"company_name": "销售新客", "login_email": "sale-new@example.com"},
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["initial_password"]

    r = http.get(f"/api/desk/clients?name=销售新客", headers=h(tokens, "admin"))
    row = r.json()["items"][0]
    assert row["sales_owner"] == "李四"
    assert row["created_by"] == "李四"
    # 销售建客不能自己启用，必须走三级审核
    assert row["status"] == "PENDING_REVIEW"
    assert row["enabled_label"] == "未开通"


def test_create_login_and_duplicate_email(env):  # noqa: ANN001
    http, tokens, _ids, _factory = env
    body = {
        "company_name": "新建客户", "login_email": "new-client@example.com",
        "crm_name": "新建-CRM", "crm_code": "H269999", "ae_owner": "卫十六",
        "settle_currency": "USD",
    }
    r = http.post("/api/desk/clients", headers=h(tokens, "ops01"), json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["initial_password"]
    assert data["login_email"] == "new-client@example.com"

    # 生成的密码可以登录门户
    r = http.post(
        "/api/auth/portal/login",
        json={"username": "new-client@example.com", "password": data["initial_password"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "CLIENT"

    # 登录邮箱不能重复
    r = http.post("/api/desk/clients", headers=h(tokens, "ops01"), json=body)
    assert r.status_code == 400
    assert "占用" in r.json()["detail"]


def test_default_password_and_email_login(env):  # noqa: ANN001
    """添加客户留空初始密码 = 默认密码；客户用邮箱（大小写/空格随便）就能登录门户。"""
    http, tokens, _ids, _factory = env
    default_pwd = settings.client_default_password
    body = {
        "company_name": "默认密码客户",
        "login_email": " Default.Pass@Example.com ",
        "settle_currency": "USD",
    }
    r = http.post("/api/desk/clients", headers=h(tokens, "ops01"), json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["login_email"] == "default.pass@example.com"
    assert data["initial_password"] == default_pwd

    # 客户手输邮箱的大小写和首尾空格不应该影响登录
    r = http.post(
        "/api/auth/portal/login",
        json={"username": " DEFAULT.PASS@example.com ", "password": default_pwd},
    )
    assert r.status_code == 200, r.text
    assert r.json()["client_id"] == data["id"]

    # 只差大小写的重复邮箱同样要拦住
    r = http.post(
        "/api/desk/clients",
        headers=h(tokens, "ops01"),
        json={"company_name": "撞邮箱客户", "login_email": "default.pass@EXAMPLE.com"},
    )
    assert r.status_code == 400
    assert "占用" in r.json()["detail"]

    # 忘记密码：留空重置 → 回到默认密码，客户仍能登录
    r = http.post(
        f"/api/desk/clients/{data['id']}/reset-password",
        headers=h(tokens, "ops01"),
        json={},
    )
    assert r.status_code == 200, r.text
    assert r.json()["password"] == default_pwd
    r = http.post(
        "/api/auth/portal/login",
        json={"username": "default.pass@example.com", "password": default_pwd},
    )
    assert r.status_code == 200, r.text


def test_edit_batch_and_reset_password(env):  # noqa: ANN001
    http, tokens, ids, factory = env
    r = http.post(
        f"/api/desk/clients/{ids['owner']}/update",
        headers=h(tokens, "ops01"),
        json={"crm_name": "改名后的CRM", "ae_owner": "蒋十七", "login_email": "owner@example.com"},
    )
    assert r.status_code == 200, r.text
    # 原客户没有登录账号：补邮箱时必须回显一次性初始密码
    assert r.json()["initial_password"]
    r = http.post(
        "/api/auth/portal/login",
        json={"username": "owner@example.com", "password": r.json()["initial_password"]},
    )
    assert r.status_code == 200, r.text

    with factory() as s:
        c = s.get(Client, ids["owner"])
        assert c.crm_name == "改名后的CRM"
        assert c.ae_owner == "蒋十七"
        account = s.execute(
            select(User).where(User.client_id == ids["owner"], User.role == Role.CLIENT)
        ).scalar_one()
        assert account.username == "owner@example.com"

    # 重置密码后新密码可登录
    r = http.post(
        f"/api/desk/clients/{ids['owner']}/reset-password",
        headers=h(tokens, "ops01"),
        json={"password": "newpass123"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "owner@example.com"
    r = http.post(
        "/api/auth/portal/login",
        json={"username": "owner@example.com", "password": "newpass123"},
    )
    assert r.status_code == 200, r.text

    # 批量修改：负责 AE + 授信 + 停用
    r = http.post(
        "/api/desk/clients/batch",
        headers=h(tokens, "admin"),
        json={"ids": [ids["owner"], ids["other"]], "ae_owner": "褚十五",
              "credit_limit": "9000", "enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 2
    with factory() as s:
        c = s.get(Client, ids["owner"])
        assert c.ae_owner == "褚十五"
        assert c.credit_limit == money("9000")
        assert c.status is ClientStatus.DISABLED

    # 销售批量改别人的客户 → 拒绝
    r = http.post(
        "/api/desk/clients/batch",
        headers=h(tokens, "sales01"),
        json={"ids": [ids["other"]], "ae_owner": "卫十六"},
    )
    assert r.status_code == 400


def test_import_and_export(env):  # noqa: ANN001
    http, tokens, ids, factory = env
    csv_text = (
        "客户编号,CRM客户,CRM编号,负责AE,授信额度\n"
        f"C202610270001,导入CRM,H269888,卫十六,12000\n"
        "C-NOT-EXIST,无主客户,H000000,卫十六,1\n"
    )
    r = http.post(
        "/api/desk/clients/import",
        headers=h(tokens, "ops01"),
        files={"file": ("clients.csv", csv_text.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["updated"] == 1
    assert result["error_count"] == 1
    with factory() as s:
        c = s.get(Client, ids["owner"])
        assert c.crm_name == "导入CRM"
        assert c.crm_code == "H269888"
        assert c.credit_limit == money("12000")

    r = http.get("/api/desk/clients/export?fmt=csv", headers=h(tokens, "finance01"))
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "登录邮箱" in text and "授信余额" in text
    assert "某某公司" in text

    # 销售只能导出自己名下
    r = http.get("/api/desk/clients/export?fmt=csv", headers=h(tokens, "sales01"))
    assert "别家客户" not in r.content.decode("utf-8-sig")


def test_crm_sync(env, tmp_path, monkeypatch):  # noqa: ANN001
    http, tokens, ids, factory = env
    src = tmp_path / "crm_clients.json"
    src.write_text(
        json.dumps(
            {"clients": [
                {"crm_code": "H269151", "crm_name": "同步后的名字"},
                {"crm_code": "H000404", "crm_name": "匹配不到"},
            ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(crm, "CRM_FILE", src)

    r = http.post("/api/desk/clients/sync", headers=h(tokens, "ops01"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced"] == 1
    assert body["unmatched"] == 1
    with factory() as s:
        assert s.get(Client, ids["owner"]).crm_name == "同步后的名字"

    # 没有配置文件时给明确提示，不假装成功
    monkeypatch.setattr(crm, "CRM_FILE", tmp_path / "missing.json")
    r = http.post("/api/desk/clients/sync", headers=h(tokens, "ops01"))
    assert r.status_code == 200
    assert r.json()["configured"] is False
