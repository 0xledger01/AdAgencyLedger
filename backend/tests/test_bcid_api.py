"""BC 管理接口自检：历史归纳 / 手工添加 / 默认项 / 删除 / 权限收口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import (
    AdAccount,
    Client,
    ClientBcid,
    ClientStatus,
    OpenRequest,
    OpenRequestStatus,
    Platform,
    Role,
    User,
)
from backend.app.models.base import money
from backend.app.services.auth import create_token


class Env:
    def __init__(self, client: TestClient, factory, ids: dict, tokens: dict) -> None:
        self.client = client
        self.factory = factory
        self.ids = ids
        self.tokens = tokens

    def h(self, who: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def bcids(self, who: str = "admin", query: str = "") -> dict:
        resp = self.client.get(
            f"/api/desk/clients/{self.ids['client_id']}/bcids{query}", headers=self.h(who)
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def rows(self) -> list[ClientBcid]:
        with self.factory() as s:
            return list(
                s.execute(
                    select(ClientBcid)
                    .where(ClientBcid.client_id == self.ids["client_id"])
                    .order_by(ClientBcid.id)
                ).scalars()
            )


@pytest.fixture()
def env(tmp_path):  # noqa: ANN201
    url = f"sqlite:///{(tmp_path / 'bcid.db').as_posix()}"
    engine = build_engine(url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    session = factory()
    client = Client(
        company_name="BC 测试客户",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        sales_owner="李四",
    )
    other = Client(
        company_name="别家客户",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        sales_owner="陈十四",
    )
    session.add_all([client, other])
    session.flush()
    client.biz_no = f"C{client.id:04d}"
    other.biz_no = f"C{other.id:04d}"

    session.add(
        AdAccount(
            account_id="act_bc_1001",
            platform=Platform.TIKTOK,
            currency="USD",
            client_id=client.id,
            bm_id="710141103420000009",
            balance=money("0"),
            daily_limit=money("1000"),
        )
    )
    session.add(
        OpenRequest(
            client_id=client.id,
            platform=Platform.TIKTOK,
            bm_id="710141103420000010",
            bc_ids="710141103420000009, 710141103420000011",
            currency="USD",
            status=OpenRequestStatus.PENDING_APPROVAL,
            subject=client.company_name,
        )
    )

    users = {}
    for username, display, role, cid in [
        ("admin", "张三", Role.ADMIN, None),
        ("sales01", "李四", Role.SALES, None),
        ("sales02", "陈十四", Role.SALES, None),
        ("risk01", "王五", Role.RISK, None),
        ("client01", "周九", Role.CLIENT, client.id),
    ]:
        u = User(
            username=username,
            password_hash="not-used",
            display_name=display,
            role=role,
            client_id=cid,
        )
        session.add(u)
        users[username] = u
    session.flush()

    tokens = {name: create_token(u) for name, u in users.items()}
    ids = {"client_id": client.id, "other_client_id": other.id}
    session.commit()
    session.close()

    def _override():  # noqa: ANN202
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    api_client = TestClient(app)
    try:
        yield Env(api_client, factory, ids, tokens)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_history_is_derived_from_accounts_and_open_requests(env: Env) -> None:
    data = env.bcids()
    assert data["client"]["id"] == env.ids["client_id"]
    assert data["total"] == 3
    by_bcid = {item["bcid"]: item for item in data["items"]}
    assert set(by_bcid) == {
        "710141103420000009",
        "710141103420000010",
        "710141103420000011",
    }
    assert all(item["source"] == "HISTORY" for item in data["items"])
    assert all(item["source_label"] == "历史绑定" for item in data["items"])
    assert data["summary"] == {"manual": 0, "history": 3, "default_bcid": None}

    # 再读一次不产生重复行（幂等对账）
    again = env.bcids()
    assert again["total"] == 3


def test_manual_add_promotes_history_and_dedupes(env: Env) -> None:
    # 新号：手工添加，直接设为默认
    resp = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "765557932675017934", "set_default": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["item"]["source"] == "MANUAL"
    assert body["item"]["source_label"] == "手工添加"
    assert body["item"]["is_default"] is True
    assert body["summary"]["default_bcid"] == "765557932675017934"

    # 同一个号再加一次：不新增行，标记为手工添加
    again = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "765557932675017934"},
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["deduped"] is True
    assert len([r for r in env.rows() if r.bcid == "765557932675017934"]) == 1

    # 历史号手工添加 → 升级为 MANUAL，不重复插入
    env.bcids()  # 先读一次列表，让历史绑定落库
    promoted = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "710141103420000009"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["created"] is False
    assert promoted.json()["item"]["source"] == "MANUAL"
    assert len([r for r in env.rows() if r.bcid == "710141103420000009"]) == 1

    # 列表里默认项置顶
    data = env.bcids()
    assert data["items"][0]["bcid"] == "765557932675017934"
    assert data["items"][0]["is_default"] is True
    assert data["summary"]["manual"] == 2
    assert data["summary"]["history"] == 2


def test_only_one_default_and_portal_preselects_it(env: Env) -> None:
    first = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "710141103420000011", "set_default": True},
    ).json()["item"]
    second = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "710141103420000010"},
    ).json()["item"]

    resp = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids/{second['id']}/default",
        headers=env.h("admin"),
        json={"is_default": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["is_default"] is True

    with env.factory() as s:
        defaults = s.execute(
            select(ClientBcid.bcid).where(
                ClientBcid.client_id == env.ids["client_id"],
                ClientBcid.is_default.is_(True),
            )
        ).scalars().all()
    assert defaults == ["710141103420000010"]

    # 门户开户页：选项默认项置顶，default_bc_ids 就是它
    page = env.client.get("/api/portal/open-requests", headers=env.h("client01"))
    assert page.status_code == 200, page.text
    payload = page.json()
    assert payload["bc_options"][0] == "710141103420000010"
    assert payload["default_bc_ids"] == ["710141103420000010"]

    # 取消默认：门户回退到第一个选项（老行为）
    off = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids/{second['id']}/default",
        headers=env.h("admin"),
        json={"is_default": False},
    )
    assert off.status_code == 200
    assert off.json()["summary"]["default_bcid"] is None
    page2 = env.client.get("/api/portal/open-requests", headers=env.h("client01")).json()
    assert page2["default_bc_ids"] == page2["bc_options"][:1]
    assert first["bcid"] in page2["bc_options"]


def test_delete_binding_leaves_accounts_untouched(env: Env) -> None:
    data = env.bcids()
    target = next(i for i in data["items"] if i["bcid"] == "710141103420000010")
    resp = env.client.delete(
        f"/api/desk/clients/{env.ids['client_id']}/bcids/{target['id']}",
        headers=env.h("admin"),
    )
    assert resp.status_code == 200, resp.text
    # 墓碑行保留在库里（可追溯），但对页面不可见
    tomb = next(r for r in env.rows() if r.bcid == "710141103420000010")
    assert tomb.is_deleted is True
    assert tomb.is_default is False
    with env.factory() as s:
        order = s.execute(
            select(OpenRequest).where(OpenRequest.client_id == env.ids["client_id"])
        ).scalars().first()
        assert order.bm_id == "710141103420000010"  # 开户单不动
        account = s.execute(
            select(AdAccount).where(AdAccount.account_id == "act_bc_1001")
        ).scalars().first()
        assert account.bm_id == "710141103420000009"  # 广告账户不动

    # 删除后重新读取列表：不会被历史归纳补回来（这是删除按钮的意义）
    after = env.bcids()
    assert all(i["bcid"] != "710141103420000010" for i in after["items"])
    assert after["summary"]["history"] == 2
    # 刷新多次也一样稳定
    assert all(i["bcid"] != "710141103420000010" for i in env.bcids()["items"])
    # 开户页选项同样排除已删除项
    portal = env.client.get("/api/portal/open-requests", headers=env.h("client01"))
    assert portal.status_code == 200, portal.text
    assert "710141103420000010" not in portal.json()["bc_options"]

    # 新的历史号仍然能自动归纳进来，墓碑只挡住被删的那一个
    with env.factory() as s:
        s.add(
            AdAccount(
                account_id="act_bc_1002",
                platform=Platform.TIKTOK,
                currency="USD",
                client_id=env.ids["client_id"],
                bm_id="710141103420000012",
                balance=money("0"),
                daily_limit=money("1000"),
            )
        )
        s.commit()
    grown = env.bcids()
    assert any(i["bcid"] == "710141103420000012" for i in grown["items"])
    assert all(i["bcid"] != "710141103420000010" for i in grown["items"])

    # 手工重新添加 = 复活同一行，不是新增重复行
    revived = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "710141103420000010"},
    )
    assert revived.status_code == 200, revived.text
    body = revived.json()
    assert body["created"] is False
    assert body["revived"] is True
    assert body["deduped"] is False
    assert body["item"]["is_default"] is False
    rows = [r for r in env.rows() if r.bcid == "710141103420000010"]
    assert len(rows) == 1 and rows[0].is_deleted is False
    assert any(i["bcid"] == "710141103420000010" for i in env.bcids()["items"])


def test_permissions_and_validation(env: Env) -> None:
    # 销售只能看自己名下客户
    assert env.client.get(
        f"/api/desk/clients/{env.ids['client_id']}/bcids", headers=env.h("sales01")
    ).status_code == 200
    assert env.client.get(
        f"/api/desk/clients/{env.ids['client_id']}/bcids", headers=env.h("sales02")
    ).status_code == 403

    # 风控可看不可改
    add_body = {"bcid": "710141103420000099"}
    assert env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("risk01"),
        json=add_body,
    ).status_code == 403

    # 客户账号不得进内部接口
    assert env.client.get(
        f"/api/desk/clients/{env.ids['client_id']}/bcids", headers=env.h("client01")
    ).status_code == 403

    # 非法 BCID
    bad = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/bcids",
        headers=env.h("admin"),
        json={"bcid": "有中文的编号"},
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "STATE_ERROR"

    # 不存在的客户
    assert env.client.get(
        "/api/desk/clients/999999/bcids", headers=env.h("admin")
    ).status_code == 404
