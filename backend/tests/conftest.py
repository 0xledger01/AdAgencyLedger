from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db import build_engine, init_db
from backend.app.models import Client, ClientStatus, OwnerType
from backend.app.models.base import money
from backend.app.services.ledger import get_or_create_wallet


@pytest.fixture()
def engine(tmp_path: Path):  # noqa: ANN201
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    eng = build_engine(url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):  # noqa: ANN001, ANN201
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session: Session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(db: Session) -> Client:
    c = Client(
        company_name="测试客户A",
        settle_currency="USD",
        status=ClientStatus.ACTIVE,
        cost_ratio=money("0.9"),
        rebate_rate=money("0.05"),
    )
    db.add(c)
    db.flush()
    c.biz_no = f"C{c.id:04d}"
    db.flush()
    return c


@pytest.fixture()
def wallet(db: Session, client: Client):  # noqa: ANN201
    w = get_or_create_wallet(db, owner_type=OwnerType.CLIENT, owner_id=client.id, currency="USD")
    db.flush()
    return w


@pytest.fixture()
def funded(db: Session, wallet):  # noqa: ANN001, ANN201
    from backend.app.services.ledger import post

    post(
        db,
        wallet_id=wallet.id,
        direction="IN",
        amount=Decimal("10000"),
        biz_type="TOPUP",
        idempotency_key="test:fund:1",
        operator="tester",
    )
    db.flush()
    return wallet
