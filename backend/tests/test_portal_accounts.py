"""客户端「广告账户管理」：列表筛选 / 详情隔离 / 发起充值（冻结）与幂等。"""

from __future__ import annotations

import json
from datetime import date, timedelta

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
    ClientStatus,
    OwnerType,
    RechargeOrder,
    Role,
    SpendDaily,
    User,
)
from backend.app.models.base import money, utcnow
from backend.app.models.docs import OpenRequest
from backend.app.models.enums import (
    AdAccountStatus,
    Direction,
    OpenRequestStatus,
    Platform,
    RechargeStatus,
    BizType,
)
from backend.app.services.auth import create_token
from backend.app.services.ledger import get_or_create_wallet, post


@pytest.fixture()
def env(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'portal-accounts.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    session = factory()

    def add_client(name: str, status: ClientStatus = ClientStatus.ACTIVE) -> Client:
        c = Client(company_name=name, settle_currency="USD", status=status)
        session.add(c)
        session.flush()
        c.biz_no = f"C{c.id:04d}"
        session.flush()
        return c

    owner = add_client("某某公司")
    other = add_client("某某公司")

    wallet = get_or_create_wallet(
        session, owner_type=OwnerType.CLIENT, owner_id=owner.id, currency="USD"
    )
    post(
        session,
        wallet_id=wallet.id,
        direction=Direction.IN,
        amount="10000",
        biz_type=BizType.TOPUP,
        idempotency_key="test:portal:accounts:1",
        biz_no="TP-ACCOUNT-1",
        operator="seed",
        remark="期初充值",
    )

    tiktok = AdAccount(
        account_id="act_portal_1001",
        platform=Platform.TIKTOK,
        currency="USD",
        client_id=owner.id,
        channel="Yatop",
        balance=money("1200.50"),
        daily_limit=money("500"),
        timezone="Asia/Shanghai",
        status=AdAccountStatus.NORMAL,
        opened_at=utcnow() - timedelta(days=3),
    )
    legacy = AdAccount(
        account_id="act_portal_1002",
        platform=Platform.FB,
        currency="USD",
        client_id=owner.id,
        channel="山恒",
        balance=money("300"),
        daily_limit=money("100"),
        timezone="America/New_York",
        status=AdAccountStatus.LIMITED,
        opened_at=utcnow() - timedelta(days=40),
    )
    banned = AdAccount(
        account_id="act_portal_1003",
        platform=Platform.TIKTOK,
        currency="USD",
        client_id=owner.id,
        balance=money("0"),
        daily_limit=money("0"),
        status=AdAccountStatus.BANNED,
        opened_at=utcnow(),
    )
    foreign = AdAccount(
        account_id="act_other_9001",
        platform=Platform.TIKTOK,
        currency="USD",
        client_id=other.id,
        balance=money("999"),
        daily_limit=money("999"),
        status=AdAccountStatus.NORMAL,
        opened_at=utcnow(),
    )
    session.add_all([tiktok, legacy, banned, foreign])
    session.flush()

    # 开户单回执里挂账户：列表的「开户主体 / 备注 / 开户单号」靠它带出
    order = OpenRequest(
        client_id=owner.id,
        platform=Platform.TIKTOK,
        subject="邵东市大禾系康百货商行(个体工商户)",
        status=OpenRequestStatus.OPENED,
        remark="门户提交：需要东南亚投放",
        agent_receipt=json.dumps(
            {
                "result": "OPENED",
                "ad_account_ids": [tiktok.id],
                "accounts": [tiktok.account_id],
            }
        ),
    )
    session.add(order)
    session.flush()
    order.biz_no = f"OR{utcnow():%Y%m%d}{order.id:05d}"

    # 消耗明细：详情页近 30 天
    session.add_all(
        [
            SpendDaily(
                date=date.today() - timedelta(days=1),
                ad_account_id=tiktok.id,
                spend=money("12.5"),
                currency="USD",
                base_spend=money("12.5"),
            ),
            SpendDaily(
                date=date.today() - timedelta(days=2),
                ad_account_id=tiktok.id,
                spend=money("7.5"),
                currency="USD",
                base_spend=money("7.5"),
            ),
        ]
    )

    user = User(
        username="portal-acc",
        password_hash="not-used",
        display_name="周九",
        role=Role.CLIENT,
        client_id=owner.id,
    )
    session.add(user)
    session.flush()
    token = create_token(user)
    ids = {
        "owner_id": owner.id,
        "other_id": other.id,
        "wallet_id": wallet.id,
        "tiktok_id": tiktok.id,
        "legacy_id": legacy.id,
        "banned_id": banned.id,
        "foreign_id": foreign.id,
    }
    session.commit()
    session.close()

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    api = TestClient(app)
    try:
        yield api, factory, ids, token
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_list_only_own_accounts_and_hides_internal_fields(env):
    api, _factory, ids, token = env
    r = api.get("/api/portal/ad-accounts", headers=h(token), params={"limit": 50})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 3
    account_ids = {it["account_id"] for it in data["items"]}
    assert account_ids == {"act_portal_1001", "act_portal_1002", "act_portal_1003"}
    assert "act_other_9001" not in account_ids

    first = next(it for it in data["items"] if it["account_id"] == "act_portal_1001")
    assert first["subject"] == "邵东市大禾系康百货商行(个体工商户)"
    assert first["open_request_biz_no"].startswith("OR")
    assert first["remark"] == "门户提交：需要东南亚投放"
    assert first["status_label"] == "正常"
    assert first["platform_label"] == "TikTok"
    assert first["bc_ids"] == []

    # 客户侧白名单：渠道 / 返点 / 成本系数不能出现
    raw = r.text
    for word in ("channel", "rebate", "rebate_rate", "cost_ratio", "返点", "代理商"):
        assert word not in raw

    assert data["can_recharge"] is True
    assert any(w["currency"] == "USD" and w["available"] == "10000.0000" for w in data["wallets"])
    assert [o["code"] for o in data["options"]["platform"]][:1] == ["TIKTOK"]
    assert {o["code"] for o in data["options"]["status"]} == {"NORMAL", "LIMITED", "BANNED"}


def test_list_filters(env):
    api, _factory, _ids, token = env
    # 账户 ID 模糊
    r = api.get("/api/portal/ad-accounts", headers=h(token), params={"keyword": "1002"})
    assert [it["account_id"] for it in r.json()["items"]] == ["act_portal_1002"]

    # 开户主体模糊
    r = api.get(
        "/api/portal/ad-accounts", headers=h(token), params={"subject": "大禾系康"}
    )
    assert [it["account_id"] for it in r.json()["items"]] == ["act_portal_1001"]

    # 媒体 / 状态
    r = api.get("/api/portal/ad-accounts", headers=h(token), params={"platform": "FB"})
    assert [it["account_id"] for it in r.json()["items"]] == ["act_portal_1002"]
    r = api.get("/api/portal/ad-accounts", headers=h(token), params={"status": "BANNED"})
    assert [it["account_id"] for it in r.json()["items"]] == ["act_portal_1003"]

    # 开户日期窗口：40 天前那台落在窗口外
    today = date.today()
    r = api.get(
        "/api/portal/ad-accounts",
        headers=h(token),
        params={
            "date_from": (today - timedelta(days=5)).isoformat(),
            "date_to": today.isoformat(),
        },
    )
    assert [it["account_id"] for it in r.json()["items"]] == [
        "act_portal_1003",
        "act_portal_1001",
    ]

    # 非法取值按 400 处理，不静默返回全量
    bad = api.get("/api/portal/ad-accounts", headers=h(token), params={"status": "NOPE"})
    assert bad.status_code == 400


def test_detail_is_scoped_to_owner(env):
    api, _factory, ids, token = env
    ok = api.get(f"/api/portal/ad-accounts/{ids['tiktok_id']}", headers=h(token))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["account"]["account_id"] == "act_portal_1001"
    assert body["wallet"]["available"] == "10000.0000"
    assert body["spend"]["total"] == "20.0000"
    assert len(body["spend"]["points"]) == 2
    assert body["recharges"]["total"] == 0
    assert body["can_recharge"] is True

    denied = api.get(f"/api/portal/ad-accounts/{ids['foreign_id']}", headers=h(token))
    assert denied.status_code == 404
    denied_post = api.post(
        f"/api/portal/ad-accounts/{ids['foreign_id']}/recharge",
        headers=h(token),
        json={"amount": "10", "request_no": "portal-test-foreign"},
    )
    assert denied_post.status_code == 404


def test_recharge_freezes_wallet_and_is_idempotent(env):
    api, factory, ids, token = env
    payload = {"amount": "500.00", "request_no": "portal-test-recharge-1"}
    r = api.post(
        f"/api/portal/ad-accounts/{ids['tiktok_id']}/recharge",
        headers=h(token),
        json=payload,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PENDING_EXEC"
    assert body["status_label"] == "待执行"
    assert body["deduped"] is False
    assert body["wallet_available"] == "9500.0000"

    # 同幂等键再点一次：返回原单，不重复冻结
    again = api.post(
        f"/api/portal/ad-accounts/{ids['tiktok_id']}/recharge",
        headers=h(token),
        json=payload,
    )
    assert again.status_code == 200
    assert again.json()["deduped"] is True
    assert again.json()["biz_no"] == body["biz_no"]
    assert again.json()["wallet_available"] == "9500.0000"

    with factory() as db:
        orders = db.execute(select(RechargeOrder)).scalars().all()
        assert len(orders) == 1
        assert orders[0].status is RechargeStatus.PENDING_EXEC
        assert money(orders[0].amount) == money("500")
        wallet = get_or_create_wallet(
            db, owner_type=OwnerType.CLIENT, owner_id=ids["owner_id"], currency="USD",
            create=False,
        )
        assert money(wallet.frozen) == money("500")
        # 余额没动：只冻结，没出账（铁律 6）
        assert money(wallet.balance) == money("10000")

    detail = api.get(f"/api/portal/ad-accounts/{ids['tiktok_id']}", headers=h(token))
    assert detail.json()["recharges"]["total"] == 1

    listed = api.get(
        "/api/portal/ad-accounts",
        headers=h(token),
        params={"keyword": "act_portal_1001"},
    ).json()["items"][0]
    assert listed["pending_recharge_count"] == 1
    assert listed["last_recharge_at"]
    # 列表里的可用余额同步扣掉冻结
    wallets = api.get("/api/portal/ad-accounts", headers=h(token)).json()["wallets"]
    assert wallets[0]["available"] == "9500.0000"


def test_recharge_guards(env):
    api, factory, ids, token = env

    # 超出可用余额：拒绝，且不落单
    over = api.post(
        f"/api/portal/ad-accounts/{ids['tiktok_id']}/recharge",
        headers=h(token),
        json={"amount": "999999", "request_no": "portal-test-over"},
    )
    assert over.status_code == 400
    assert over.json()["code"] == "INSUFFICIENT_BALANCE"

    # 封停账户不能充值
    banned = api.post(
        f"/api/portal/ad-accounts/{ids['banned_id']}/recharge",
        headers=h(token),
        json={"amount": "10", "request_no": "portal-test-banned"},
    )
    assert banned.status_code == 400
    assert "封停" in banned.json()["detail"]

    # 客户未开通：整站不能发起充值
    with factory() as db:
        client = db.get(Client, ids["owner_id"])
        client.status = ClientStatus.PENDING_LIMIT
        db.commit()
    inactive = api.post(
        f"/api/portal/ad-accounts/{ids['tiktok_id']}/recharge",
        headers=h(token),
        json={"amount": "10", "request_no": "portal-test-inactive"},
    )
    assert inactive.status_code == 400
    assert "未开通" in inactive.json()["detail"]

    with factory() as db:
        assert db.execute(select(RechargeOrder)).scalars().all() == []


def test_export_csv_excludes_internal_fields(env):
    api, _factory, _ids, token = env
    r = api.get("/api/portal/ad-accounts/export", headers=h(token))
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "act_portal_1001" in text
    assert "act_other_9001" not in text
    # 开户审核是客户可读字段；导出与列表口径一致
    assert "开户审核" in text and "已开户" in text
    for word in ("返点", "rebate", "成本系数", "代理商", "Yatop", "山恒"):
        assert word not in text

