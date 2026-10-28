"""管理端问题工单：权限收口、销售客户范围、状态机、回复可见性。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import AuditLog, Client, ClientStatus, PortalTicket, Role, User
from backend.app.models.base import utcnow
from backend.app.services.auth import create_token


@pytest.fixture()
def env(tmp_path):  # noqa: ANN201
    engine = build_engine(f"sqlite:///{(tmp_path / 'tickets.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    s = factory()
    c1 = Client(company_name="甲客户", status=ClientStatus.ACTIVE, sales_owner="李四", settle_currency="USD")
    c2 = Client(company_name="乙客户", status=ClientStatus.ACTIVE, sales_owner="王五", settle_currency="USD")
    s.add_all([c1, c2])
    s.flush()
    c1.biz_no, c2.biz_no = "C0001", "C0002"
    s.flush()

    now = utcnow()
    t_pending = PortalTicket(
        client_id=c1.id,
        title="账户余额对不上",
        content="后台显示 12,000 USD，媒体后台只剩 9,800 USD。",
        status="PENDING",
        priority="HIGH",
    )
    t_processing = PortalTicket(
        client_id=c1.id,
        title="发票抬头需要修改",
        content="上月对账单抬头写错了。",
        status="PROCESSING",
        priority="NORMAL",
        reply=f"[{now - timedelta(days=1):%Y-%m-%d %H:%M} 赵六]\n已收到，正在处理。",
        handled_by="赵六",
        handled_at=now - timedelta(days=1),
    )
    t_closed = PortalTicket(
        client_id=c1.id,
        title="对账单怎么导出",
        content="没找到导出入口。",
        status="CLOSED",
        priority="NORMAL",
        reply=f"[{now - timedelta(days=3):%Y-%m-%d %H:%M} 张三]\n在「我的钱包」右上角导出。",
        handled_by="张三",
        handled_at=now - timedelta(days=3),
    )
    t_other = PortalTicket(
        client_id=c2.id,
        title="新账户加预算",
        content="act_2001 想加 5,000 USD。",
        status="PENDING",
        priority="LOW",
    )
    s.add_all([t_pending, t_processing, t_closed, t_other])
    s.flush()

    users = {}
    for username, display, role, client_id in [
        ("sales01", "李四", Role.SALES, None),
        ("sales02", "王五", Role.SALES, None),
        ("risk01", "孙七", Role.RISK, None),
        ("ops01", "钱七", Role.OPS, None),
        ("client01", "周九", Role.CLIENT, c1.id),
    ]:
        u = User(
            username=username,
            password_hash="not-used",
            display_name=display,
            role=role,
            client_id=client_id,
        )
        s.add(u)
        users[username] = u
    s.flush()

    tokens = {name: create_token(u) for name, u in users.items()}
    ids = {
        "client1": c1.id,
        "client2": c2.id,
        "ticket_pending": t_pending.id,
        "ticket_processing": t_processing.id,
        "ticket_closed": t_closed.id,
        "ticket_other": t_other.id,
    }
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
        yield SimpleEnv(http, factory, ids, tokens)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class SimpleEnv:
    def __init__(self, http: TestClient, factory, ids: dict, tokens: dict) -> None:
        self.client = http
        self.factory = factory
        self.ids = ids
        self.tokens = tokens

    def h(self, who: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def db(self):  # noqa: ANN201
        return self.factory()

    def list(self, who: str = "ops01", **params):  # noqa: ANN201
        resp = self.client.get("/api/desk/tickets", headers=self.h(who), params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()


def test_customer_and_anonymous_blocked(env: SimpleEnv) -> None:
    assert env.client.get("/api/desk/tickets").status_code == 401
    assert env.client.get("/api/desk/tickets", headers=env.h("client01")).status_code == 403
    assert (
        env.client.post(
            f"/api/desk/tickets/{env.ids['ticket_pending']}/handle",
            headers=env.h("client01"),
            json={"status": "PROCESSING"},
        ).status_code
        == 403
    )


def test_portal_ticket_detail_only_own_client(env: SimpleEnv) -> None:
    tid = env.ids["ticket_pending"]
    resp = env.client.get(f"/api/portal/tickets/{tid}", headers=env.h("client01"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == tid
    assert data["title"] == "账户余额对不上"
    assert "9,800 USD" in data["content"]
    assert data["status_label"] == "待处理"
    # 内部字段不下发
    assert "priority" not in data and "handled_by" not in data

    # 别家客户的工单按不存在处理，不泄露是否存在
    other = env.ids["ticket_other"]
    assert env.client.get(f"/api/portal/tickets/{other}", headers=env.h("client01")).status_code == 404
    assert env.client.get("/api/portal/tickets/999999", headers=env.h("client01")).status_code == 404

    # 未登录 / 内部员工都不能走门户详情
    assert env.client.get(f"/api/portal/tickets/{tid}").status_code == 401
    assert env.client.get(f"/api/portal/tickets/{tid}", headers=env.h("ops01")).status_code == 403


def test_sales_only_sees_own_clients(env: SimpleEnv) -> None:
    mine = env.list("sales01")
    assert mine["total"] == 3
    assert {i["client_id"] for i in mine["items"]} == {env.ids["client1"]}
    # 待处理排在前面
    assert mine["items"][0]["status"] == "PENDING"

    other = env.list("sales02")
    assert other["total"] == 1
    assert other["items"][0]["client_id"] == env.ids["client2"]

    # 销售不能跨客户看详情 / 处理
    tid = env.ids["ticket_other"]
    assert env.client.get(f"/api/desk/tickets/{tid}", headers=env.h("sales01")).status_code == 403
    assert (
        env.client.post(
            f"/api/desk/tickets/{tid}/handle",
            headers=env.h("sales01"),
            json={"status": "PROCESSING"},
        ).status_code
        == 403
    )


def test_risk_can_view_but_not_handle(env: SimpleEnv) -> None:
    assert env.list("risk01")["total"] == 4
    resp = env.client.post(
        f"/api/desk/tickets/{env.ids['ticket_pending']}/handle",
        headers=env.h("risk01"),
        json={"status": "PROCESSING", "reply": "看看"},
    )
    assert resp.status_code == 403


def test_handle_writes_state_reply_and_audit(env: SimpleEnv) -> None:
    tid = env.ids["ticket_pending"]
    resp = env.client.post(
        f"/api/desk/tickets/{tid}/handle",
        headers=env.h("ops01"),
        json={"status": "PROCESSING", "priority": "HIGH", "reply": "正在核对账户余额，今天内给答复。"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "PROCESSING"
    assert data["status_label"] == "处理中"
    assert data["handled_by"] == "钱七"
    assert data["handled_at"]
    assert "正在核对账户余额" in data["reply"]
    assert "PROCESSING" in data["next_statuses"]

    with env.db() as s:
        log = (
            s.query(AuditLog)
            .filter(AuditLog.action == "ticket.handle", AuditLog.doc_id == tid)
            .one()
        )
        assert log.actor == "钱七"
        assert '"PROCESSING"' in (log.after or "")
        assert "处理中" in (log.reason or "")

    # 客户门户能看到中文状态和回复
    portal = env.client.get("/api/portal/tickets", headers=env.h("client01")).json()
    row = next(x for x in portal["items"] if x["id"] == tid)
    assert row["status_label"] == "处理中"
    assert "正在核对账户余额" in row["reply"]


def test_invalid_transition_and_reply_required(env: SimpleEnv) -> None:
    closed = env.ids["ticket_closed"]
    resp = env.client.post(
        f"/api/desk/tickets/{closed}/handle",
        headers=env.h("ops01"),
        json={"status": "PENDING"},
    )
    assert resp.status_code == 400
    assert "不能从" in resp.json()["detail"]

    pending = env.ids["ticket_pending"]
    # 结单必须填回复；该工单还没有任何回复
    resp = env.client.post(
        f"/api/desk/tickets/{pending}/handle",
        headers=env.h("ops01"),
        json={"status": "RESOLVED"},
    )
    assert resp.status_code == 400
    assert "回复" in resp.json()["detail"]

    # 什么都没改
    resp = env.client.post(
        f"/api/desk/tickets/{pending}/handle",
        headers=env.h("ops01"),
        json={},
    )
    assert resp.status_code == 400
    assert "没有需要保存" in resp.json()["detail"]


def test_reply_thread_and_reopen(env: SimpleEnv) -> None:
    tid = env.ids["ticket_processing"]
    resp = env.client.post(
        f"/api/desk/tickets/{tid}/handle",
        headers=env.h("ops01"),
        json={"status": "RESOLVED", "reply": "发票已按新抬头重开，请查收邮箱。"},
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["status"] == "RESOLVED"
    assert "已收到，正在处理。" in first["reply"]
    assert "发票已按新抬头重开" in first["reply"]

    resp = env.client.post(
        f"/api/desk/tickets/{tid}/handle",
        headers=env.h("ops01"),
        json={"status": "PROCESSING", "reply": "客户反馈抬头仍不对，重新核对。"},
    )
    assert resp.status_code == 200, resp.text
    again = resp.json()
    assert again["status"] == "PROCESSING"
    assert again["reply"].count("客户反馈抬头仍不对") == 1
    assert "发票已按新抬头重开" in again["reply"]


def test_filters_and_summary(env: SimpleEnv) -> None:
    data = env.list("ops01", status="PENDING")
    assert data["total"] == 2
    assert {i["status"] for i in data["items"]} == {"PENDING"}
    # 状态计数忽略状态筛选本身
    assert data["summary"]["counts"]["PENDING"] == 2
    assert data["summary"]["counts"]["PROCESSING"] == 1
    assert data["summary"]["counts"]["CLOSED"] == 1

    assert env.list("ops01", keyword="发票")["total"] == 1
    assert env.list("ops01", keyword=f"#{env.ids['ticket_pending']}")["total"] == 1
    assert env.list("ops01", client="乙客户")["total"] == 1
    assert env.list("ops01", client="C0001")["total"] == 3
    assert env.list("ops01", priority="HIGH")["total"] == 1

    page = env.list("ops01", limit=2, offset=0)
    assert page["total"] == 4 and len(page["items"]) == 2
    page2 = env.list("ops01", limit=2, offset=2)
    assert len(page2["items"]) == 2
    assert {i["id"] for i in page["items"]}.isdisjoint({i["id"] for i in page2["items"]})

    bad = env.client.get("/api/desk/tickets", headers=env.h("ops01"), params={"status": "OPEN"})
    assert bad.status_code == 400


def test_detail_has_client_and_next_statuses(env: SimpleEnv) -> None:
    resp = env.client.get(
        f"/api/desk/tickets/{env.ids['ticket_pending']}", headers=env.h("ops01")
    )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["client"]["company_name"] == "甲客户"
    assert d["client"]["sales_owner"] == "李四"
    assert d["next_statuses"] == ["PENDING", "PROCESSING", "RESOLVED", "CLOSED"]
    assert "9,800" in d["content"]
