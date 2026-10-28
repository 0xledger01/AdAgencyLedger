"""门户「我的钱包」：三卡口径、流水字段、筛选、充值渠道与授信还款申请。"""

from __future__ import annotations

from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import Client, ClientStatus, ClientTopUp, OwnerType, Role, User
from backend.app.models.base import money
from backend.app.models.enums import BizType, Direction
from backend.app.models.docs import SystemSetting
from backend.app.services import fx
from backend.app.models.base import utcnow
from backend.app.services.auth import create_token
from backend.app.services.ledger import get_or_create_wallet, post, reverse


@pytest.fixture()
def env(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'wallet.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    session = factory()
    client = Client(
        company_name="钱包测试客户",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        credit_limit=money("5000"),
        credit_used=money("1200"),
    )
    session.add(client)
    session.flush()
    client.biz_no = f"C{client.id:04d}"
    session.flush()

    wallet = get_or_create_wallet(
        session, owner_type=OwnerType.CLIENT, owner_id=client.id, currency="USD"
    )
    post(
        session,
        wallet_id=wallet.id,
        direction=Direction.IN,
        amount="10000",
        biz_type=BizType.TOPUP,
        idempotency_key="test:portal:wallet:1",
        biz_no="TP-TEST-1",
        operator="seed",
        remark="期初充值",
    )
    post(
        session,
        wallet_id=wallet.id,
        direction=Direction.OUT,
        amount="500",
        biz_type=BizType.RECHARGE,
        idempotency_key="test:portal:wallet:2",
        biz_no="RC-TEST-2",
        operator="finance01",
        remark="加款至广告账户",
    )
    original, _ = post(
        session,
        wallet_id=wallet.id,
        direction=Direction.IN,
        amount="300",
        biz_type=BizType.TOPUP,
        idempotency_key="test:portal:wallet:3",
        biz_no="TP-TEST-3",
        operator="seed",
        remark="待冲正充值",
    )
    reverse(session, tx=original, operator="finance01", reason="重复到账")
    session.flush()

    user = User(
        username="portal01",
        password_hash="not-used",
        display_name="周九",
        role=Role.CLIENT,
        client_id=client.id,
    )
    session.add(user)
    session.flush()
    token = create_token(user)
    ids = {"client_id": client.id, "wallet_id": wallet.id, "original_tx": original.id}
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


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def seed_pboc_cache(factory) -> None:  # noqa: ANN001
    """给报价接口塞一条新鲜缓存，测试不依赖外网。"""
    now = utcnow()
    with factory() as db:
        db.add(
            SystemSetting(
                key=fx.CACHE_KEY,
                value=json.dumps(
                    {
                        "rate": "6.7165",
                        "source": "中国外汇交易中心（央行中间价）",
                        "quoted_at": now.isoformat(timespec="seconds"),
                        "fetched_at": now.isoformat(timespec="seconds"),
                    }
                ),
            )
        )
        db.commit()


def test_topup_quote_prices_rmb(env, monkeypatch: pytest.MonkeyPatch) -> None:
    api, factory, _ids, token = env
    seed_pboc_cache(factory)
    monkeypatch.setattr(fx, "fetch_live", lambda: None)

    resp = api.get("/api/portal/topup-quote?amount=10", headers=headers(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert money(data["rate"]) == money("6.7165")
    # 10 × 6.7165 = 67.165 → 向上取整到 67.17
    assert money(data["payable_amount"]) == money("67.17")
    assert data["payable_currency"] == "CNY"
    assert data["amount_usd"] == "10.0000"


def test_topup_quote_rejects_stale_expected_rate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    api, factory, _ids, token = env
    seed_pboc_cache(factory)
    monkeypatch.setattr(fx, "fetch_live", lambda: None)

    resp = api.post(
        "/api/portal/topups",
        headers=headers(token),
        json={
            "amount": "10",
            "currency": "USD",
            "payment_channel": "OFFLINE",
            "expected_rate": "7.0000",
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "RATE_CHANGED"


def test_topup_saves_pboc_rate_snapshot(env, monkeypatch: pytest.MonkeyPatch) -> None:
    api, factory, ids, token = env
    seed_pboc_cache(factory)
    monkeypatch.setattr(fx, "fetch_live", lambda: None)

    resp = api.post(
        "/api/portal/topups",
        headers=headers(token),
        json={
            "amount": "10",
            "currency": "USD",
            "payment_channel": "OFFLINE",
            "expected_rate": "6.7165",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert money(data["payable_amount"]) == money("67.17")
    assert data["payable_currency"] == "CNY"
    assert money(data["exchange_rate"]) == money("6.7165")

    with factory() as db:
        row = db.query(ClientTopUp).filter(ClientTopUp.biz_no == data["biz_no"]).one()
        assert money(row.payable_amount) == money("67.17")
        assert row.payable_currency == "CNY"
        assert row.rate_source and "央行中间价" in row.rate_source


def test_wallet_cards_use_cash_plus_credit(env) -> None:
    api, _factory, _ids, token = env
    resp = api.get("/api/portal/wallet", headers=headers(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    item = data["items"][0]
    assert item["currency"] == "USD"
    # 10000 - 500 + 300 - 300（冲正）= 9500
    assert money(item["wallet_balance"]) == Decimal("9500")
    assert money(item["cash_available"]) == Decimal("9500")
    assert money(item["credit_limit"]) == Decimal("5000")
    assert money(item["credit_used"]) == Decimal("1200")
    assert money(item["credit_available"]) == Decimal("3800")
    # 资金总账户不含广告账户余额
    assert money(item["total_available"]) == Decimal("13300")
    assert data["can_repay"] is True


def test_transactions_expose_audit_fields(env) -> None:
    api, _factory, ids, token = env
    data = api.get("/api/portal/wallet/transactions", headers=headers(token)).json()
    assert data["total"] == 4

    items = data["items"]
    for it in items:
        assert it["biz_no"]
        assert it["balance_before"] is not None
        assert it["balance_after"] is not None
        assert it["status"] in ("DONE", "REVERSED")

    original = next(it for it in items if it["id"] == ids["original_tx"])
    assert original["status"] == "REVERSED"
    assert original["status_label"] == "已冲正"
    assert original["feedback"] and "冲正" in original["feedback"]

    # 冲正流水自己是已完成，方向相反
    reversal = next(it for it in items if it["direction"] == "OUT" and "冲正" in (it["remark"] or ""))
    assert reversal["status"] == "DONE"

    # 余额链首尾相接
    asc = sorted(items, key=lambda it: it["id"])
    for prev, cur in zip(asc, asc[1:]):
        assert money(cur["balance_before"]) == money(prev["balance_after"])


def test_transaction_filters(env) -> None:
    api, _factory, _ids, token = env
    h = headers(token)

    by_no = api.get("/api/portal/wallet/transactions?biz_no=RC-TEST", headers=h).json()
    assert by_no["total"] == 1
    assert by_no["items"][0]["biz_type"] == "RECHARGE"
    assert by_no["items"][0]["biz_type_label"] == "广告账户充值"

    reversed_only = api.get(
        "/api/portal/wallet/transactions?biz_type=TOPUP&status=REVERSED", headers=h
    ).json()
    assert reversed_only["total"] == 1
    assert reversed_only["items"][0]["status"] == "REVERSED"

    done_only = api.get("/api/portal/wallet/transactions?status=DONE", headers=h).json()
    assert done_only["total"] == 3

    assert api.get("/api/portal/wallet/transactions?status=BAD", headers=h).status_code == 422


def test_topup_channel_and_two_decimals(env) -> None:
    api, factory, ids, token = env
    h = headers(token)

    too_precise = api.post(
        "/api/portal/topups", headers=h, json={"amount": "1.001", "payment_channel": "OFFLINE"}
    )
    assert too_precise.status_code == 400
    assert "2 位小数" in too_precise.json()["detail"]

    not_open = api.post(
        "/api/portal/topups", headers=h, json={"amount": "100", "payment_channel": "YEEPAY"}
    )
    assert not_open.status_code == 400
    assert "暂未开通" in not_open.json()["detail"]

    ok = api.post(
        "/api/portal/topups",
        headers=h,
        json={"amount": "100.50", "payment_channel": "OFFLINE", "remark": "月底补款"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "PENDING_PAY"
    assert body["payment_channel"] == "OFFLINE"
    assert body["payment_channel_label"] == "线下汇款"

    with factory() as s:
        row = s.query(ClientTopUp).filter(ClientTopUp.client_id == ids["client_id"]).one()
        assert row.payment_channel == "OFFLINE"
        assert money(row.amount) == Decimal("100.5")

    listing = api.get("/api/portal/topups", headers=h).json()
    assert {c["code"] for c in listing["channels"]} == {"OFFLINE"}
    assert listing["items"][0]["payment_channel_label"] == "线下汇款"


def test_credit_repayment_application_does_not_touch_balance(env) -> None:
    api, factory, _ids, token = env
    h = headers(token)

    before = api.get("/api/portal/wallet", headers=h).json()["items"][0]

    too_much = api.post(
        "/api/portal/credit-repayments", headers=h, json={"amount": "99999"}
    )
    assert too_much.status_code == 400
    assert "超过已用授信" in too_much.json()["detail"]

    ok = api.post(
        "/api/portal/credit-repayments",
        headers=h,
        json={"amount": "500", "remark": "对公转账还款"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "PENDING_REVIEW"
    assert body["status_label"] == "待审核"

    after = api.get("/api/portal/wallet", headers=h).json()["items"][0]
    assert money(after["wallet_balance"]) == money(before["wallet_balance"])
    assert money(after["credit_used"]) == Decimal("1200")

    with factory() as s:
        row = s.query(ClientTopUp).filter(ClientTopUp.biz_no == body["biz_no"]).one()
        assert row.fund_account_type == "CREDIT"
        assert row.op_type == "INCREASE"
        assert row.applied_by == "周九"

    listing = api.get("/api/portal/topups", headers=h).json()
    item = next(t for t in listing["items"] if t["biz_no"] == body["biz_no"])
    assert item["fund_account_type_label"] == "授信账户"
    assert item["op_type_label"] == "加款"
