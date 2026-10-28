"""公开接口：门户登录页的开户申请（无需登录）。

这里只收销售联系客户所需的基础信息，**不创建客户档案、不创建登录账号**。
管理端在 `/api/desk/client-leads` 查看并跟进，正式建档仍走客户管理。
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..services import leads as svc_leads
from ..services.errors import LedgerError
from .deps import DbSession

router = APIRouter(prefix="/api/public", tags=["公开申请"])


@router.get("/client-applications/options", response_model=dict, summary="开户申请公开选项")
def application_options() -> dict:
    return svc_leads.public_options()


class PublicApplicationIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    contact_name: str = Field(min_length=1, max_length=60)
    phone: str | None = Field(None, max_length=30)
    wechat: str | None = Field(None, max_length=60)
    email: str | None = Field(None, max_length=120)
    platform: str | None = Field(None, max_length=16)
    monthly_budget_expect: Decimal = Field(Decimal("0"), ge=0, le=Decimal("99999999"))
    remark: str | None = Field(None, max_length=1000)


@router.post(
    "/client-applications",
    response_model=dict,
    summary="提交开户申请（只留联系方式，销售跟进）",
)
def create_application(
    body: PublicApplicationIn, db: DbSession, request: Request
) -> dict:
    try:
        svc_leads.create_application(
            db,
            company_name=body.company_name,
            contact_name=body.contact_name,
            phone=body.phone,
            wechat=body.wechat,
            email=body.email,
            platform=body.platform,
            monthly_budget_expect=body.monthly_budget_expect,
            remark=body.remark,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except LedgerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    # 不返回线索 ID / 联系方式：公开接口不提供任何查询能力，避免被拿来探测手机号是否已提交。
    return {
        "ok": True,
        "message": "申请已提交，销售会在 1 个工作日内联系你",
    }
