"""资金流水台账（运营口径）：一行一笔钱，按代理商 / 账单ID / 客户 / 交易类型 / 时间筛选。

和 `ledger_views.py` 的三视图的关系：
- 三视图按"客户 / 全局 / 账户消耗"拆开看，是财务的分析口径；
- 本模块是业务方定的**台账口径**：不拆 Tab，一张表 + 五个筛选，全部流水混排。
  代理商、广告账户这两列只对广告账户类流水有值（靠加款单反查），其余为 —。

铁律不变：
- 只读。不修改、不删除任何流水；
- 禁止跨币种求和，汇总一律按币种分开；
- 交易类型不是 biz_type 的同义词：同一个 biz_type 进/出是两种动作
  （充值 vs 减款、广告账户充值 vs 退款），运营按动作筛账。
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money
from ..models.client import AdAccount, Client
from ..models.docs import RechargeOrder
from ..models.enums import (
    BIZ_TYPE_LABEL,
    CLIENT_HIDDEN_BIZ_TYPES,
    BizType,
    Direction,
    OwnerType,
)
from ..models.wallet import Wallet, WalletTransaction
from .anonymize import display_name, display_text
from .ledger_views import LedgerViewError, _client_summary, _mark_reversed

REVERSE_PREFIX = "reverse:"

# code -> (biz_type, 真实业务方向, 中文)
TXN_TYPES: dict[str, tuple[str, str, str]] = {
    "CASH_IN": (BizType.TOPUP.value, Direction.IN.value, "现金账户充值"),
    "CASH_OUT": (BizType.REFUND.value, Direction.OUT.value, "现金账户减款"),
    "CASH_REFUND": (BizType.REFUND.value, Direction.IN.value, "现金账户退款"),
    "AD_TOPUP": (BizType.RECHARGE.value, Direction.OUT.value, "广告账户充值"),
    "AD_REFUND": (BizType.RECHARGE.value, Direction.IN.value, "广告账户退款"),
    "ADJUST_IN": (BizType.ADJUST.value, Direction.IN.value, "调账加款"),
    "ADJUST_OUT": (BizType.ADJUST.value, Direction.OUT.value, "调账扣款"),
    "TRANSFER_OUT": (BizType.TRANSFER.value, Direction.OUT.value, "转出一代"),
    "TRANSFER_IN": (BizType.TRANSFER.value, Direction.IN.value, "一代转入"),
    "REBATE": (BizType.REBATE.value, Direction.IN.value, "返点"),
    "REBATE_OUT": (BizType.REBATE.value, Direction.OUT.value, "返点冲回"),
    "SPEND": (BizType.SPEND.value, Direction.OUT.value, "消耗"),
}

TXN_TYPE_OPTIONS = [
    {"code": code, "label": label} for code, (_b, _d, label) in TXN_TYPES.items()
]


def reverse_of(tx: WalletTransaction) -> int | None:
    """冲正流水的幂等键是 `reverse:<原流水 id>`，原流水 id 从键里还原。"""
    key = tx.idempotency_key or ""
    if not key.startswith(REVERSE_PREFIX):
        return None
    try:
        return int(key[len(REVERSE_PREFIX):])
    except ValueError:
        return None


def _reverse_type_map(session: Session) -> dict[int, tuple[str, str]]:
    """冲正流水 id -> 根流水（一路 reverse 到最初那一笔）的 (biz_type, direction)。

    冲正可以再被冲正（把冲正撤回），这时方向不能只翻一次，要落到根流水上，
    否则第二层冲正会被归成"充值"这种不存在的动作。
    """
    rows = session.execute(
        select(WalletTransaction.id, WalletTransaction.idempotency_key).where(
            WalletTransaction.idempotency_key.like(f"{REVERSE_PREFIX}%")
        )
    ).all()
    edges: dict[int, int] = {}
    for tx_id, key in rows:
        try:
            edges[tx_id] = int(str(key)[len(REVERSE_PREFIX):])
        except (TypeError, ValueError):
            continue
    if not edges:
        return {}

    parents = set(edges.values())
    meta: dict[int, tuple[str, str]] = {
        tx_id: (str(biz), str(direction))
        for tx_id, biz, direction in session.execute(
            select(
                WalletTransaction.id,
                WalletTransaction.biz_type,
                WalletTransaction.direction,
            ).where(WalletTransaction.id.in_(parents))
        ).all()
    }

    out: dict[int, tuple[str, str]] = {}
    for tx_id in edges:
        cur, guard = tx_id, 0
        while cur in edges and guard < 20:
            cur = edges[cur]
            guard += 1
        if cur in meta:
            out[tx_id] = meta[cur]
    return out


def txn_type_of(
    tx: WalletTransaction, rev_map: dict[int, tuple[str, str]] | None = None
) -> tuple[str, str, bool]:
    """返回 (code, 中文, 是否冲正)。冲正按根流水那一笔的方向归动作。"""
    rev = reverse_of(tx)
    biz = str(tx.biz_type)
    direction = tx.direction
    if rev is not None:
        root = (rev_map or {}).get(tx.id)
        if root is not None:
            biz, direction = root[0], Direction(root[1])
        else:
            direction = Direction.OUT if direction is Direction.IN else Direction.IN
    for code, (b, d, label) in TXN_TYPES.items():
        if b == biz and d == str(direction):
            return code, label, rev is not None
    return biz, BIZ_TYPE_LABEL.get(tx.biz_type, biz), rev is not None


def _txn_type_cond(code: str, rev_map: dict[int, tuple[str, str]]):  # noqa: ANN202
    """交易类型筛选：正常流水按自身 (biz, direction)，冲正流水按根流水的 (biz, direction)。"""
    entry = TXN_TYPES.get((code or "").strip().upper())
    if entry is None:
        raise LedgerViewError(f"交易类型取值非法：{code}")
    biz, direction, _label = entry
    conds = [
        and_(
            WalletTransaction.biz_type == biz,
            WalletTransaction.direction == direction,
            ~WalletTransaction.idempotency_key.like(f"{REVERSE_PREFIX}%"),
        )
    ]
    rev_ids = [
        tx_id for tx_id, root in rev_map.items() if root == (biz, direction)
    ]
    if rev_ids:
        conds.append(WalletTransaction.id.in_(rev_ids))
    return or_(*conds)


def _flow_conds(
    *,
    agent: str | None,
    bill_no: str | None,
    client_keyword: str | None,
    txn_type: str | None,
    date_from: date | None,
    date_to: date | None,
    sales_owner: str | None,
    hide_rebate: bool,
    rev_map: dict[int, tuple[str, str]],
) -> list:
    conds: list = []
    if agent and agent.strip():
        # 代理商 = 账户归属渠道；没有账户级归属时退回客户归属渠道，
        # 这样筛 "Yatop" 能同时看到该渠道下的钱包流水与账户流水。
        conds.append(func.coalesce(AdAccount.channel, Client.channel) == agent.strip())
    if bill_no and bill_no.strip():
        conds.append(WalletTransaction.biz_no.like(f"%{bill_no.strip()}%"))
    if client_keyword and client_keyword.strip():
        like = f"%{client_keyword.strip()}%"
        conds.append(
            or_(
                Client.company_name.like(like),
                Client.biz_no.like(like),
                Client.crm_code.like(like),
            )
        )
    if txn_type:
        conds.append(_txn_type_cond(txn_type, rev_map))
    if date_from:
        conds.append(
            WalletTransaction.occurred_at
            >= datetime.combine(date_from, datetime.min.time())
        )
    if date_to:
        conds.append(
            WalletTransaction.occurred_at
            < datetime.combine(date_to, datetime.max.time())
        )
    if sales_owner:
        conds.append(Client.sales_owner == sales_owner)
    if hide_rebate:
        conds.append(
            WalletTransaction.biz_type.notin_(
                [b.value for b in CLIENT_HIDDEN_BIZ_TYPES]
            )
        )
    return conds


def _flow_stmt(*cols, conds: list):  # noqa: ANN002, ANN202
    """资金流水的公共 FROM/JOIN。

    广告账户 / 代理商只在广告账户类流水上有值：靠 recharge_order 反查。
    ref_doctype 必须一起判，否则 ref_id 会和别种单据的 id 串号。
    """
    return (
        select(*cols)
        .select_from(WalletTransaction)
        .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
        .outerjoin(
            Client,
            and_(Wallet.owner_type == OwnerType.CLIENT, Client.id == Wallet.owner_id),
        )
        .outerjoin(
            RechargeOrder,
            and_(
                WalletTransaction.ref_doctype == "recharge_order",
                RechargeOrder.id == WalletTransaction.ref_id,
            ),
        )
        .outerjoin(AdAccount, AdAccount.id == RechargeOrder.ad_account_id)
        .where(Wallet.owner_type == OwnerType.CLIENT, *conds)
    )


def _flow_item(
    tx: WalletTransaction,
    client: Client | None,
    account: AdAccount | None,
    rev_map: dict[int, tuple[str, str]],
) -> dict:
    code, label, is_reversal = txn_type_of(tx, rev_map)
    return {
        "id": tx.id,
        "biz_no": tx.biz_no,
        "client_id": client.id if client else None,
        "client_name": client.company_name if client else None,
        "client_no": client.biz_no if client else None,
        "agent": (account.channel if account else None)
        or (client.channel if client else None),
        "ad_account_pk": account.id if account else None,
        "ad_account_id": account.account_id if account else None,
        "txn_type": code,
        "txn_type_label": label,
        "is_reversal": is_reversal,
        "reverse_of": reverse_of(tx),
        "direction": str(tx.direction),
        "amount": str(money(tx.amount)),
        "currency": tx.currency,
        "balance_before": str(money(tx.balance_before)),
        "balance_after": str(money(tx.balance_after)),
        "remark": display_text(tx.remark),
        "operator": display_name(tx.operator),
        "occurred_at": tx.occurred_at.isoformat(sep=" ", timespec="seconds"),
    }


def fund_flow(
    session: Session,
    *,
    agent: str | None = None,
    bill_no: str | None = None,
    client_keyword: str | None = None,
    txn_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sales_owner: str | None = None,
    hide_rebate: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """资金流水台账：组合筛选 + 分页 + 按币种汇总。"""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), 10000)
    rev_map = _reverse_type_map(session)
    conds = _flow_conds(
        agent=agent,
        bill_no=bill_no,
        client_keyword=client_keyword,
        txn_type=txn_type,
        date_from=date_from,
        date_to=date_to,
        sales_owner=sales_owner,
        hide_rebate=hide_rebate,
        rev_map=rev_map,
    )

    base = _flow_stmt(WalletTransaction, Client, AdAccount, conds=conds)
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = session.execute(
        base.order_by(
            WalletTransaction.occurred_at.desc(), WalletTransaction.id.desc()
        )
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    items = _mark_reversed(
        session,
        [_flow_item(tx, client, account, rev_map) for tx, client, account in rows],
    )

    # 汇总按币种分开，禁止跨币种相加
    sum_rows = session.execute(
        _flow_stmt(
            WalletTransaction.currency,
            WalletTransaction.direction,
            func.coalesce(func.sum(WalletTransaction.amount), 0),
            func.count(),
            conds=conds,
        ).group_by(WalletTransaction.currency, WalletTransaction.direction)
    ).all()
    buckets: dict[str, dict] = {}
    for currency, direction, amount, count in sum_rows:
        bucket = buckets.setdefault(
            currency,
            {
                "currency": currency,
                "inflow": Decimal("0"),
                "outflow": Decimal("0"),
                "count": 0,
            },
        )
        if str(direction) == Direction.IN.value:
            bucket["inflow"] += money(amount)
        else:
            bucket["outflow"] += money(amount)
        bucket["count"] += int(count)
    per_currency = [
        {
            "currency": v["currency"],
            "inflow": str(money(v["inflow"])),
            "outflow": str(money(v["outflow"])),
            "net": str(money(v["inflow"] - v["outflow"])),
            "count": v["count"],
        }
        for v in sorted(buckets.values(), key=lambda x: x["currency"])
    ]

    # 只命中一个客户时，附带该客户的期初/期末（财务月度结账口径）。
    # 带交易类型筛选时不算：那不是客户账户的全口径。
    client_summary = None
    if client_keyword and client_keyword.strip() and not txn_type:
        crow = session.execute(
            _flow_stmt(
                Client.id, Client.company_name, Client.biz_no, conds=conds
            ).distinct()
        ).all()
        ids = {r[0] for r in crow}
        if len(ids) == 1:
            cid, cname, cno = crow[0]
            client_summary = {
                "client_id": cid,
                "client_name": cname,
                "client_no": cno,
                **_client_summary(
                    session,
                    client_id=cid,
                    currency=None,
                    date_from=date_from,
                    date_to=date_to,
                    hide_rebate=hide_rebate,
                ),
            }

    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "items": items,
        "txn_types": TXN_TYPE_OPTIONS,
        "summary": {"per_currency": per_currency, "client": client_summary},
    }


FUND_FLOW_COLUMNS = [
    ("id", "ID"),
    ("biz_no", "账单ID"),
    ("client_name", "客户名称"),
    ("client_no", "客户编号"),
    ("agent", "代理商"),
    ("ad_account_id", "广告账户ID"),
    ("txn_type_label", "交易类型"),
    ("amount", "交易金额"),
    ("currency", "币种"),
    ("balance_before", "交易前余额"),
    ("balance_after", "交易后余额"),
    ("remark", "备注"),
    ("occurred_at", "创建时间"),
]


def _export_rows(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        row = dict(it)
        if row.get("is_reversal"):
            row["txn_type_label"] = f"{row.get('txn_type_label') or ''}（冲正）"
        out.append(row)
    return out


def _row_values(row: dict) -> list:
    return [
        row.get(key) if row.get(key) is not None else ""
        for key, _label in FUND_FLOW_COLUMNS
    ]


def export_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in FUND_FLOW_COLUMNS])
    for row in _export_rows(items):
        writer.writerow(_row_values(row))
    return buf.getvalue().encode("utf-8-sig")


def export_xlsx(items: list[dict]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "资金流水"
    ws.append([label for _key, label in FUND_FLOW_COLUMNS])
    for row in _export_rows(items):
        ws.append(_row_values(row))
    widths = (8, 26, 30, 16, 12, 22, 16, 14, 8, 16, 16, 32, 20)
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
