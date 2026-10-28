from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import auth as api_auth
from .api import desk as api_desk
from .api import desk_accounts as api_desk_accounts
from .api import desk_bcid as api_desk_bcid
from .api import desk_fund_app as api_desk_fund_app
from .api import desk_leads as api_desk_leads
from .api import desk_open as api_desk_open
from .api import desk_recharge as api_desk_recharge
from .api import desk_spend as api_desk_spend
from .api import desk_tickets as api_desk_tickets
from .api import desk_topup as api_desk_topup
from .api import files as api_files
from .api import portal as api_portal
from .api import portal_accounts as api_portal_accounts
from .api import portal_bcid as api_portal_bcid
from .api import portal_transactions as api_portal_transactions
from .api import public as api_public
from .config import settings
from .db import SessionLocal, engine, init_db
from .seed import DEMO_PENDING_CLIENTS, seed
from .services.errors import LedgerError


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    init_db(engine)
    if settings.seed_on_startup:
        with SessionLocal() as session:
            # seed 返回 False 时也可能补了账号 / 演示单据（升级路径），统一提交
            seed(session)
            session.commit()
    yield


app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_auth.router)
app.include_router(api_desk.router)
app.include_router(api_desk_accounts.router)
app.include_router(api_desk_bcid.router)
# P3：充值管理 / 开户管理 / 加款任务 / 消耗导入（都挂在 /api/desk 前缀下）
app.include_router(api_desk_topup.router)
app.include_router(api_desk_fund_app.router)
app.include_router(api_desk_open.router)
app.include_router(api_desk_recharge.router)
app.include_router(api_desk_spend.router)
app.include_router(api_desk_tickets.router)
app.include_router(api_desk_leads.router)
app.include_router(api_portal.router)
app.include_router(api_portal_accounts.router)
app.include_router(api_portal_bcid.router)
app.include_router(api_portal_transactions.router)
app.include_router(api_files.router)
app.include_router(api_public.router)


_ERROR_STATUS = {
    "AUTH_ERROR": 401,
    "PERMISSION_DENIED": 403,
    "DIRECT_BALANCE_MUTATION": 409,
    "LEDGER_IMMUTABLE": 409,
    "IMMUTABLE_VIOLATION": 409,
    "INSUFFICIENT_BALANCE": 400,
    "INVALID_AMOUNT": 400,
    "INVALID_FILE": 400,
    "STATE_ERROR": 400,
    "REVIEW_ERROR": 400,
    "IDEMPOTENCY_CONFLICT": 409,
    "LEDGER_CHAIN_BROKEN": 500,
    "RATE_CHANGED": 409,
    "FX_UNAVAILABLE": 503,
}


@app.exception_handler(LedgerError)
async def ledger_error_handler(_request: Request, exc: LedgerError) -> JSONResponse:
    code = str(getattr(exc, "code", "LEDGER_ERROR"))
    return JSONResponse(
        status_code=_ERROR_STATUS.get(code, 400),
        content={"detail": str(exc), "code": code},
    )


@app.get("/api/health", tags=["系统"])
def health() -> dict:
    return {"ok": True, "app": settings.app_name, "db": engine.dialect.name, "version": "0.2.0"}


@app.get("/api/dev/wallets/{wallet_id}/verify", tags=["开发工具"])
def verify_chain(wallet_id: int) -> dict:
    from .services.ledger import verify_wallet_chain, wallet_summary

    # 与 reset-demo 一致：非调试环境不得暴露账本细节（F-26）
    if not settings.debug:
        raise RuntimeError("仅调试模式可用")
    with SessionLocal() as db:
        count = verify_wallet_chain(db, wallet_id)
        return {"wallet": wallet_summary(db, wallet_id), "chain_ok": True, "tx_count": count}


@app.post("/api/dev/reset-demo", tags=["开发工具"])
def reset_demo() -> dict:
    """把演示客户重置回审核起点。只清审核记录，绝不动流水（流水不可删）。"""
    from sqlalchemy import delete

    from .models import Client, ClientReview, ClientStatus

    if not settings.debug:
        raise RuntimeError("仅调试模式可用")

    with SessionLocal() as db:
        # 演示客户的公司名统一脱敏成「某某公司」，按统一社会信用代码定位这两个客户
        codes = [c[1] for c in DEMO_PENDING_CLIENTS]
        demo = (
            db.query(Client)
            .filter(Client.credit_code.in_(codes))
            .order_by(Client.id)
            .all()
        )
        if len(demo) < 2:
            return {"ok": False, "message": "演示客户不存在"}

        reset_status = {row[1]: row[4] for row in DEMO_PENDING_CLIENTS}
        for c in demo:
            db.execute(delete(ClientReview).where(ClientReview.client_id == c.id))
            c.status = reset_status.get(c.credit_code or "", ClientStatus.PENDING_REVIEW)
            c.reject_reason = None
            c.activated_at = None
        db.commit()
        return {
            "ok": True,
            "clients": [{"id": c.id, "name": c.company_name, "status": str(c.status)} for c in demo],
        }
