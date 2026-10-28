"""客户门户 BC 管理：只看自己、默认预选、删除墓碑、越权隔离。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import (
    AdAccount,
    Client,
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
    def __init__(self, client: TestClient, ids: dict, tokens: dict) -> None:
        self.client = client
        self.ids = ids
        self.tokens = tokens

    def h(self, who: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def list(self, who: str = "c1", query: str = "") -> dict:
        resp = self.client.get(f"/api/portal/bcids{query}", headers=self.h(who))
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture()
def env(tmp_path):  # noqa: ANN201
    url = f"sqlite:///{(tmp_path / 'portal_bcid.db').as_posix()}"
    engine = build_engine(url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    session = factory()
    c1 = Client(
        company_name="门户 BC 客户",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        sales_owner="李四",
    )
    c2 = Client(
        company_name="另一家客户",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        sales_owner="陈十四",
    )
    session.add_all([c1, c2])
    session.flush()
    c1.biz_no = f"C{c1.id:04d}"
    c2.biz_no = f"C{c2.id:04d}"

    session.add(
        AdAccount(
            account_id="act_portal_bc_1",
            platform=Platform.TIKTOK,
            currency="USD",
            client_id=c1.id,
            bm_id="710141103420000109",
            balance=money("0"),
            daily_limit=money("1000"),
        )
    )
    session.add(
        OpenRequest(
            client_id=c1.id,
            platform=Platform.TIKTOK,
            bm_id="710141103420000110",
            bc_ids="710141103420000109, 710141103420000111",
            currency="USD",
            status=OpenRequestStatus.PENDING_APPROVAL,
            subject=c1.company_name,
        )
    )
    session.add(
        OpenRequest(
            client_id=c2.id,
            platform=Platform.TIKTOK,
            bm_id="710141103420000209",
            currency="USD",
            status=OpenRequestStatus.PENDING_APPROVAL,
            subject=c2.company_name,
        )
    )

    users = {}
    for username, display, role, cid in [
        ("c1", "客户一", Role.CLIENT, c1.id),
        ("c2", "客户二", Role.CLIENT, c2.id),
        ("admin", "张三", Role.ADMIN, None),
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
    ids = {"c1": c1.id, "c2": c2.id}
    session.commit()
    session.close()

    def _override():  # noqa: ANN202
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    api = TestClient(app)
    try:
        yield Env(api, ids, tokens)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_list_only_own_history(env: Env) -> None:
    data = env.list("c1")
    assert data["total"] == 3
    assert all(i["source"] == "HISTORY" for i in data["items"])
    assert {i["bcid"] for i in data["items"]} == {
        "710141103420000109",
        "710141103420000110",
        "710141103420000111",
    }
    assert data["summary"] == {"manual": 0, "history": 3, "default_bcid": None}

    # client_id 不在 URL 上：即便硬塞 query 也不生效
    other = env.list("c1", f"?client_id={env.ids['c2']}&limit=200")
    assert other["total"] == 3
    assert all(i["bcid"].startswith("7101411034200001") for i in other["items"])

    mine = env.list("c2")
    assert mine["total"] == 1
    assert mine["items"][0]["bcid"] == "710141103420000209"


def test_add_default_and_portal_preselect(env: Env) -> None:
    resp = env.client.post(
        "/api/portal/bcids",
        headers=env.h("c1"),
        json={"bcid": "765557932675017934", "set_default": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["item"]["source"] == "MANUAL"
    assert body["item"]["is_default"] is True
    assert body["summary"]["default_bcid"] == "765557932675017934"

    # 重复添加不产生第二行
    again = env.client.post(
        "/api/portal/bcids",
        headers=env.h("c1"),
        json={"bcid": "765557932675017934"},
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["deduped"] is True

    # 开户申请页自动预选默认项
    page = env.client.get("/api/portal/open-requests", headers=env.h("c1"))
    assert page.status_code == 200, page.text
    payload = page.json()
    assert payload["bc_options"][0] == "765557932675017934"
    assert payload["default_bc_ids"] == ["765557932675017934"]

    # 取消默认后回退到第一个选项
    row = body["item"]
    off = env.client.post(
        f"/api/portal/bcids/{row['id']}/default",
        headers=env.h("c1"),
        json={"is_default": False},
    )
    assert off.status_code == 200
    assert off.json()["summary"]["default_bcid"] is None
    page2 = env.client.get("/api/portal/open-requests", headers=env.h("c1")).json()
    assert page2["default_bc_ids"] == page2["bc_options"][:1]


def test_delete_is_sticky_and_readd_revives(env: Env) -> None:
    data = env.list("c1")
    target = next(i for i in data["items"] if i["bcid"] == "710141103420000110")
    resp = env.client.delete(f"/api/portal/bcids/{target['id']}", headers=env.h("c1"))
    assert resp.status_code == 200, resp.text

    after = env.list("c1")
    assert all(i["bcid"] != "710141103420000110" for i in after["items"])
    assert after["summary"]["history"] == 2
    assert all(i["bcid"] != "710141103420000110" for i in env.list("c1")["items"])

    # 手工重新添加复活同一行
    revived = env.client.post(
        "/api/portal/bcids",
        headers=env.h("c1"),
        json={"bcid": "710141103420000110"},
    )
    assert revived.status_code == 200, revived.text
    assert revived.json()["revived"] is True
    assert revived.json()["created"] is False
    assert any(i["bcid"] == "710141103420000110" for i in env.list("c1")["items"])


def test_cannot_touch_other_clients_rows(env: Env) -> None:
    row = env.list("c1")["items"][0]
    for method, path, body in [
        ("post", f"/api/portal/bcids/{row['id']}/default", {"is_default": True}),
        ("delete", f"/api/portal/bcids/{row['id']}", None),
    ]:
        resp = getattr(env.client, method)(
            path,
            headers=env.h("c2"),
            **({"json": body} if body is not None else {}),
        )
        assert resp.status_code == 404, resp.text

    # 别家客户的列表里看不到这两条
    other = env.list("c2")
    assert all(i["bcid"] != row["bcid"] for i in other["items"])


def test_identity_and_validation(env: Env) -> None:
    # 未登录
    assert env.client.get("/api/portal/bcids").status_code == 401
    # 内部账号不得走门户接口
    assert (
        env.client.get("/api/portal/bcids", headers=env.h("admin")).status_code == 403
    )
    # 客户不得走内部后台的 BC 接口
    assert (
        env.client.get(
            f"/api/desk/clients/{env.ids['c1']}/bcids", headers=env.h("c1")
        ).status_code
        == 403
    )
    # 非法 BCID
    bad = env.client.post(
        "/api/portal/bcids", headers=env.h("c1"), json={"bcid": "有中文"}
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "STATE_ERROR"
