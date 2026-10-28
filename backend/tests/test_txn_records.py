"""交易记录（广告账户交易流水的只读投影）。

底线：这个页面只读，不冻结、不记账、不改任何余额。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import AdAccount, Platform, WalletTransaction
from backend.app.models.base import money
from backend.app.services import recharge, txn_records
from backend.app.services.errors import StateError


@pytest.fixture()
def account(db: Session, client):  # noqa: ANN001, ANN201
    acc = AdAccount(
        account_id="act_txn_1001",
        platform=Platform.FB,
        currency="USD",
        client_id=client.id,
        channel="Yatop",
        balance=money("500"),
        daily_limit=money("5000"),
    )
    db.add(acc)
    db.flush()
    return acc


def make_order(db: Session, account: AdAccount, *, amount: str = "12.34", request_no: str = "txn-test-1"):  # noqa: ANN001
    order, _deduped = recharge.create_recharge(
        db,
        ad_account_id=account.id,
        amount=amount,
        request_no=request_no,
        operator="发起人",
    )
    db.flush()
    return order


def only_item(db: Session, order_id: int, **filters) -> dict:  # noqa: ANN003
    data = txn_records.list_transactions(db, **filters)
    rows = [r for r in data["items"] if r["id"] == order_id]
    assert len(rows) == 1, f"交易记录里找不到加款单 {order_id}"
    return rows[0]


def test_create_recharge_stamps_stable_txn_no(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account)
    day = order.created_at.strftime("%Y%m%d")
    assert order.txn_no == f"ADTR{day}{order.id:06d}"
    assert order.txn_no.startswith("ADTR") and len(order.txn_no) == 18
    # 与业务单号分开，互不覆盖
    assert order.biz_no and order.biz_no.startswith("RC") and order.biz_no != order.txn_no


def test_list_projects_transaction_columns(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account)
    item = only_item(db, order.id)

    assert item["txn_no"] == order.txn_no
    assert item["biz_no"] == order.biz_no
    assert item["txn_type"] == "RECHARGE"
    assert item["txn_type_label"] == "广告账户充值"
    assert item["account_id"] == "act_txn_1001"
    assert item["amount"] == "12.3400"
    assert item["currency"] == "USD"
    assert item["status"] == "PENDING"
    assert item["status_label"] == "待开始"
    assert item["status_raw"] == "PENDING_EXEC"
    assert item["operator"] == "发起人"
    assert item["feedback"] is None
    assert item["occurred_at"]


def test_execute_success_updates_status_operator_and_feedback(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account)
    recharge.execute(db, order.id, outcome="SUCCESS", operator="执行员")
    db.flush()

    item = only_item(db, order.id)
    assert item["status"] == "DONE"
    assert item["status_label"] == "已完成"
    assert item["operator"] == "执行员"
    assert item["feedback"]
    # 成功路径仍然只加一次媒体侧余额
    assert money(account.balance) == money("512.34")


def test_rejected_txn_shows_platform_feedback(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account, amount="20")
    recharge.execute(db, order.id, outcome="REJECTED", operator="执行员")
    db.flush()

    item = only_item(db, order.id)
    assert item["status"] == "FAILED"
    assert item["status_label"] == "失败"
    assert "媒体侧拒绝" in (item["feedback"] or "")
    # 拒绝不解冻失败：冻结已释放，余额未被扣
    assert money(account.balance) == money("500")


def test_cancelled_txn_records_reason(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account, amount="5")
    recharge.cancel(db, order.id, reason="客户取消", operator="撤销员")
    db.flush()

    item = only_item(db, order.id)
    assert item["status"] == "CANCELLED"
    assert item["status_label"] == "已撤销"
    assert item["feedback"] == "已作废：客户取消"
    assert item["operator"] == "撤销员"


def test_filters_by_txn_biz_account_status_and_date(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001, ANN003
    first = make_order(db, account, amount="12.34", request_no="txn-filter-1")
    second_acc = AdAccount(
        account_id="act_txn_2002",
        platform=Platform.FB,
        currency="USD",
        client_id=account.client_id,
        channel="Yatop",
        balance=money("0"),
        daily_limit=money("5000"),
    )
    db.add(second_acc)
    db.flush()
    second = make_order(db, second_acc, amount="99", request_no="txn-filter-2")

    by_txn = txn_records.list_transactions(db, txn_no=first.txn_no)
    assert by_txn["total"] == 1 and by_txn["items"][0]["id"] == first.id

    by_biz = txn_records.list_transactions(db, txn_no=second.biz_no)
    assert by_biz["total"] == 1 and by_biz["items"][0]["id"] == second.id

    by_account = txn_records.list_transactions(db, account_keyword="2002")
    assert by_account["total"] == 1 and by_account["items"][0]["account_id"] == "act_txn_2002"

    pending = txn_records.list_transactions(db, status_f="PENDING")
    assert pending["total"] == 2
    assert txn_records.list_transactions(db, status_f="DONE")["total"] == 0

    today = first.updated_at.date()
    assert txn_records.list_transactions(db, date_from=today, date_to=today)["total"] == 2
    yesterday = today - timedelta(days=1)
    assert txn_records.list_transactions(db, date_to=yesterday)["total"] == 0

    with pytest.raises(StateError):
        txn_records.list_transactions(db, status_f="WHATEVER")
    with pytest.raises(StateError):
        txn_records.list_transactions(db, txn_type="REFUND")


def test_pagination_slices_stable_order(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    first = make_order(db, account, amount="1", request_no="txn-page-1")
    second = make_order(db, account, amount="2", request_no="txn-page-2")
    db.flush()

    page1 = txn_records.list_transactions(db, limit=1, offset=0)
    page2 = txn_records.list_transactions(db, limit=1, offset=1)
    assert page1["total"] == 2 and len(page1["items"]) == 1
    assert page2["total"] == 2 and len(page2["items"]) == 1
    # 最新的在前：同一条不会在两页里重复出现
    assert {page1["items"][0]["id"], page2["items"][0]["id"]} == {first.id, second.id}


def test_export_csv_uses_page_columns(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account)
    data = txn_records.list_transactions(db)
    text = txn_records.export_csv(data["items"]).decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0] == "交易编号,交易类型,广告账户ID,交易金额,币种,操作备注,状态,反馈信息,操作人,操作时间"
    assert order.txn_no in lines[1]
    assert "广告账户充值" in lines[1]


def test_list_is_read_only(db: Session, funded, account: AdAccount) -> None:  # noqa: ANN001
    order = make_order(db, account)
    wallet = funded
    before = (
        money(wallet.balance),
        money(wallet.frozen),
        money(account.balance),
        db.execute(select(func.count()).select_from(WalletTransaction)).scalar_one(),
    )

    for _ in range(2):
        txn_records.list_transactions(db)
        txn_records.export_csv(txn_records.list_transactions(db)["items"])

    db.flush()
    after = (
        money(wallet.balance),
        money(wallet.frozen),
        money(account.balance),
        db.execute(select(func.count()).select_from(WalletTransaction)).scalar_one(),
    )
    assert before == after
    assert order.status.value == "PENDING_EXEC"
