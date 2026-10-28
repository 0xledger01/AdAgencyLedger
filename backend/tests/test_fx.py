"""USD→CNY 报价与充值快照的单元测试（不走外网）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.app.models.base import money, utcnow
from backend.app.models.client import Client
from backend.app.models.docs import SystemSetting
from backend.app.services import fx
from backend.app.services.errors import InvalidAmount, RateChanged, RateUnavailable, StateError
from backend.app.services.topup import create_topup


def seed_rate(
    db: Session,
    rate: str = "7.1234",
    *,
    fetched_hours_ago: float = 0,
    source: str = "open.er-api.com",
) -> None:
    fetched = utcnow() - timedelta(hours=fetched_hours_ago)
    db.add(
        SystemSetting(
            key=fx.CACHE_KEY,
            value=json.dumps(
                {
                    "rate": rate,
                    "source": source,
                    "quoted_at": fetched.isoformat(timespec="seconds"),
                    "fetched_at": fetched.isoformat(timespec="seconds"),
                }
            ),
        )
    )
    db.flush()


def no_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx, "fetch_live", lambda: None)


# ------------------------------------------------------------------ 金额算法

def test_payable_cny_rounds_up_to_cent() -> None:
    # 100 × 7.1234 = 712.34 整
    assert fx.payable_cny("100", "7.1234") == money("712.34")
    # 1.01 × 7.1234 = 7.194634 → 向上到 7.20，避免到账不足
    assert fx.payable_cny("1.01", "7.1234") == money("7.20")
    # 0.01 × 7.0001 = 0.070001 → 0.08
    assert fx.payable_cny("0.01", "7.0001") == money("0.08")


def test_validate_amount_usd() -> None:
    assert fx.validate_amount_usd("100.00") == money("100")
    with pytest.raises(InvalidAmount):
        fx.validate_amount_usd("0")
    with pytest.raises(InvalidAmount):
        fx.validate_amount_usd("1.001")
    with pytest.raises(InvalidAmount):
        fx.validate_amount_usd("1000001")


# ------------------------------------------------------------------ 缓存与降级

def test_fresh_cache_avoids_network(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_rate(db)

    def boom() -> None:
        raise AssertionError("新鲜缓存不应请求外网")

    monkeypatch.setattr(fx, "fetch_live", boom)
    quote = fx.get_quote(db)
    assert quote.rate == money("7.1234")
    assert quote.stale is False


def test_stale_cache_used_when_provider_down(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_rate(db, fetched_hours_ago=3)
    no_live(monkeypatch)
    quote = fx.get_quote(db)
    assert quote.stale is True
    assert "分钟前" in (quote.warning or "")


def test_expired_cache_refuses_to_quote(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_rate(db, fetched_hours_ago=30)
    no_live(monkeypatch)
    with pytest.raises(RateUnavailable) as exc:
        fx.get_quote(db)
    assert exc.value.code == "FX_UNAVAILABLE"


def test_live_rate_is_persisted(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_live() -> fx.FxQuote:
        return fx.FxQuote(
            rate=money("7.2000"),
            source="open.er-api.com",
            quoted_at=utcnow(),
            fetched_at=utcnow(),
        )

    monkeypatch.setattr(fx, "fetch_live", fake_live)
    quote = fx.get_quote(db)
    assert quote.rate == money("7.2")
    cached = fx.get_quote(db)
    assert cached.rate == money("7.2")
    assert cached.source == "open.er-api.com"


def test_pboc_cache_refreshes_after_daily_publish(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = utcnow()
    db.add(
        SystemSetting(
            key=fx.CACHE_KEY,
            value=json.dumps(
                {
                    "rate": "6.7000",
                    "source": "中国外汇交易中心（央行中间价）",
                    # 上一次发布日（昨天 09:15）
                    "quoted_at": (
                        fx._pboc_publish_cutoff(now) - timedelta(days=1)
                    ).isoformat(timespec="seconds"),
                    # 40 分钟前刚取过：普通 TTL 还没到期，但已过 30 分钟重试间隔
                    "fetched_at": (now - timedelta(minutes=40)).isoformat(timespec="seconds"),
                }
            ),
        )
    )
    db.flush()

    def fake_live() -> fx.FxQuote:
        return fx.FxQuote(
            rate=money("6.7200"),
            source="中国外汇交易中心（央行中间价）",
            quoted_at=now,
            fetched_at=now,
        )

    monkeypatch.setattr(fx, "fetch_live", fake_live)
    quote = fx.get_quote(db)
    assert quote.rate == money("6.72")


def test_pboc_mid_parser() -> None:
    data = {
        "data": {
            "lastDate": "2026-10-28 9:15",
        },
        "records": [
            {"vrtEName": "EUR/CNY", "price": "7.8000"},
            {"vrtEName": "USD/CNY", "price": "6.7165"},
        ],
    }
    parsed = fx._from_pboc_mid(data)
    assert parsed is not None
    rate, quoted_at = parsed
    assert rate == money("6.7165")
    # 北京时间 9:15 → UTC 01:15
    assert quoted_at == datetime(2026, 10, 28, 1, 15)


# ------------------------------------------------------------------ 漂移校验

def test_rate_drift_rejected(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_rate(db, "7.0000")
    no_live(monkeypatch)
    # 0.1% 以内放行
    assert fx.resolve_order_quote(db, "100", "7.0070").rate == money("7")
    # 超过 0.2% 要求重新确认
    with pytest.raises(RateChanged) as exc:
        fx.resolve_order_quote(db, "100", "7.0200")
    assert exc.value.code == "RATE_CHANGED"


# ------------------------------------------------------------------ 单据快照

def test_topup_snapshots_cny_payable(db: Session, client: Client) -> None:
    t = create_topup(
        db,
        client_id=client.id,
        amount="100",
        currency="USD",
        exchange_rate="7.1234",
        payable_amount="712.34",
        payable_currency="CNY",
        rate_source="USD/CNY 7.1234 · open.er-api.com",
    )
    assert money(t.amount) == money("100")
    assert money(t.received_amount) == money("100")
    assert money(t.exchange_rate) == money("7.1234")
    assert money(t.payable_amount) == money("712.34")
    assert t.payable_currency == "CNY"
    assert t.rate_source and "open.er-api.com" in t.rate_source


def test_topup_default_stays_one_to_one(db: Session, client: Client) -> None:
    t = create_topup(db, client_id=client.id, amount="50", currency="USD")
    assert money(t.exchange_rate) == money("1")
    assert money(t.payable_amount) == money("50")
    assert t.payable_currency == "USD"


def test_same_currency_with_rate_rejected(db: Session, client: Client) -> None:
    with pytest.raises(StateError):
        create_topup(
            db,
            client_id=client.id,
            amount="100",
            currency="USD",
            exchange_rate="7.1",
            payable_amount="710",
            payable_currency="USD",
        )
