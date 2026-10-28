"""大额二次确认（DESIGN §8 默认 5000 USD）。

单独成模块的理由：`confirm_topup` 主体归 P3.5 窗口在改，这里只提供**前置判定**，
由 API 层在调用 confirm 之前拦一道，避免两边编辑同一个函数。

口径：
- 阈值优先取 `system_setting.large_amount_threshold_usd`，取不到回落 `config.settings`；
- 非 USD 币种按单据上的 `exchange_rate` 折 USD，**没有汇率就按 1:1 保守处理**（宁可多问一次）；
- 只拦"首次确认"，已到账的幂等重放不拦（`confirm_topup` 自己会直接返回）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.base import money
from ..models.client import ClientTopUp
from ..models.docs import SystemSetting
from ..models.enums import TopUpStatus

SETTING_KEY = "large_amount_threshold_usd"


def get_threshold(session: Session) -> Decimal:
    row = session.execute(
        select(SystemSetting).where(SystemSetting.key == SETTING_KEY)
    ).scalar_one_or_none()
    if row is not None and row.value not in (None, ""):
        try:
            return money(Decimal(str(row.value)))
        except Exception:  # noqa: BLE001 - 脏配置不阻断业务，回落默认值
            pass
    return money(Decimal(str(settings.large_amount_threshold_usd)))


def to_usd(amount: Decimal, currency: str, exchange_rate: Decimal | None) -> Decimal:
    if currency == "USD":
        return money(amount)
    rate = money(exchange_rate) if exchange_rate else Decimal("1")
    return money(amount * rate)


def check_large_amount(
    session: Session, topup: ClientTopUp, *, force: bool = False
) -> dict | None:
    """需要二次确认时返回提示体，否则返回 None。

    已到账的单据不拦：重复确认走幂等返回，不该再弹一次确认框。
    """
    if force or topup.status is TopUpStatus.RECEIVED:
        return None
    threshold = get_threshold(session)
    usd = to_usd(money(topup.amount), topup.currency, topup.exchange_rate)
    # 严格大于才拦：阈值本身（5000）视为常规额度，避免把日常单量全部拦成二次确认
    if usd <= threshold:
        return None
    return {
        "need_second_confirm": True,
        "threshold": str(money(threshold)),
        "amount_usd": str(money(usd)),
        "currency": "USD",
        "biz_no": topup.biz_no,
        "message": (
            f"本单折合 {money(usd)} USD，达到大额阈值 {money(threshold)} USD，"
            "请再次确认后重新提交（force=true）"
        ),
    }
