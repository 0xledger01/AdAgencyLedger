"""P3 接口自检（进程内 TestClient，不打真端口）。

覆盖契约第 8 节里所有后端可验证项：
1 确认到账余额 +X / 2 重复确认幂等 / 3 退回 / 4 冲正双签 / 5 未审批不得提交 /
6 核对清单缺项 / 7 审批→提交→回执 OPENED / 8 发起冻结 / 9 unknown 不动账 /
10 核查 FAILED 解冻 / 11 SUCCESS 三账联动 / 12 重复导入 skipped / 13 消耗不动钱包 /
14 客户 403 / 15 权限矩阵 403
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.api.deps import get_db
from backend.app.db import build_engine, init_db
from backend.app.main import app
from backend.app.models import (
    AdAccount,
    AgentChannel,
    AuditLog,
    Client,
    ClientStatus,
    OwnerType,
    Platform,
    RechargeOrder,
    Role,
    SpendDaily,
    User,
    Wallet,
    WalletTransaction,
)
from backend.app.models.base import money
from backend.app.services.auth import create_token
from backend.app.services.ledger import get_or_create_wallet, post, verify_wallet_chain


class Env:
    """一次测试一份独立库 + 独立 TestClient。"""

    def __init__(self, client: TestClient, factory, ids: dict, tokens: dict) -> None:
        self.client = client
        self.factory = factory
        self.ids = ids
        self.tokens = tokens

    # ---------------------------------------------------------- 小工具
    def h(self, who: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def db(self):  # noqa: ANN201
        return self.factory()

    def wallet(self) -> tuple[Decimal, Decimal, int]:
        with self.db() as s:
            w = s.execute(
                select(Wallet).where(
                    Wallet.owner_type == OwnerType.CLIENT,
                    Wallet.owner_id == self.ids["client_id"],
                    Wallet.currency == "USD",
                )
            ).scalar_one()
            return money(w.balance), money(w.frozen), w.id

    def account(self) -> tuple[Decimal, int]:
        with self.db() as s:
            a = s.get(AdAccount, self.ids["ad_account_id"])
            return money(a.balance), a.id

    def tx_count(self, key: str) -> int:
        with self.db() as s:
            return int(
                s.execute(
                    select(func.count()).select_from(WalletTransaction).where(
                        WalletTransaction.idempotency_key == key
                    )
                ).scalar_one()
            )

    def chain_ok(self) -> bool:
        with self.db() as s:
            verify_wallet_chain(s, self.ids["wallet_id"])
        return True

    def make_topup(self, amount: str = "5000", **extra) -> dict:
        body = {
            "client_id": self.ids["client_id"],
            "amount": amount,
            "currency": "USD",
            "payment_method": "BANK_TRANSFER",
            "payer": "张三",
            "fund_source_type": "SELF",
            "voucher": "seed/voucher.png",
            "remark": "财务代录",
        }
        body.update(extra)
        resp = self.client.post("/api/desk/topups", headers=self.h("finance01"), json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture()
def env(tmp_path):  # noqa: ANN201
    url = f"sqlite:///{(tmp_path / 'p3.db').as_posix()}"
    engine = build_engine(url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    session = factory()
    agent = AgentChannel(name="Yatop", code="YATOP", settled=True)
    session.add(agent)
    session.flush()

    client = Client(
        company_name="测试客户A",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        channel="Yatop",
        sales_owner="李四",
        cost_ratio=money("0.9"),
    )
    session.add(client)
    session.flush()
    client.biz_no = f"C{client.id:04d}"
    session.flush()

    account = AdAccount(
        account_id="act_1001",
        platform=Platform.FB,
        currency="USD",
        client_id=client.id,
        channel="Yatop",
        balance=money("1000"),
        daily_limit=money("5000"),
    )
    session.add(account)
    session.flush()

    # 客户钱包先充值 10000（走账本，不塞假余额）
    wallet = get_or_create_wallet(
        session, owner_type=OwnerType.CLIENT, owner_id=client.id, currency="USD"
    )
    post(
        session,
        wallet_id=wallet.id,
        direction="IN",
        amount="10000",
        biz_type="TOPUP",
        idempotency_key="test:fund:client:usd",
        operator="seed",
        remark="测试初始资金",
    )
    agent_wallet = get_or_create_wallet(
        session, owner_type=OwnerType.AGENT, owner_id=agent.id, currency="USD"
    )
    post(
        session,
        wallet_id=agent_wallet.id,
        direction="IN",
        amount="80000",
        biz_type="TRANSFER",
        idempotency_key="test:fund:agent:usd",
        operator="seed",
    )

    users = {}
    for username, display, role in [
        ("finance01", "赵六", Role.FINANCE),
        ("ops01", "钱七", Role.OPS),
        ("admin", "张三", Role.ADMIN),
        ("client01", "周九", Role.CLIENT),
    ]:
        u = User(
            username=username,
            password_hash="not-used",
            display_name=display,
            role=role,
            client_id=client.id if role is Role.CLIENT else None,
        )
        session.add(u)
        users[username] = u
    session.flush()

    tokens = {name: create_token(u) for name, u in users.items()}
    ids = {
        "client_id": client.id,
        "ad_account_id": account.id,
        "wallet_id": wallet.id,
        "agent_wallet_id": agent_wallet.id,
    }
    session.commit()
    session.close()

    def _override():  # noqa: ANN202
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    client_api = TestClient(app)
    try:
        yield Env(client_api, factory, ids, tokens)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------- 断言 1 / 2

def test_confirm_topup_increases_balance_and_is_idempotent(env: Env) -> None:
    before, _frozen, _wid = env.wallet()
    item = env.make_topup("5000")
    assert item["status"] == "PENDING_CONFIRM"
    assert item["status_label"] == "待确认"
    assert item["payment_method_label"] == "线下转账"
    assert item["fund_source_label"] == "客户自有"

    resp = env.client.post(
        f"/api/desk/topups/{item['id']}/confirm", headers=env.h("finance01"), json={"remark": "核对到账"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "RECEIVED" and data["status_label"] == "已到账"
    assert money(data["balance"]) == before + Decimal("5000")
    assert money(data["available"]) == before + Decimal("5000")

    after, _f, _w = env.wallet()
    assert after == before + Decimal("5000")
    assert env.tx_count(f"topup:{item['id']}") == 1

    with env.db() as s:
        tx = s.execute(
            select(WalletTransaction).where(
                WalletTransaction.idempotency_key == f"topup:{item['id']}"
            )
        ).scalar_one()
        assert str(tx.biz_type) == "TOPUP"
        assert str(tx.direction) == "IN"
        assert tx.ref_doctype == "client_topup" and tx.ref_id == item["id"]

    # 重复确认：200、tx_id 相同、余额与流水数都不再变化
    again = env.client.post(
        f"/api/desk/topups/{item['id']}/confirm", headers=env.h("finance01"), json={}
    )
    assert again.status_code == 200
    assert again.json()["tx_id"] == data["tx_id"]
    assert env.wallet()[0] == after
    assert env.tx_count(f"topup:{item['id']}") == 1
    assert env.chain_ok()


def test_topup_detail_returns_ledger(env: Env) -> None:
    item = env.make_topup("800")
    env.client.post(
        f"/api/desk/topups/{item['id']}/confirm", headers=env.h("finance01"), json={}
    )
    resp = env.client.get(f"/api/desk/topups/{item['id']}", headers=env.h("finance01"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["id"] == item["id"]
    assert len(body["ledger"]) == 1
    tx = body["ledger"][0]
    assert set(tx) == {
        "id", "seq", "direction", "amount", "currency",
        "balance_after", "biz_type", "operator", "remark", "occurred_at",
    }
    assert tx["direction"] == "IN" and tx["biz_type"] == "TOPUP"


# ---------------------------------------------------------------- 断言 3

def test_reject_topup_keeps_balance(env: Env) -> None:
    before, _f, _w = env.wallet()
    # 财务代录即使没传凭证也落「待确认」（否则 3.5 退回路走不通）
    item = env.make_topup("3000", voucher=None)
    assert item["status"] == "PENDING_CONFIRM"
    resp = env.client.post(
        f"/api/desk/topups/{item['id']}/reject",
        headers=env.h("finance01"),
        json={"reason": "凭证金额不符"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RETURNED"
    assert resp.json()["status_label"] == "已退回"
    assert env.wallet()[0] == before

    # 空原因必须 400（不是 422）
    item2 = env.make_topup("100")
    bad = env.client.post(
        f"/api/desk/topups/{item2['id']}/reject", headers=env.h("finance01"), json={"reason": ""}
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "STATE_ERROR"


# ---------------------------------------------------------------- 断言 4

def test_reverse_topup_needs_second_sign(env: Env) -> None:
    item = env.make_topup("2000")
    env.client.post(
        f"/api/desk/topups/{item['id']}/confirm", headers=env.h("finance01"), json={}
    )
    after_confirm, _f, _w = env.wallet()

    miss = env.client.post(
        f"/api/desk/topups/{item['id']}/reverse",
        headers=env.h("admin"),
        json={"reason": "银行退票"},
    )
    assert miss.status_code == 400

    same = env.client.post(
        f"/api/desk/topups/{item['id']}/reverse",
        headers=env.h("admin"),
        json={"reason": "银行退票", "second_approver": "张三"},
    )
    assert same.status_code == 400
    assert env.wallet()[0] == after_confirm

    ok = env.client.post(
        f"/api/desk/topups/{item['id']}/reverse",
        headers=env.h("admin"),
        json={"reason": "银行退票", "second_approver": "赵六"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "REVERSED" and body["status_label"] == "已冲正"
    assert body["reverse_tx_id"]
    assert money(body["balance"]) == after_confirm - Decimal("2000")

    # 原流水仍在（只追加红字）
    assert env.tx_count(f"topup:{item['id']}") == 1
    detail = env.client.get(f"/api/desk/topups/{item['id']}", headers=env.h("finance01")).json()
    assert len(detail["ledger"]) == 2
    assert env.chain_ok()


def test_adjust_is_idempotent_by_request_no(env: Env) -> None:
    before, _f, _w = env.wallet()
    body = {
        "amount": "100",
        "direction": "IN",
        "reason": "多收退款抵扣",
        "request_no": "ADJ-20261028-001",
        "second_approver": "张三",
    }
    resp = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/adjust",
        headers=env.h("finance01"),
        json=body,
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert money(first["balance"]) == before + Decimal("100")

    again = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/adjust",
        headers=env.h("finance01"),
        json=body,
    )
    assert again.status_code == 200
    assert again.json()["tx_id"] == first["tx_id"]
    assert env.wallet()[0] == before + Decimal("100")

    # 第二签不能是本人
    same = env.client.post(
        f"/api/desk/clients/{env.ids['client_id']}/adjust",
        headers=env.h("admin"),
        json={**body, "request_no": "ADJ-20261028-002", "second_approver": "张三"},
    )
    assert same.status_code == 400


# ---------------------------------------------------------------- 断言 5 / 6 / 7

def _new_open_request(env: Env, daily_limit: str = "1000") -> dict:
    resp = env.client.post(
        "/api/desk/open-requests",
        headers=env.h("ops01"),
        json={
            "client_id": env.ids["client_id"],
            "platform": "TIKTOK",
            "bm_id": "BM-1",
            "timezone": "Asia/Shanghai",
            "currency": "USD",
            "daily_limit": daily_limit,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


ALL_CHECKS = {"subject": True, "industry": True, "bm": True, "duplicate": True, "channel": True}


def test_submit_without_approval_is_blocked(env: Env) -> None:
    item = _new_open_request(env)
    assert item["status"] == "PENDING_APPROVAL" and item["status_label"] == "待审批"

    resp = env.client.post(
        f"/api/desk/open-requests/{item['id']}/submit", headers=env.h("ops01")
    )
    assert resp.status_code == 400
    assert "未审批通过" in resp.json()["detail"]


def test_approve_requires_all_five_checks(env: Env) -> None:
    item = _new_open_request(env)
    resp = env.client.post(
        f"/api/desk/open-requests/{item['id']}/approve",
        headers=env.h("ops01"),
        json={"checks": {**ALL_CHECKS, "duplicate": False}, "remark": "核对"},
    )
    assert resp.status_code == 400
    assert "duplicate" in resp.json()["detail"]

    missing = env.client.post(
        f"/api/desk/open-requests/{item['id']}/approve",
        headers=env.h("ops01"),
        json={"checks": {"subject": True, "industry": True, "bm": True, "channel": True}},
    )
    assert missing.status_code == 400
    assert "duplicate" in missing.json()["detail"]


def test_open_request_full_flow_creates_ad_account(env: Env) -> None:
    item = _new_open_request(env, daily_limit="1500")

    approved = env.client.post(
        f"/api/desk/open-requests/{item['id']}/approve",
        headers=env.h("ops01"),
        json={"checks": ALL_CHECKS, "remark": "核对通过"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "PENDING_SUBMIT"
    assert approved.json()["status_label"] == "待提交"

    submitted = env.client.post(
        f"/api/desk/open-requests/{item['id']}/submit", headers=env.h("ops01")
    )
    assert submitted.status_code == 200, submitted.text
    sub = submitted.json()
    assert sub["status"] == "AGENT_REVIEWING" and sub["status_label"] == "一代审核中"
    assert json.loads(sub["agent_receipt"])["mock"] is True

    receipt = env.client.post(
        f"/api/desk/open-requests/{item['id']}/receipt",
        headers=env.h("ops01"),
        json={"result": "OPENED", "account_id": "1234567890"},
    )
    assert receipt.status_code == 200, receipt.text
    rec = receipt.json()
    assert rec["status"] == "OPENED" and rec["status_label"] == "已开户"
    assert rec["ad_account_id"]

    with env.db() as s:
        acc = s.get(AdAccount, rec["ad_account_id"])
        assert acc.account_id == "1234567890"
        assert acc.client_id == env.ids["client_id"]
        assert str(acc.platform) == "TIKTOK"
        assert money(acc.daily_limit) == money("1500")
        assert acc.channel == "Yatop"
        assert str(acc.status) == "NORMAL"
        assert acc.opened_at is not None

    # 账户 ID 重复 → 400 账户 ID 已存在
    item2 = _new_open_request(env)
    env.client.post(
        f"/api/desk/open-requests/{item2['id']}/approve",
        headers=env.h("ops01"), json={"checks": ALL_CHECKS},
    )
    env.client.post(f"/api/desk/open-requests/{item2['id']}/submit", headers=env.h("ops01"))
    dup = env.client.post(
        f"/api/desk/open-requests/{item2['id']}/receipt",
        headers=env.h("ops01"),
        json={"result": "OPENED", "account_id": "1234567890"},
    )
    assert dup.status_code == 400
    assert "账户 ID 已存在" in dup.json()["detail"]


def test_batch_submit_skips_unapproved(env: Env) -> None:
    ready = _new_open_request(env)
    env.client.post(
        f"/api/desk/open-requests/{ready['id']}/approve",
        headers=env.h("ops01"), json={"checks": ALL_CHECKS},
    )
    not_ready = _new_open_request(env)

    resp = env.client.post(
        "/api/desk/open-requests/batch-submit",
        headers=env.h("ops01"),
        json={"ids": [ready["id"], not_ready["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["submitted"] == [ready["id"]]
    assert body["skipped"][0]["id"] == not_ready["id"]
    assert "未审批通过" in body["skipped"][0]["reason"]


def test_open_request_reject(env: Env) -> None:
    item = _new_open_request(env)
    resp = env.client.post(
        f"/api/desk/open-requests/{item['id']}/reject",
        headers=env.h("ops01"),
        json={"reason": "主体与营业执照不一致"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    assert resp.json()["status_label"] == "已驳回"


def test_approve_remark_goes_to_audit_log(env: Env) -> None:
    """OpenRequest 无 remark 列，审批备注必须落到 AuditLog 留痕。"""
    item = _new_open_request(env)
    resp = env.client.post(
        f"/api/desk/open-requests/{item['id']}/approve",
        headers=env.h("ops01"),
        json={"checks": ALL_CHECKS, "remark": "主体、BM、渠道均已核对"},
    )
    assert resp.status_code == 200, resp.text

    with env.db() as s:
        log = s.execute(
            select(AuditLog).where(
                AuditLog.doctype == "open_request",
                AuditLog.doc_id == item["id"],
                AuditLog.action == "APPROVE",
            )
        ).scalar_one()
        assert log.reason == "主体、BM、渠道均已核对"
        assert log.actor == "钱七"


# ---------------------------------------------------------------- 断言 8 / 9 / 10 / 11

def _create_recharge(env: Env, amount: str = "500", request_no: str | None = None) -> dict:
    resp = env.client.post(
        "/api/desk/recharges",
        headers=env.h("ops01"),
        json={
            "ad_account_id": env.ids["ad_account_id"],
            "amount": amount,
            "request_no": request_no or f"REQ-{uuid4().hex[:12]}",
            "remark": "客户要求",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_recharge_freezes_only(env: Env) -> None:
    balance, frozen, _wid = env.wallet()
    item = _create_recharge(env, "500")
    assert item["status"] == "PENDING_EXEC" and item["status_label"] == "待执行"
    assert item["initiated_by"] == "钱七"
    assert item["account_id"] == "act_1001" and item["platform"] == "FB"

    new_balance, new_frozen, _w = env.wallet()
    assert new_balance == balance
    assert new_frozen == frozen + Decimal("500")


def test_recharge_unknown_does_not_move_money(env: Env) -> None:
    item = _create_recharge(env, "400")
    balance, frozen, _wid = env.wallet()

    resp = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "UNKNOWN"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "TO_VERIFY"
    assert resp.json()["status_label"] == "待核查"

    after_balance, after_frozen, _w = env.wallet()
    assert after_balance == balance
    assert after_frozen == frozen
    assert env.tx_count(f"recharge:{item['id']}") == 0
    assert env.chain_ok()

    # 核查 FAILED → 解冻、余额不变
    failed = env.client.post(
        f"/api/desk/recharges/{item['id']}/resolve",
        headers=env.h("ops01"),
        json={"result": "FAILED", "reason": "媒体后台未到账"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "REJECTED"
    final_balance, final_frozen, _w = env.wallet()
    assert final_balance == balance
    assert final_frozen == Decimal("0.0000")

    # 重试：重新冻结 + 回到待执行 + retry_count=1
    retried = env.client.post(
        f"/api/desk/recharges/{item['id']}/retry", headers=env.h("ops01")
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "PENDING_EXEC"
    assert retried.json()["retry_count"] == 1
    assert env.wallet()[1] == Decimal("400.0000")


def test_recharge_success_moves_three_books(env: Env) -> None:
    item = _create_recharge(env, "500")
    balance, _frozen, _wid = env.wallet()
    acc_before, _aid = env.account()
    with env.db() as s:
        agent_before = money(
            s.execute(
                select(Wallet).where(
                    Wallet.owner_type == OwnerType.AGENT,
                    Wallet.owner_id == s.execute(
                        select(AgentChannel).where(AgentChannel.name == "Yatop")
                    ).scalar_one().id,
                )
            ).scalar_one().balance
        )

    resp = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS" and body["status_label"] == "成功"
    assert body["serial_no"]
    assert money(body["balance"]) == balance - Decimal("500")
    assert money(body["account_balance"]) == acc_before + Decimal("500")

    # 客户钱包 −X、广告账户 +X、一代钱包 −X
    assert env.wallet()[0] == balance - Decimal("500")
    assert env.wallet()[1] == Decimal("0.0000")
    assert env.account()[0] == acc_before + Decimal("500")
    with env.db() as s:
        agent_after = money(
            s.execute(
                select(Wallet).where(
                    Wallet.owner_type == OwnerType.AGENT,
                    Wallet.owner_id == s.execute(
                        select(AgentChannel).where(AgentChannel.name == "Yatop")
                    ).scalar_one().id,
                )
            ).scalar_one().balance
        )
    assert agent_after == agent_before - Decimal("500")

    with env.db() as s:
        tx = s.execute(
            select(WalletTransaction).where(
                WalletTransaction.idempotency_key == f"recharge:{item['id']}"
            )
        ).scalar_one()
        assert str(tx.biz_type) == "RECHARGE" and str(tx.direction) == "OUT"
        assert tx.ref_doctype == "recharge_order" and tx.ref_id == item["id"]
    assert env.chain_ok()

    # 已成功的单不能再执行
    again = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    assert again.status_code == 400


def test_recharge_circuit_breaker(env: Env) -> None:
    item = _create_recharge(env, "100")
    on = env.client.post(
        "/api/desk/recharges/circuit", headers=env.h("ops01"), json={"platform": "FB", "enabled": True}
    )
    assert on.status_code == 200
    assert on.json() == {"platform": "FB", "enabled": True, "persisted": True}

    blocked = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    assert blocked.status_code == 400
    assert "已熔断" in blocked.json()["detail"]

    listing = env.client.get("/api/desk/recharges/circuit", headers=env.h("ops01")).json()
    assert {"platform": "FB", "enabled": True} in listing["items"]

    env.client.post(
        "/api/desk/recharges/circuit", headers=env.h("ops01"), json={"platform": "FB", "enabled": False}
    )
    ok = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    assert ok.status_code == 200


def test_recharge_rejected_releases_freeze(env: Env) -> None:
    balance, frozen, _wid = env.wallet()
    item = _create_recharge(env, "200")
    resp = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "REJECTED"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    after_balance, after_frozen, _w = env.wallet()
    assert after_balance == balance
    assert after_frozen == frozen


def test_recharge_insufficient_balance(env: Env) -> None:
    resp = env.client.post(
        "/api/desk/recharges",
        headers=env.h("ops01"),
        json={
            "ad_account_id": env.ids["ad_account_id"],
            "amount": "999999",
            "request_no": "REQ-BALANCE-0001",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INSUFFICIENT_BALANCE"

    zero = env.client.post(
        "/api/desk/recharges",
        headers=env.h("ops01"),
        json={
            "ad_account_id": env.ids["ad_account_id"],
            "amount": "0",
            "request_no": "REQ-BALANCE-0002",
        },
    )
    assert zero.status_code == 400
    assert zero.json()["code"] == "INVALID_AMOUNT"

    missing = env.client.post(
        "/api/desk/recharges",
        headers=env.h("ops01"),
        json={"ad_account_id": 999999, "amount": "10", "request_no": "REQ-BALANCE-0003"},
    )
    assert missing.status_code == 400


def test_recharge_request_no_idempotent(env: Env) -> None:
    """同一个 request_no 连点两次：只一张单、只冻结一次。"""
    balance, frozen, _wid = env.wallet()
    req = "REQ-IDEMPOTENT-0001"

    first = _create_recharge(env, "300", request_no=req)
    assert first["deduped"] is False
    mid_balance, mid_frozen, _w = env.wallet()
    assert mid_balance == balance
    assert mid_frozen == frozen + Decimal("300")

    second = _create_recharge(env, "300", request_no=req)
    assert second["deduped"] is True
    assert second["id"] == first["id"]
    assert second["biz_no"] == first["biz_no"]

    end_balance, end_frozen, _w = env.wallet()
    assert end_balance == balance
    assert end_frozen == mid_frozen  # 没有第二次冻结

    with env.db() as s:
        rows = s.execute(
            select(RechargeOrder).where(
                RechargeOrder.idempotency_key == f"recharge:{req}"
            )
        ).scalars().all()
        assert len(rows) == 1

    # request_no 缺失 → 422（pydantic）
    no_key = env.client.post(
        "/api/desk/recharges",
        headers=env.h("ops01"),
        json={"ad_account_id": env.ids["ad_account_id"], "amount": "10"},
    )
    assert no_key.status_code == 422


def test_recharge_cancel_releases_freeze(env: Env) -> None:
    balance, frozen, _wid = env.wallet()
    item = _create_recharge(env, "250")
    assert env.wallet()[1] == frozen + Decimal("250")
    acc_before, _aid = env.account()

    resp = env.client.post(
        f"/api/desk/recharges/{item['id']}/cancel",
        headers=env.h("ops01"),
        json={"reason": "客户临时取消加款"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["status_label"] == "已撤销"
    assert body["reason"] == "客户临时取消加款"

    end_balance, end_frozen, _w = env.wallet()
    assert end_balance == balance  # 钱从没出去过
    assert end_frozen == frozen  # 冻结回落
    assert env.account()[0] == acc_before  # 广告账户不动
    assert env.tx_count(f"recharge:{item['id']}") == 0

    with env.db() as s:
        order = s.get(RechargeOrder, item["id"])
        assert json.loads(order.receipt)["cancel"]["by"] == "钱七"

    # summary 自然多出 CANCELLED 计数
    listing = env.client.get(
        "/api/desk/recharges", headers=env.h("ops01"), params={"status": "CANCELLED"}
    )
    assert listing.status_code == 200
    assert listing.json()["summary"]["CANCELLED"] >= 1

    # 已撤销的单不能再执行/撤销
    again = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    assert again.status_code == 400


def test_recharge_cancel_rejects_illegal_state(env: Env) -> None:
    item = _create_recharge(env, "120")
    ok = env.client.post(
        f"/api/desk/recharges/{item['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "REJECTED"},
    )
    assert ok.status_code == 200

    # REJECTED 不能撤销
    resp = env.client.post(
        f"/api/desk/recharges/{item['id']}/cancel",
        headers=env.h("ops01"),
        json={"reason": "想撤销"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "STATE_ERROR"

    # 空 reason → 400
    pending = _create_recharge(env, "80")
    blank = env.client.post(
        f"/api/desk/recharges/{pending['id']}/cancel",
        headers=env.h("ops01"),
        json={"reason": "   "},
    )
    assert blank.status_code == 400
    assert blank.json()["code"] == "STATE_ERROR"


# ---------------------------------------------------------------- 断言 12 / 13

def test_spend_import_dedup_and_wallet_untouched(env: Env) -> None:
    wallet_before = env.wallet()[0]
    acc_before, _aid = env.account()
    day = (date.today() - timedelta(days=1)).isoformat()

    rows = [
        {"date": day, "account_id": "act_1001", "spend": "120.5", "currency": "USD", "exchange_rate": "1"},
        {"date": day, "account_id": "act_1001", "spend": "10", "currency": "USD", "exchange_rate": "1"},
        {"date": "2026-13-45", "account_id": "act_1001", "spend": "10"},
        {"date": day, "account_id": "act_9999", "spend": "10"},
        {"date": day, "account_id": "act_1001", "spend": "-5"},
        {"date": "2999-01-01", "account_id": "act_1001", "spend": "10"},
    ]

    dry = env.client.post(
        "/api/desk/spend/import", headers=env.h("finance01"), json={"dry_run": True, "rows": rows}
    )
    assert dry.status_code == 200, dry.text
    report = dry.json()
    assert report["dry_run"] is True
    assert report["total"] == 6
    assert report["success"] == 1
    assert len(report["failed"]) == 4
    reasons = {f["reason"] for f in report["failed"]}
    assert reasons == {"日期非法", "账户不存在", "金额为负"}
    assert report["skipped"] == [{"row": 2, "account_id": "act_1001", "date": day, "reason": "批次内重复"}]
    with env.db() as s:
        assert s.execute(select(func.count()).select_from(SpendDaily)).scalar_one() == 0

    real = env.client.post(
        "/api/desk/spend/import", headers=env.h("finance01"), json={"dry_run": False, "rows": rows}
    )
    assert real.status_code == 200
    assert real.json()["success"] == 1

    # 消耗只扣广告账户，不动客户钱包
    assert env.wallet()[0] == wallet_before
    assert env.account()[0] == acc_before - money("120.5")

    # 第二次全部 skipped 且 reason 含「已存在」
    again = env.client.post(
        "/api/desk/spend/import",
        headers=env.h("finance01"),
        json={"dry_run": False, "rows": rows[:2]},
    )
    assert again.status_code == 200
    body = again.json()
    assert body["success"] == 0
    assert len(body["skipped"]) == 2
    assert all("已存在" in s["reason"] for s in body["skipped"])
    assert env.wallet()[0] == wallet_before


def test_spend_anomaly_flags(env: Env) -> None:
    day = (date.today() - timedelta(days=1)).isoformat()
    # 先造 7 天历史均值 100
    with env.db() as s:
        for i in range(7):
            s.add(
                SpendDaily(
                    date=date.today() - timedelta(days=i + 2),
                    ad_account_id=env.ids["ad_account_id"],
                    spend=money("100"),
                    currency="USD",
                    exchange_rate=money("1"),
                    base_spend=money("100"),
                    source="IMPORT",
                )
            )
        s.commit()

    resp = env.client.post(
        "/api/desk/spend/import",
        headers=env.h("finance01"),
        json={
            "dry_run": True,
            "rows": [
                {"date": day, "account_id": "act_1001", "spend": "500"},
                {"date": date.today().isoformat(), "account_id": "act_1001", "spend": "0"},
            ],
        },
    )
    assert resp.status_code == 200
    anomalies = resp.json()["anomalies"]
    assert [a["reason"] for a in anomalies] == ["SPIKE", "ZERO_SPEND"]


def test_spend_list(env: Env) -> None:
    day = (date.today() - timedelta(days=1)).isoformat()
    env.client.post(
        "/api/desk/spend/import",
        headers=env.h("finance01"),
        json={"dry_run": False, "rows": [{"date": day, "account_id": "act_1001", "spend": "88"}]},
    )
    resp = env.client.get("/api/desk/spend", headers=env.h("finance01"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["account_id"] == "act_1001"
    assert item["client_name"] == "测试客户A"
    assert item["source"] == "IMPORT"
    assert money(item["spend"]) == money("88")
    assert body["totals"]["spend"] == [{"currency": "USD", "amount": "88.0000"}]
    # 单条 88、无历史 → 既不算 SPIKE 也不算 ZERO_SPEND
    assert item["anomaly"] is None


def test_spend_list_flags_spike(env: Env) -> None:
    """列表也要跑异常判定（不是只在导入报告里跑）。"""
    with env.db() as s:
        for i in range(7):
            s.add(
                SpendDaily(
                    date=date.today() - timedelta(days=i + 2),
                    ad_account_id=env.ids["ad_account_id"],
                    spend=money("100"),
                    currency="USD",
                    exchange_rate=money("1"),
                    base_spend=money("100"),
                    source="IMPORT",
                )
            )
        s.commit()

    day = (date.today() - timedelta(days=1)).isoformat()
    resp = env.client.post(
        "/api/desk/spend/import",
        headers=env.h("finance01"),
        json={"dry_run": False, "rows": [{"date": day, "account_id": "act_1001", "spend": "500"}]},
    )
    assert resp.status_code == 200
    assert [a["reason"] for a in resp.json()["anomalies"]] == ["SPIKE"]

    listing = env.client.get(
        "/api/desk/spend", headers=env.h("finance01"), params={"account_id": "act_1001"}
    )
    assert listing.status_code == 200
    row = listing.json()["items"][0]
    assert row["date"] == day
    assert money(row["spend"]) == money("500")
    assert row["anomaly"] == "SPIKE"


# ---------------------------------------------------------------- 断言 14 / 15

def test_client_role_is_blocked(env: Env) -> None:
    for path in ("/api/desk/topups", "/api/desk/open-requests", "/api/desk/recharges", "/api/desk/spend"):
        resp = env.client.get(path, headers=env.h("client01"))
        assert resp.status_code == 403, path


def test_permission_matrix(env: Env) -> None:
    item = env.make_topup("700")
    # OPS 有 TOPUP_VIEW，没有 TOPUP_CONFIRM
    assert env.client.get("/api/desk/topups", headers=env.h("ops01")).status_code == 200
    denied = env.client.post(
        f"/api/desk/topups/{item['id']}/confirm", headers=env.h("ops01"), json={}
    )
    assert denied.status_code == 403
    # 没有 token 一律 401
    assert env.client.get("/api/desk/topups").status_code == 401


# ---------------------------------------------------------------- 列表形状

def test_list_shapes(env: Env) -> None:
    env.make_topup("1000")
    pending = env.client.get(
        "/api/desk/topups", headers=env.h("finance01"), params={"status": "PENDING_CONFIRM"}
    )
    assert pending.status_code == 200
    body = pending.json()
    assert body["total"] >= 1
    assert body["items"][0]["biz_no"].startswith("TP")
    assert body["summary"]["pending_count"] >= 1
    assert {"currency": "USD", "amount": "1000.0000"} in body["summary"]["pending_amount"]

    keyword = env.client.get(
        "/api/desk/topups", headers=env.h("finance01"), params={"keyword": "测试客户A"}
    )
    assert keyword.status_code == 200 and keyword.json()["total"] >= 1

    open_list = env.client.get("/api/desk/open-requests", headers=env.h("ops01"))
    assert open_list.status_code == 200
    assert set(open_list.json()["summary"]) >= {"PENDING_APPROVAL", "PENDING_SUBMIT", "OPENED"}

    item = _create_recharge(env, "50")
    env.client.post(
        f"/api/desk/recharges/{item['id']}/exec", headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    rech = env.client.get("/api/desk/recharges", headers=env.h("ops01")).json()
    assert rech["total"] >= 1
    row = rech["items"][0]
    assert row["biz_no"].startswith("RC")
    assert row["status_label"] == "成功"
    assert row["elapsed_ms"] is not None
    assert rech["summary"].get("SUCCESS", 0) >= 1


# ---------------------------------------------------------------- 资金总览卡片 / 交易明细

def test_overview_account_cards_and_client_keyword(env: Env) -> None:
    """总览主视图是逐广告账户卡片；交易明细支持客户名称/编号模糊筛选与导出。"""
    resp = env.client.get("/api/desk/wallets/overview", headers=env.h("admin"))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    cards = data.get("accounts")
    assert isinstance(cards, list) and cards
    card = next(a for a in cards if a["account_id"] == "act_1001")
    assert card["client_name"] == "测试客户A"
    assert card["channel"] == "Yatop"
    assert card["currency"] == "USD"
    assert card["currency_label"] == "美元"
    assert card["status"] == "NORMAL"
    assert card["status_label"] == "正常"
    assert card["updated_at"]

    # 老口径保留，越权/分组断言不被这次改版打破
    assert isinstance(data["ad_accounts"]["by_agent"], list)
    assert isinstance(data["client_wallets"]["by_client"], list)

    item = _create_recharge(env, "100", "TEST-OVERVIEW-001")
    listed = env.client.get(
        "/api/desk/recharges",
        headers=env.h("finance01"),
        params={"client_keyword": "测试客户"},
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()["items"]
    assert any(r["id"] == item["id"] for r in rows)
    row = next(r for r in rows if r["id"] == item["id"])
    # 未执行成功：到账金额为空，不能把请求金额伪装成已到账
    assert row["credited_amount"] is None
    assert row["sync_at"]
    assert row["status_label"] == "待执行"

    empty = env.client.get(
        "/api/desk/recharges",
        headers=env.h("finance01"),
        params={"client_keyword": "不存在的客户"},
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    exported = env.client.get(
        "/api/desk/recharges/export",
        headers=env.h("finance01"),
        params={"client_keyword": "测试客户"},
    )
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert "广告账户充值" in exported.text
    assert "测试客户A" in exported.text


# ---------------------------------------------------------------- 客户申请 → 管理端审批

ALL_CHECKS_BODY = {
    "subject": True, "industry": True, "bm": True, "duplicate": True, "channel": True,
}


def _client_apply(env: Env, **extra) -> dict:
    body = {
        "platform": "TIKTOK",
        "subject": "测试客户A",
        "apply_count": 2,
        "bm_id": "BM-APPLY-1",
        "timezone": "America/New_York",
        "currency": "USD",
        "daily_limit": "800",
        "remark": "需要两个 TikTok 户",
    }
    body.update(extra)
    resp = env.client.post("/api/portal/open-requests", headers=env.h("client01"), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_client_applies_then_desk_auto_approves_to_opened(env: Env) -> None:
    item = _client_apply(env)
    assert item["status"] == "PENDING_APPROVAL" and item["status_label"] == "待审批"
    assert item["apply_count"] == 2 and item["completed_count"] == 0
    assert item["open_method_label"] == "自动开户"
    assert item["platform_label"] == "TikTok"

    # 客户提交后立刻出现在管理端「待处理」
    pending = env.client.get(
        "/api/desk/open-requests", headers=env.h("ops01"), params={"tab": "pending"}
    )
    assert pending.status_code == 200
    body = pending.json()
    assert body["pending_total"] >= 1
    row = next(r for r in body["items"] if r["id"] == item["id"])
    assert row["source"] == "CLIENT" and row["source_label"] == "客户申请"
    assert row["subject"] == "测试客户A"
    assert row["channel"] == "Yatop"
    assert row["apply_count"] == 2 and row["completed_count"] == 0

    approved = env.client.post(
        f"/api/desk/open-requests/{item['id']}/approve",
        headers=env.h("ops01"),
        json={"checks": ALL_CHECKS_BODY, "remark": "核对通过", "auto_complete": True},
    )
    assert approved.status_code == 200, approved.text
    got = approved.json()
    assert got["status"] == "OPENED" and got["status_label"] == "已开户"
    assert got["completed_count"] == 2

    with env.db() as s:
        rows = s.execute(
            select(AdAccount).where(AdAccount.client_id == env.ids["client_id"])
        ).scalars().all()
        auto = [a for a in rows if a.account_id.startswith("AUTO-")]
        assert len(auto) == 2
        assert {a.platform for a in auto} == {Platform.TIKTOK}
        assert all(a.channel == "Yatop" for a in auto)

    mine = env.client.get("/api/portal/open-requests", headers=env.h("client01"))
    assert mine.status_code == 200
    mine_row = next(r for r in mine.json()["items"] if r["id"] == item["id"])
    assert mine_row["status"] == "OPENED" and mine_row["completed_count"] == 2
    assert mine_row["handler"] == "钱七"
    # 列表「申请人」回真实提交人，不是当前登录人（同客户可能有多个门户账号）
    me = env.client.get("/api/auth/me", headers=env.h("client01")).json()
    assert mine_row["applicant"] == me["display_name"]
    assert "channel" not in mine_row and "rebate" not in mine_row


def test_only_tiktok_is_offered_and_accepted(env: Env) -> None:
    """当前业务只做 TikTok：下拉只给 TikTok，写接口拒绝其它媒体。"""
    listing = env.client.get("/api/portal/open-requests", headers=env.h("client01"))
    assert listing.status_code == 200
    assert [p["code"] for p in listing.json()["options"]["platform"]] == ["TIKTOK"]

    portal = env.client.post(
        "/api/portal/open-requests",
        headers=env.h("client01"),
        json={"platform": "GOOGLE", "subject": "非 TikTok 主体", "apply_count": 1},
    )
    assert portal.status_code == 400
    assert "仅支持 TikTok" in portal.json()["detail"]

    desk = env.client.post(
        "/api/desk/open-requests",
        headers=env.h("ops01"),
        json={"client_id": env.ids["client_id"], "platform": "FB"},
    )
    assert desk.status_code == 400
    assert "仅支持 TikTok" in desk.json()["detail"]

    desk_enums = env.client.get("/api/desk/enums", headers=env.h("ops01")).json()
    assert [p["code"] for p in desk_enums["platform"]] == ["TIKTOK"]


def test_client_apply_duplicate_and_count_validation(env: Env) -> None:
    _client_apply(env, platform="TIKTOK", subject="重复主体测试")
    dup = env.client.post(
        "/api/portal/open-requests",
        headers=env.h("client01"),
        json={"platform": "TIKTOK", "subject": "重复主体测试", "apply_count": 1},
    )
    assert dup.status_code == 400
    assert "已有在办" in dup.json()["detail"]

    bad = env.client.post(
        "/api/portal/open-requests",
        headers=env.h("client01"),
        json={"platform": "TIKTOK", "subject": "数量非法", "apply_count": 21},
    )
    assert bad.status_code == 422

    blank = env.client.post(
        "/api/portal/open-requests",
        headers=env.h("client01"),
        json={"platform": "TIKTOK", "subject": "   ", "apply_count": 1},
    )
    assert blank.status_code == 400
    assert "开户主体" in blank.json()["detail"]


def test_client_application_materials_reach_desk_detail(env: Env) -> None:
    """客户填的企业信息/账户信息必须原样到管理端审批抽屉，不能只留在门户。"""
    item = _client_apply(
        env,
        platform="TIKTOK",
        subject="材料完整主体",
        credit_code="91440300TEST0001X",
        registered_place="CN",
        industry="Ecommerce",
        target_country="US",
        bc_ids=["BM-MAT-1", "BM-MAT-2"],
        product_link="https://example.com/landing",
    )
    det = env.client.get(
        f"/api/desk/open-requests/{item['id']}", headers=env.h("ops01")
    )
    assert det.status_code == 200, det.text
    got = det.json()["item"]
    assert got["credit_code"] == "91440300TEST0001X"
    assert got["registered_place"] == "CN"
    assert got["industry"] == "Ecommerce"
    assert got["target_country"] == "US"
    assert got["bc_ids"] == ["BM-MAT-1", "BM-MAT-2"]
    assert got["product_link"] == "https://example.com/landing"

    # 审批抽屉靠 enums 把代码翻成中文，这三个键必须真的下发
    enums = env.client.get("/api/desk/enums", headers=env.h("ops01")).json()
    assert {"code": "US", "label": "美国"} in enums["open_country"]
    assert {"code": "Ecommerce", "label": "电商"} in enums["open_industry"]
    assert {"code": "CN", "label": "中国大陆"} in enums["registered_place"]


def test_desk_reject_is_visible_to_client_and_writes_audit(env: Env) -> None:
    item = _client_apply(env, platform="TIKTOK", subject="驳回主体")
    resp = env.client.post(
        f"/api/desk/open-requests/{item['id']}/reject",
        headers=env.h("ops01"),
        json={"reason": "开户主体与营业执照不一致"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "REJECTED"

    mine = env.client.get("/api/portal/open-requests", headers=env.h("client01"))
    row = next(r for r in mine.json()["items"] if r["id"] == item["id"])
    assert row["status_label"] == "已驳回"
    assert row["reject_reason"] == "开户主体与营业执照不一致"
    assert row["handler"] == "钱七"
    assert row["processed_at"]

    with env.db() as s:
        log = s.execute(
            select(AuditLog).where(
                AuditLog.doctype == "open_request",
                AuditLog.doc_id == item["id"],
                AuditLog.action == "REJECT",
            )
        ).scalar_one()
        assert log.actor == "钱七"


def test_open_request_filters_and_manual_account_ids(env: Env) -> None:
    first = _client_apply(env, platform="TIKTOK", subject="筛选主体A", apply_count=1)
    second = _client_apply(env, platform="TIKTOK", subject="筛选主体B", apply_count=2)

    by_subject = env.client.get(
        "/api/desk/open-requests",
        headers=env.h("ops01"),
        params={"subject": "筛选主体B"},
    )
    assert by_subject.status_code == 200
    assert [r["id"] for r in by_subject.json()["items"]] == [second["id"]]

    by_no = env.client.get(
        "/api/desk/open-requests",
        headers=env.h("ops01"),
        params={"biz_no": first["biz_no"]},
    )
    assert [r["id"] for r in by_no.json()["items"]] == [first["id"]]

    manual = env.client.post(
        f"/api/desk/open-requests/{second['id']}/approve",
        headers=env.h("ops01"),
        json={
            "checks": ALL_CHECKS_BODY,
            "auto_complete": True,
            "account_ids": ["manual-1", "manual-2"],
            "remark": "人工开户",
        },
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["status"] == "OPENED"
    with env.db() as s:
        acc = s.execute(
            select(AdAccount).where(AdAccount.account_id == "manual-2")
        ).scalar_one()
        assert acc.client_id == env.ids["client_id"]

    mismatch = _client_apply(env, platform="TIKTOK", subject="人工数量不符", apply_count=2)
    bad = env.client.post(
        f"/api/desk/open-requests/{mismatch['id']}/approve",
        headers=env.h("ops01"),
        json={"checks": ALL_CHECKS_BODY, "auto_complete": True, "account_ids": ["only-one"]},
    )
    assert bad.status_code == 400
    assert "不一致" in bad.json()["detail"]

    # 客户不能访问后台，后台不能走门户开户接口
    assert env.client.get("/api/desk/open-requests", headers=env.h("client01")).status_code == 403
    assert env.client.get("/api/portal/open-requests", headers=env.h("ops01")).status_code == 403


# ---------------------------------------------------------------- 账户管理

def test_account_management_list_and_detail(env: Env) -> None:
    listing = env.client.get("/api/desk/ad-accounts", headers=env.h("finance01"))
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["account_id"] == "act_1001"
    assert item["client_name"] == "测试客户A"
    assert item["channel"] == "Yatop"
    assert item["authorized"] is True
    assert item["status_label"] == "正常"
    # 余额按币种分开，不跨币种求和
    assert body["summary"]["by_currency"] == [
        {"currency": "USD", "balance": "1000.0000", "count": 1}
    ]

    detail = env.client.get(
        f"/api/desk/ad-accounts/{env.ids['ad_account_id']}", headers=env.h("finance01")
    )
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["account"]["account_id"] == "act_1001"
    assert d["client"]["company_name"] == "测试客户A"
    assert d["wallets"][0]["available"] == "10000.0000"
    assert d["recharges"]["items"] == []
    assert d["recharge_summary"]["total"] == 0
    assert d["open_request"] is None

    assert env.client.get("/api/desk/ad-accounts/999999", headers=env.h("finance01")).status_code == 404
    assert env.client.get("/api/desk/ad-accounts", headers=env.h("client01")).status_code == 403


def test_account_management_filters_export_and_recharge_history(env: Env) -> None:
    created = env.client.post(
        "/api/desk/recharges",
        headers=env.h("finance01"),
        json={
            "ad_account_id": env.ids["ad_account_id"],
            "amount": "500",
            "request_no": "rc-account-test-1",
            "remark": "账户管理页充值",
        },
    )
    assert created.status_code == 200, created.text
    order = created.json()

    executed = env.client.post(
        f"/api/desk/recharges/{order['id']}/exec",
        headers=env.h("ops01"),
        json={"outcome": "SUCCESS"},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["account_balance"] == "1500.0000"

    detail = env.client.get(
        f"/api/desk/ad-accounts/{env.ids['ad_account_id']}", headers=env.h("finance01")
    ).json()
    assert detail["account"]["balance"] == "1500.0000"
    assert detail["recharges"]["total"] == 1
    assert detail["recharges"]["items"][0]["status"] == "SUCCESS"
    assert detail["recharge_summary"]["success_by_currency"] == [
        {"currency": "USD", "amount": "500.0000", "count": 1}
    ]
    assert detail["recharge_summary"]["last_success_at"]

    by_platform = env.client.get(
        "/api/desk/ad-accounts", headers=env.h("ops01"), params={"platform": "FB"}
    )
    assert by_platform.status_code == 200
    assert by_platform.json()["total"] == 1
    assert env.client.get(
        "/api/desk/ad-accounts", headers=env.h("ops01"), params={"platform": "GOOGLE"}
    ).json()["total"] == 0

    by_keyword = env.client.get(
        "/api/desk/ad-accounts", headers=env.h("finance01"), params={"keyword": "act_100"}
    )
    assert by_keyword.json()["total"] == 1
    by_client = env.client.get(
        "/api/desk/ad-accounts", headers=env.h("finance01"), params={"client_keyword": "测试客户"}
    )
    assert by_client.json()["total"] == 1

    exported = env.client.get("/api/desk/ad-accounts/export", headers=env.h("finance01"))
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert "act_1001" in exported.content.decode("utf-8-sig")

    bad = env.client.get(
        "/api/desk/ad-accounts", headers=env.h("finance01"), params={"platform": "META"}
    )
    assert bad.status_code == 400
