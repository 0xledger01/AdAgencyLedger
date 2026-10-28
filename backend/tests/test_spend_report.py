"""D6 消耗报表：GMV Max / BC 拆分、指标、分组、环比、币种隔离。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import AdAccount, Client, ClientStatus, Platform, SpendDaily
from backend.app.models.base import money
from backend.app.services import spend as svc
from backend.app.services.errors import StateError


def _account(
    db: Session,
    client: Client,
    account_id: str,
    *,
    platform: Platform = Platform.TIKTOK,
    currency: str = "USD",
    channel: str | None = "Yatop",
    balance: str = "10000",
) -> AdAccount:
    acc = AdAccount(
        account_id=account_id,
        platform=platform,
        currency=currency,
        client_id=client.id,
        channel=channel,
        balance=money(balance),
        daily_limit=money("5000"),
    )
    db.add(acc)
    db.flush()
    return acc


def _daily(
    db: Session,
    acc: AdAccount,
    day: date,
    spend: str,
    *,
    currency: str | None = None,
) -> SpendDaily:
    row = SpendDaily(
        date=day,
        ad_account_id=acc.id,
        spend=money(spend),
        currency=currency or acc.currency,
        exchange_rate=money("1"),
        base_spend=money(spend),
        source="TEST",
    )
    db.add(row)
    db.flush()
    return row


def test_legacy_import_maps_to_bc(db: Session, client: Client) -> None:
    acc = _account(db, client, "act_legacy")
    day = date.today() - timedelta(days=1)

    report = svc.import_spend(
        db,
        [{"date": day.isoformat(), "account_id": acc.account_id, "spend": "100"}],
    )
    db.flush()

    assert report["success"] == 1
    row = db.execute(select(SpendDaily)).scalar_one()
    assert row.spend == money("100")
    # 老格式只有总消耗：整体按 BC 口径，GMV Max 为 0
    assert row.bc_spend == money("100")
    assert row.bc_net_spend == money("100")
    assert row.bc_cash_spend == money("100")
    assert row.gmv_max_spend == money("0")
    assert acc.balance == money("9900")

    # 媒体后台的斜杠日期也要认
    slash_day = date.today() - timedelta(days=2)
    slash = svc.import_spend(
        db,
        [{"date": slash_day.strftime("%Y/%m/%d"), "account_id": acc.account_id, "spend": "1"}],
    )
    assert slash["success"] == 1


def test_import_breakdown_metrics(db: Session, client: Client) -> None:
    acc = _account(db, client, "act_breakdown")
    day = date.today() - timedelta(days=1)

    report = svc.import_spend(
        db,
        [
            {
                "date": day.isoformat(),
                "account_id": acc.account_id,
                "gmv_max_spend": "70",
                "gmv_max_net_spend": "65",
                "bc_spend": "30",
                "bc_net_spend": "28",
                "bc_cash_spend": "20",
                "bc_grant_spend": "10",
                "impressions": "10,000",
                "clicks": "200",
                "conversions": "10",
            }
        ],
    )
    assert report["success"] == 1

    data = svc.spend_report(db, date_from=day, date_to=day, group_by="client")
    ov = data["overview"]
    assert ov["gmv_bc_total"] == "100.00"
    assert ov["gmv_bc_net"] == "93.00"
    assert ov["gmv_total"] == "70.00"
    assert ov["bc_total"] == "30.00"
    assert ov["bc_cash"] == "20.00"
    assert ov["bc_grant"] == "10.00"
    assert ov["impressions"] == 10000
    assert ov["clicks"] == 200
    assert ov["conversions"] == 10
    assert ov["cpm"] == "3.0000"
    assert ov["ctr"] == "2.00"
    assert ov["cpc"] == "0.1500"

    row = data["rows"][0]
    assert row["client_id"] == client.id
    assert row["platform"] == "TIKTOK"
    assert row["bc"]["impressions"] == 10000
    assert row["bc"]["cvr"] == "5.00"
    assert data["top_clients"][0]["share_pct"] == 100.0
    assert data["totals"]["label"] == "合计"


def test_import_breakdown_only_and_mismatch(db: Session, client: Client) -> None:
    acc = _account(db, client, "act_split")
    day = date.today() - timedelta(days=2)

    ok = svc.import_spend(
        db,
        [{
            "date": day.isoformat(), "account_id": acc.account_id,
            "gmv_max_spend": "70", "bc_spend": "30",
        }],
    )
    assert ok["success"] == 1
    row = db.execute(select(SpendDaily)).scalar_one()
    assert row.spend == money("100")
    assert row.gmv_max_spend == money("70")
    assert row.bc_spend == money("30")

    bad = svc.import_spend(
        db,
        [{
            "date": (day - timedelta(days=1)).isoformat(), "account_id": acc.account_id,
            "spend": "100", "gmv_max_spend": "60", "bc_spend": "30",
        }],
    )
    assert bad["success"] == 0
    assert bad["failed"][0]["reason"] == "金额不一致"

    neg = svc.import_spend(
        db,
        [{
            "date": (day - timedelta(days=2)).isoformat(), "account_id": acc.account_id,
            "spend": "10", "impressions": "-1",
        }],
    )
    assert neg["failed"][0]["reason"] == "指标为负"


def test_report_grouping_mom_and_top(db: Session, client: Client) -> None:
    client.sales_owner = "孙东瑶"
    other = Client(
        company_name="测试客户B", settle_currency="USD", status=ClientStatus.ACTIVE,
        sales_owner="李四", crm_code="H100002",
    )
    db.add(other)
    db.flush()
    other.biz_no = f"C{other.id:04d}"
    a1 = _account(db, client, "act_a", platform=Platform.TIKTOK)
    b1 = _account(db, other, "act_b", platform=Platform.FB, channel="山恒")

    cur_from = date(2026, 1, 8)
    cur_to = date(2026, 1, 14)
    prev_from = date(2026, 1, 1)
    prev_to = date(2026, 1, 7)
    _daily(db, a1, cur_from, "60")
    _daily(db, a1, cur_from + timedelta(days=1), "40")
    _daily(db, a1, prev_from, "50")
    _daily(db, b1, cur_from, "300")
    _daily(db, b1, prev_from, "100")

    data = svc.spend_report(db, date_from=cur_from, date_to=cur_to, group_by="client")
    assert [r["label"] for r in data["rows"]] == ["测试客户B", "测试客户A"]
    assert data["rows"][1]["mom_pct"] == 100.0   # 50 -> 100
    assert data["rows"][0]["mom_pct"] == 200.0   # 100 -> 300
    assert data["overview"]["gmv_bc_total"] == "400.00"
    assert [t["client_name"] for t in data["top_clients"]] == ["测试客户B", "测试客户A"]
    assert data["top_clients"][0]["share_pct"] == 75.0
    # spark 与所选区间等长（7 天），按天回填
    assert len(data["rows"][0]["spark"]) == 7
    assert data["rows"][0]["spark"][0] == "300.0000"

    by_account = svc.spend_report(db, date_from=cur_from, date_to=cur_to, group_by="account")
    assert {r["label"] for r in by_account["rows"]} == {"act_a", "act_b"}
    # TOP 客户面板不随 Tab 变化：账户报表也要有
    assert [t["client_name"] for t in by_account["top_clients"]] == ["测试客户B", "测试客户A"]

    by_sales = svc.spend_report(db, date_from=cur_from, date_to=cur_to, group_by="sales")
    assert {r["label"] for r in by_sales["rows"]} == {"孙东瑶", "李四"}
    assert len(by_sales["top_clients"]) == 2

    by_agent = svc.spend_report(db, date_from=cur_from, date_to=cur_to, group_by="agent")
    assert {r["label"] for r in by_agent["rows"]} == {"Yatop", "山恒"}


def test_report_currency_scope(db: Session, client: Client) -> None:
    usd = _account(db, client, "act_usd", currency="USD", platform=Platform.TIKTOK)
    hkd = _account(db, client, "act_hkd", currency="HKD", platform=Platform.FB)
    day = date(2026, 2, 3)
    _daily(db, usd, day, "100", currency="USD")
    _daily(db, hkd, day, "800", currency="HKD")

    data = svc.spend_report(db, date_from=day, date_to=day, group_by="client")
    assert data["range"]["currencies"] == ["HKD", "USD"]
    assert data["range"]["currency"] == "HKD"       # 默认取消耗最大的币种
    assert data["overview"]["gmv_bc_total"] == "800.00"

    only_usd = svc.spend_report(db, date_from=day, date_to=day, group_by="client", currency="USD")
    assert only_usd["range"]["currency"] == "USD"
    assert only_usd["overview"]["gmv_bc_total"] == "100.00"
    assert len(only_usd["rows"]) == 1
    # 没有跨币种求和：USD 视图里看不到 800
    assert all(r["currency"] == "USD" for r in only_usd["rows"])


def test_report_daily_mode_and_validation(db: Session, client: Client) -> None:
    acc = _account(db, client, "act_daily")
    day = date(2026, 3, 1)
    _daily(db, acc, day, "10")
    _daily(db, acc, day + timedelta(days=1), "20")

    data = svc.spend_report(
        db, date_from=day, date_to=day + timedelta(days=1), group_by="client", mode="daily"
    )
    assert len(data["rows"]) == 2
    assert [r["date"] for r in data["rows"]] == ["2026-03-02", "2026-03-01"]
    assert all(r["mom_pct"] is None for r in data["rows"])
    assert all(r["spark"] == [] for r in data["rows"])

    with pytest.raises(StateError):
        svc.spend_report(db, date_from=day, date_to=day - timedelta(days=1))
    with pytest.raises(StateError):
        svc.spend_report(db, date_from=day, date_to=day, group_by="nope")
    with pytest.raises(StateError):
        svc.spend_report(db, date_from=day, date_to=day, mode="nope")


def test_report_csv_has_headers_and_total(db: Session, client: Client) -> None:
    acc = _account(db, client, "act_csv")
    _daily(db, acc, date(2026, 4, 1), "12.5")
    data = svc.spend_report(db, date_from=date(2026, 4, 1), date_to=date(2026, 4, 1))
    text = svc.report_csv(data)
    lines = text.strip().splitlines()
    assert "GMV Max+BC总消耗" in lines[0]
    assert lines[-1].startswith(",合计,合计")
    assert "12.50" in text
