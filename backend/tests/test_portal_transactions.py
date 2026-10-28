"""门户「交易记录」：只读、只给本客户、白名单字段、筛选与导出。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import (
    AdAccount,
    Client,
    ClientStatus,
    OwnerType,
    Platform,
    RechargeOrder,
    Role,
    User,
    WalletTransaction,
)
from backend.app.models.base import money
from backend.app.services import recharge, txn_records
from backend.app.services.auth import create_token
from backend.app.services.ledger import get_or_create_wallet, post


def make_client(db: Session, name: str) -> Client:
    c = Client(company_name=name, settle_currency="USD", status=ClientStatus.ACTIVE)
    db.add(c)
    db.flush()
    c.biz_no = f"C{c.id:04d}"
    db.flush()
    return c


def make_account(db: Session, client: Client, account_id: str) -> AdAccount:
    acc = AdAccount(
        account_id=account_id,
        platform=Platform.TIKTOK,
        currency="USD",
        client_id=client.id,
        channel="Yatop",
        balance=money("100"),
        daily_limit=money("5000"),
    )
    db.add(acc)
    db.flush()
    return acc


def fund(db: Session, client: Client, key: str) -> None:
    w = get_or_create_wallet(
        db, owner_type=OwnerType.CLIENT, owner_id=client.id, currency="USD"
    )
    post(
        db,
        wallet_id=w.id,
        direction="IN",
        amount="10000",
        biz_type="TOPUP",
        idempotency_key=key,
        operator="seed",
    )
    db.flush()


@pytest.fixture()
def env(tmp_path):  # noqa: ANN001, ANN201
    engine = build_engine(f"sqlite:///{(tmp_path / 'portal-txn.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    with factory() as db:
        mine = make_client(db, "交易记录客户A")
        other = make_client(db, "交易记录客户B")
        fund(db, mine, "portal-txn:fund:a")
        fund(db, other, "portal-txn:fund:b")
        acc_a = make_account(db, mine, "act_portal_a")
        acc_b = make_account(db, other, "act_portal_b")

        pending, _ = recharge.create_recharge(
            db,
            ad_account_id=acc_a.id,
            amount="12.34",
            request_no="portal-txn-1",
            operator="客户经办",
        )
        done, _ = recharge.create_recharge(
            db,
            ad_account_id=acc_a.id,
            amount="20",
            request_no="portal-txn-2",
            operator="客户经办",
        )
        recharge.execute(db, done.id, outcome="SUCCESS", operator="执行员")
        foreign, _ = recharge.create_recharge(
            db,
            ad_account_id=acc_b.id,
            amount="99",
            request_no="portal-txn-3",
            operator="别人家客户",
        )

        user_a = User(
            username="portal_txn_a",
            password_hash="x",
            display_name="周九",
            role=Role.CLIENT,
            client_id=mine.id,
        )
        user_b = User(
            username="portal_txn_b",
            password_hash="x",
            display_name="吴十",
            role=Role.CLIENT,
            client_id=other.id,
        )
        desk_user = User(
            username="portal_txn_desk",
            password_hash="x",
            display_name="张三",
            role=Role.ADMIN,
        )
        db.add_all([user_a, user_b, desk_user])
        db.flush()

        ids = {
            "mine": mine.id,
            "other": other.id,
            "pending": pending.id,
            "done": done.id,
            "pending_no": pending.txn_no,
            "done_no": done.txn_no,
            "foreign_no": foreign.txn_no,
            "wallet_a": acc_a.id,
        }
        tokens = {
            "a": create_token(user_a),
            "b": create_token(user_b),
            "desk": create_token(desk_user),
        }
        db.commit()

    def _override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    api = TestClient(app)
    try:
        yield api, factory, ids, tokens
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_lists_only_own_transactions_with_whitelisted_fields(env) -> None:  # noqa: ANN001
    api, _factory, ids, tokens = env
    r = api.get("/api/portal/transactions", headers=h(tokens["a"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 2
    assert {row["txn_no"] for row in data["items"]} == {ids["pending_no"], ids["done_no"]}

    expected = {
        "id", "txn_no", "txn_type", "txn_type_label", "account_id", "amount",
        "currency", "status", "status_label", "feedback", "operator", "occurred_at",
    }
    for row in data["items"]:
        assert set(row) == expected
        assert row["account_id"] == "act_portal_a"
    # 内部字段一个都不许下发给客户
    blob = r.text
    for word in ("交易记录客户A", "Yatop", "返点", "rebate"):
        assert word not in blob
    assert data["txn_type_options"] == [{"code": "RECHARGE", "label": "广告账户充值"}]

    # 客户 B 看不到客户 A 的单
    r_b = api.get(
        "/api/portal/transactions", headers=h(tokens["b"]), params={"txn_no": ids["pending_no"]}
    )
    assert r_b.status_code == 200
    assert r_b.json()["total"] == 0

    # 内部后台账号不能借门户接口拿客户数据
    assert api.get("/api/portal/transactions", headers=h(tokens["desk"])).status_code == 403


def test_filters_and_status_labels(env) -> None:  # noqa: ANN001
    api, _factory, ids, tokens = env
    base = "/api/portal/transactions"

    done = api.get(base, headers=h(tokens["a"]), params={"status": "DONE"}).json()
    assert done["total"] == 1
    assert done["items"][0]["txn_no"] == ids["done_no"]
    assert done["items"][0]["status_label"] == "已完成"
    assert done["items"][0]["feedback"] == "已到账"

    pending = api.get(base, headers=h(tokens["a"]), params={"status": "PENDING"}).json()
    assert pending["total"] == 1
    assert pending["items"][0]["status_label"] == "待开始"
    assert pending["items"][0]["feedback"] is None

    by_no = api.get(base, headers=h(tokens["a"]), params={"txn_no": ids["pending_no"]}).json()
    assert by_no["total"] == 1
    assert by_no["items"][0]["id"] == ids["pending"]

    by_acc = api.get(
        base, headers=h(tokens["a"]), params={"account_keyword": "portal_a"}
    ).json()
    assert by_acc["total"] == 2
    by_other_acc = api.get(
        base, headers=h(tokens["a"]), params={"account_keyword": "portal_b"}
    ).json()
    assert by_other_acc["total"] == 0

    bad_status = api.get(base, headers=h(tokens["a"]), params={"status": "NOPE"})
    assert bad_status.status_code == 400
    bad_type = api.get(base, headers=h(tokens["a"]), params={"txn_type": "REFUND"})
    assert bad_type.status_code == 400


def test_export_is_scoped_and_has_no_rebate(env) -> None:  # noqa: ANN001
    api, _factory, ids, tokens = env
    r = api.get("/api/portal/transactions/export", headers=h(tokens["a"]))
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0] == "交易编号,交易类型,广告账户ID,交易金额,币种,状态,反馈信息,操作人,操作时间"
    assert ids["pending_no"] in text and ids["done_no"] in text
    assert ids["foreign_no"] not in text
    for word in ("返点", "rebate", "佣金"):
        assert word not in text


def test_list_is_read_only(env) -> None:  # noqa: ANN001
    api, factory, ids, tokens = env
    with factory() as db:
        before = (
            db.execute(select(func.count()).select_from(WalletTransaction)).scalar_one(),
            db.execute(select(func.count()).select_from(RechargeOrder)).scalar_one(),
        )
        account_balance = money(db.get(AdAccount, ids["wallet_a"]).balance)

    for _ in range(3):
        api.get("/api/portal/transactions", headers=h(tokens["a"]))
        api.get("/api/portal/transactions/export", headers=h(tokens["a"]))

    with factory() as db:
        after = (
            db.execute(select(func.count()).select_from(WalletTransaction)).scalar_one(),
            db.execute(select(func.count()).select_from(RechargeOrder)).scalar_one(),
        )
        assert money(db.get(AdAccount, ids["wallet_a"]).balance) == account_balance
    assert before == after


def test_service_rejects_cross_client_and_unknown_filters(env) -> None:  # noqa: ANN001
    api, factory, ids, _tokens = env
    with factory() as db:
        data = txn_records.list_client_transactions(db, client_id=ids["mine"], limit=10)
        assert data["total"] == 2
        assert {row["txn_no"] for row in data["items"]} == {ids["pending_no"], ids["done_no"]}
        # 分页顺序稳定：最新的在前
        page1 = txn_records.list_client_transactions(
            db, client_id=ids["mine"], limit=1, offset=0
        )
        page2 = txn_records.list_client_transactions(
            db, client_id=ids["mine"], limit=1, offset=1
        )
        assert page1["items"][0]["id"] != page2["items"][0]["id"]
