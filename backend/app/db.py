from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings


def build_engine(url: str, *, echo: bool = False) -> Engine:
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = {"echo": echo, "future": True}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    eng = create_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()

    return eng


engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)

# 流水表在库层面就是只追加的：连裸 SQL 都改不动
_TRIGGERS_SQLITE = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_wallet_tx_no_update
    BEFORE UPDATE ON wallet_transaction
    BEGIN SELECT RAISE(ABORT, 'wallet_transaction is append-only'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_wallet_tx_no_delete
    BEFORE DELETE ON wallet_transaction
    BEGIN SELECT RAISE(ABORT, 'wallet_transaction is append-only'); END;
    """,
]

_TRIGGERS_PG = [
    """
    CREATE OR REPLACE FUNCTION wallet_tx_append_only() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'wallet_transaction is append-only'; END;
    $$ LANGUAGE plpgsql;
    """,
    """
    DROP TRIGGER IF EXISTS trg_wallet_tx_no_update ON wallet_transaction;
    CREATE TRIGGER trg_wallet_tx_no_update BEFORE UPDATE ON wallet_transaction
    FOR EACH ROW EXECUTE FUNCTION wallet_tx_append_only();
    """,
    """
    DROP TRIGGER IF EXISTS trg_wallet_tx_no_delete ON wallet_transaction;
    CREATE TRIGGER trg_wallet_tx_no_delete BEFORE DELETE ON wallet_transaction
    FOR EACH ROW EXECUTE FUNCTION wallet_tx_append_only();
    """,
]


def install_guards(target_engine: Engine) -> None:
    stmts = _TRIGGERS_SQLITE if target_engine.dialect.name == "sqlite" else _TRIGGERS_PG
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def init_db(target_engine: Engine | None = None, *, create_all: bool = True) -> None:
    from .migrate import run_migrations
    from .models import Base  # 延迟导入，避免循环

    eng = target_engine or engine
    if create_all:
        Base.metadata.create_all(eng)
    run_migrations(eng)
    install_guards(eng)


def get_session() -> Session:
    return SessionLocal()
