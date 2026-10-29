from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
from .config import WEB_DIR, settings
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


# ---------------------------------------------------------------------------
# 前端静态托管（打包成 exe 后前后端同源，不再需要 5173 的 vite dev server）
#
# 注册顺序有硬性要求：
#   1. /assets、/static-web 的 Mount 放在所有 include_router 之后；
#   2. catch-all 的 GET 路由**必须是整个 app 最后注册的路由**（见文件末尾），
#      否则会把 /api/*、/docs、/openapi.json 全部吃掉。
# dev 下若 frontend/dist 不存在（还没 build），这里整体跳过，纯 API 模式照常可用。
# ---------------------------------------------------------------------------
_WEB_DIR: Path = WEB_DIR
_WEB_ENABLED: bool = bool(settings.serve_frontend) and (_WEB_DIR / "index.html").is_file()

if _WEB_ENABLED:
    # 打包后的 JS/CSS 在 web/assets 下；index.html 里引用的是绝对路径 /assets/xxx
    if (_WEB_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_WEB_DIR / "assets"), name="assets")
    # 兜底：/favicon.svg 之类的零散文件也能直接命中（不是必须，但少一层 fallback）
    app.mount("/static-web", StaticFiles(directory=_WEB_DIR), name="static-web")


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


# ---------------------------------------------------------------------------
# SPA fallback：前端是单页 + History API（/desk/ledger、/portal/wallet 等），
# 刷新深链接时后端必须回 index.html，否则 404。
#
# 三条硬约束：
#   1. 本路由必须是**全文最后一个**注册项 —— 之前注册的路由（含 /api/*、
#      /docs、/openapi.json、StaticFiles Mount）优先匹配；
#   2. api/ 前缀一律 404，绝不返回 HTML（否则前端拿到 HTML 当 JSON 解析全崩）；
#   3. 磁盘文件命中时 resolve() 后必须仍在 _WEB_DIR 之内，挡住 ../ 穿越。
# ---------------------------------------------------------------------------
if _WEB_ENABLED:

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """静态文件优先，其余路径回落到 index.html。"""
        normalized = (full_path or "").replace("\\", "/").strip("/")

        # 1) API 前缀不做 SPA 回落，交给 FastAPI 正常 404（保持 JSON 语义）
        if normalized == "api" or normalized.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        root = _WEB_DIR.resolve()
        # 2) 真实文件优先（favicon.svg、robots.txt 等）
        if normalized:
            candidate = (root / normalized).resolve()
            if (candidate == root or root in candidate.parents) and candidate.is_file():
                return FileResponse(candidate)

        # 3) 其余全部回落 index.html（含 /、/portal、/desk/ledger …）
        return FileResponse(root / "index.html")
