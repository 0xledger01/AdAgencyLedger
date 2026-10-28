"""门户开户申请（销售线索）：公开提交、去重、权限收口、状态机。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import Client, ClientLead, ClientLeadStatus, Role, User
from backend.app.services.auth import create_token


@pytest.fixture()
def env(tmp_path):  # noqa: ANN201
    engine = build_engine(f"sqlite:///{(tmp_path / 'leads.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    s = factory()
    users = {}
    for username, display, role in [
        ("sales01", "李四", Role.SALES),
        ("sales02", "王五", Role.SALES),
        ("ops01", "钱七", Role.OPS),
        ("finance01", "赵六", Role.FINANCE),
    ]:
        u = User(
            username=username,
            password_hash="not-used",
            display_name=display,
            role=role,
        )
        s.add(u)
        users[username] = u
    s.flush()
    tokens = {name: create_token(u) for name, u in users.items()}
    s.commit()
    s.close()

    def _override():  # noqa: ANN202
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    http = TestClient(app)
    try:
        yield SimpleEnv(http, factory, tokens)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class SimpleEnv:
    def __init__(self, http: TestClient, factory, tokens: dict) -> None:
        self.client = http
        self.factory = factory
        self.tokens = tokens

    def h(self, who: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def db(self):  # noqa: ANN201
        return self.factory()

    def apply(self, **overrides):  # noqa: ANN201
        payload = {
            "company_name": "某某公司",
            "contact_name": "陈晨",
            "phone": "13800000001",
            "wechat": "chenchen_ad",
            "email": "chen@example.com",
            "platform": "TIKTOK",
            "monthly_budget_expect": 20000,
            "remark": "主投东南亚，希望本周开户",
        }
        payload.update(overrides)
        return self.client.post("/api/public/client-applications", json=payload)


def test_public_application_creates_lead_only(env: SimpleEnv) -> None:
    resp = env.apply()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert "1 个工作日" in data["message"]
    # 公开接口不返回线索 ID / 联系方式，避免被拿来探测手机号是否已提交
    assert "id" not in data and "phone" not in data and "biz_no" not in data

    with env.db() as db:
        lead = db.execute(select(ClientLead)).scalar_one()
        assert lead.company_name == "某某公司"
        assert lead.contact_name == "陈晨"
        assert lead.phone == "13800000001"
        assert lead.status == str(ClientLeadStatus.NEW)
        assert lead.biz_no and lead.biz_no.startswith("LD")

        # 铁律：门户申请只是线索，不创建客户档案 / 登录账号
        assert db.execute(select(func.count()).select_from(Client)).scalar_one() == 0
        assert db.execute(select(func.count()).select_from(User)).scalar_one() == 4


def test_public_application_validation_and_dedupe(env: SimpleEnv) -> None:
    # 三样联系方式都没有：拒绝
    bad = env.apply(phone=None, wechat=None, email=None)
    assert bad.status_code == 400
    assert "至少填写" in bad.json()["detail"]

    # 非 TikTok 平台：当前产品只开 TikTok
    bad_platform = env.apply(platform="FB")
    assert bad_platform.status_code == 400
    assert "TikTok" in bad_platform.json()["detail"]

    # 邮箱格式只做最低限度校验
    bad_email = env.apply(email="not-an-email")
    assert bad_email.status_code == 400

    first = env.apply()
    assert first.status_code == 200
    second = env.apply(company_name="换个公司名也一样")
    assert second.status_code == 200
    with env.db() as db:
        assert db.execute(select(func.count()).select_from(ClientLead)).scalar_one() == 1


def test_desk_lead_permissions_and_sales_scope(env: SimpleEnv) -> None:
    assert env.client.get("/api/desk/client-leads").status_code == 401
    assert env.client.get("/api/desk/client-leads", headers=env.h("finance01")).status_code == 403

    assert env.apply().status_code == 200
    with env.db() as db:
        lead_id = db.execute(select(ClientLead.id)).scalar_one()

    # 销售能看未认领线索
    mine = env.client.get("/api/desk/client-leads", headers=env.h("sales01"))
    assert mine.status_code == 200, mine.text
    data = mine.json()
    assert data["total"] == 1
    assert data["items"][0]["contact_name"] == "陈晨"
    assert data["items"][0]["status_label"] == "待联系"
    assert data["summary"]["new"] == 1

    # 销售认领：状态改已联系，跟进人自动落到当前账号
    claimed = env.client.post(
        f"/api/desk/client-leads/{lead_id}/handle",
        headers=env.h("sales01"),
        json={"status": "CONTACTED", "owner": "李四", "note": "已电话联系"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "CONTACTED"
    assert claimed.json()["owner"] == "李四"
    assert claimed.json()["contacted_at"]

    # 另一个销售看不到已被认领的线索
    other_list = env.client.get("/api/desk/client-leads", headers=env.h("sales02"))
    assert other_list.status_code == 200
    assert other_list.json()["total"] == 0
    assert (
        env.client.get(f"/api/desk/client-leads/{lead_id}", headers=env.h("sales02")).status_code
        == 403
    )
    assert (
        env.client.post(
            f"/api/desk/client-leads/{lead_id}/handle",
            headers=env.h("sales02"),
            json={"status": "CLOSED", "close_reason": "抢单"},
        ).status_code
        == 403
    )

    # 运营可以看全量并转客户
    ops = env.client.get("/api/desk/client-leads", headers=env.h("ops01"))
    assert ops.status_code == 200
    assert ops.json()["total"] == 1
    converted = env.client.post(
        f"/api/desk/client-leads/{lead_id}/handle",
        headers=env.h("ops01"),
        json={"status": "CONVERTED", "owner": "李四", "note": "已在客户管理建档"},
    )
    assert converted.status_code == 200, converted.text
    assert converted.json()["status"] == "CONVERTED"
    assert converted.json()["next_statuses"] == ["CONVERTED"]


def test_lead_state_machine(env: SimpleEnv) -> None:
    assert env.apply().status_code == 200
    with env.db() as db:
        lead_id = db.execute(select(ClientLead.id)).scalar_one()

    # 待联系不能直接跳「已转客户」
    bad = env.client.post(
        f"/api/desk/client-leads/{lead_id}/handle",
        headers=env.h("sales01"),
        json={"status": "CONVERTED"},
    )
    assert bad.status_code == 400
    assert "不能从" in bad.json()["detail"]

    # 关闭必须能留原因；关闭后可重新打开
    closed = env.client.post(
        f"/api/desk/client-leads/{lead_id}/handle",
        headers=env.h("sales01"),
        json={"status": "CLOSED", "close_reason": "号码无效"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["close_reason"] == "号码无效"

    reopened = env.client.post(
        f"/api/desk/client-leads/{lead_id}/handle",
        headers=env.h("sales01"),
        json={"status": "NEW"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "NEW"
    assert reopened.json()["close_reason"] is None


def test_lead_enums_available_to_frontend(env: SimpleEnv) -> None:
    resp = env.client.get("/api/desk/enums", headers=env.h("sales01"))
    assert resp.status_code == 200, resp.text
    labels = {o["code"]: o["label"] for o in resp.json()["client_lead_status"]}
    assert labels["NEW"] == "待联系"
    assert labels["CONTACTED"] == "已联系"
    assert labels["CONVERTED"] == "已转客户"
    assert labels["CLOSED"] == "已关闭"
