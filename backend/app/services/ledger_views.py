"""D3.1 客户资金流水三视图 + 汇总恒等式 + 导出 + 核销。

三个视图：
- 客户往来流水（按客户）—— 这客户充了多少、用完多少、还剩多少
- 全量资金流水（全局时间序）—— 财务日常核对、审计
- 账户消耗流水（按广告账户）—— 这笔钱花在哪个账户、哪天花的

恒等式：期初 + 本期进 − 本期出 = 期末
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money
from ..models.client import AdAccount, Client, SpendDaily
from ..models.enums import CLIENT_HIDDEN_BIZ_TYPES, BIZ_TYPE_LABEL, BizType, Direction, OwnerType
from ..models.user import LedgerReconcile, User
from ..models.wallet import Wallet, WalletTransaction
from .anonymize import display_name, display_text, name_variants
from .errors import InvalidAmount, LedgerError
from .ledger import post, require_wallet


class LedgerViewError(LedgerError):
    code = "LEDGER_VIEW_ERROR"


FILTERABLE_BIZ = {b.value for b in BizType}

# 门户流水状态：账本只追加，所以一条流水要么已完成，要么被红字冲正。
# 这里不引入新状态字段，避免把"事实"写成可变的单据状态。
LEDGER_STATUS_LABEL = {"DONE": "已完成", "REVERSED": "已冲正"}


def _base_query(currency: str | None, biz_type: str | None, direction: str | None,
                date_from: date | None, date_to: date | None,
                amount_min: Decimal | None, amount_max: Decimal | None,
                payer: str | None, operator: str | None,
                biz_no: str | None = None,
                owner_type: OwnerType = OwnerType.CLIENT) -> Select:
    stmt = (
        select(WalletTransaction, Wallet, Client)
        .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
        .outerjoin(Client, and_(Wallet.owner_type == OwnerType.CLIENT, Client.id == Wallet.owner_id))
        .where(Wallet.owner_type == owner_type)
    )
    if currency:
        stmt = stmt.where(WalletTransaction.currency == currency)
    if biz_type:
        stmt = stmt.where(WalletTransaction.biz_type == biz_type)
    if direction:
        stmt = stmt.where(WalletTransaction.direction == direction)
    if date_from:
        stmt = stmt.where(WalletTransaction.occurred_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        stmt = stmt.where(WalletTransaction.occurred_at < datetime.combine(date_to, datetime.max.time()))
    if amount_min is not None:
        stmt = stmt.where(WalletTransaction.amount >= money(amount_min))
    if amount_max is not None:
        stmt = stmt.where(WalletTransaction.amount <= money(amount_max))
    if payer:
        stmt = stmt.where(or_(*[
            WalletTransaction.payer.like(f"%{v}%") for v in name_variants(payer)
        ]))
    if operator:
        stmt = stmt.where(or_(*[
            WalletTransaction.operator.like(f"%{v}%") for v in name_variants(operator)
        ]))
    if biz_no and biz_no.strip():
        stmt = stmt.where(WalletTransaction.biz_no.like(f"%{biz_no.strip()}%"))
    return stmt


def _serialize(tx: WalletTransaction, wallet: Wallet, client: Client | None,
               hide_rebate: bool) -> dict:
    """白名单序列化。客户侧绝不出现 rebate 相关字段。"""
    biz = str(tx.biz_type)
    if hide_rebate and tx.biz_type in CLIENT_HIDDEN_BIZ_TYPES:
        biz_label = "账户调整"
    else:
        biz_label = BIZ_TYPE_LABEL.get(tx.biz_type, biz)

    row = {
        "id": tx.id,
        "occurred_at": tx.occurred_at.isoformat(sep=" ", timespec="seconds"),
        "client_id": wallet.owner_id if wallet.owner_type is OwnerType.CLIENT else None,
        "client_name": client.company_name if client else None,
        "client_no": client.biz_no if client else None,
        "biz_type": biz,
        "biz_type_label": biz_label,
        "direction": str(tx.direction),
        "amount": str(money(tx.amount)),
        "currency": tx.currency,
        "exchange_rate": str(money(tx.exchange_rate)),
        "base_amount": str(money(tx.base_amount)),
        "balance_before": str(money(tx.balance_before)),
        "balance_after": str(money(tx.balance_after)),
        # 历史账本不允许改，真人名只在读取时脱敏
        "payer": display_name(tx.payer),
        "voucher": tx.voucher,
        "biz_no": tx.biz_no,
        "operator": display_name(tx.operator),
        "remark": display_text(tx.remark),
        "ref_doctype": tx.ref_doctype,
        "ref_id": tx.ref_id,
    }
    return row


def _reversed_ids(session: Session, tx_ids: list[int]) -> set[int]:
    """找出已被冲正的流水 id（靠 reverse:<id> 幂等键反查）。"""
    if not tx_ids:
        return set()
    keys = session.execute(
        select(WalletTransaction.idempotency_key).where(
            WalletTransaction.idempotency_key.in_([f"reverse:{i}" for i in tx_ids])
        )
    ).scalars().all()
    out: set[int] = set()
    for k in keys:
        try:
            out.add(int(str(k).split(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    return out


def _reversal_map(session: Session, tx_ids: list[int]) -> dict[int, str]:
    """已被冲正的流水 → 冲正说明。靠 `reverse:<id>` 幂等键反查，不改原流水。"""
    if not tx_ids:
        return {}
    rows = session.execute(
        select(WalletTransaction.idempotency_key, WalletTransaction.remark).where(
            WalletTransaction.idempotency_key.in_([f"reverse:{i}" for i in tx_ids])
        )
    ).all()
    out: dict[int, str] = {}
    for key, remark in rows:
        try:
            out[int(str(key).split(":", 1)[1])] = display_text(remark) or ""
        except (IndexError, ValueError):
            continue
    return out


def _mark_reversed(session: Session, items: list[dict]) -> list[dict]:
    marks = _reversal_map(session, [it["id"] for it in items])
    for it in items:
        it["reversed"] = it["id"] in marks
        it["reverse_remark"] = marks.get(it["id"])
    return items


def client_ledger(
    session: Session,
    *,
    client_id: int,
    currency: str | None = None,
    biz_type: str | None = None,
    direction: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    payer: str | None = None,
    operator: str | None = None,
    biz_no: str | None = None,
    status: str | None = None,
    hide_rebate: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """客户往来流水 + 汇总。期初取筛选区间之前最后一条 balance_after。"""
    stmt = _base_query(
        currency, biz_type, direction, date_from, date_to,
        amount_min, amount_max, payer, operator, biz_no, OwnerType.CLIENT,
    ).where(Wallet.owner_id == client_id)

    if hide_rebate:
        stmt = stmt.where(WalletTransaction.biz_type.notin_([b.value for b in CLIENT_HIDDEN_BIZ_TYPES]))

    # 状态是按"是否出现 reverse:<id>"推出来的，不是流水列，只能先取 id 集合再过滤。
    if status:
        key = status.strip().upper()
        if key not in LEDGER_STATUS_LABEL:
            raise LedgerViewError(f"状态取值非法：{status}")
        reversed_ids = [
            int(str(k).split(":", 1)[1])
            for (k,) in session.execute(
                select(WalletTransaction.idempotency_key).where(
                    WalletTransaction.idempotency_key.like("reverse:%")
                )
            ).all()
            if str(k).split(":", 1)[-1].isdigit()
        ]
        if key == "REVERSED":
            stmt = stmt.where(WalletTransaction.id.in_(reversed_ids or [-1]))
        else:
            stmt = stmt.where(WalletTransaction.id.notin_(reversed_ids or [-1]))

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(WalletTransaction.occurred_at.desc(), WalletTransaction.id.desc())
        .limit(limit).offset(offset)
    ).all()

    items = _mark_reversed(session, [_serialize(tx, w, c, hide_rebate) for tx, w, c in rows])

    summary = _client_summary(
        session, client_id=client_id, currency=currency, date_from=date_from, date_to=date_to,
        biz_type=biz_type, hide_rebate=hide_rebate,
    )
    return {"total": int(total), "items": items, "summary": summary}


def _client_summary(
    session: Session,
    *,
    client_id: int,
    currency: str | None,
    date_from: date | None,
    date_to: date | None,
    biz_type: str | None = None,
    hide_rebate: bool = False,
) -> dict:
    wallets = session.execute(
        select(Wallet).where(Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client_id)
    ).scalars().all()
    if currency:
        wallets = [w for w in wallets if w.currency == currency]

    per_currency: dict[str, dict] = {}
    for w in wallets:
        opening = _opening_balance(session, w.id, date_from)
        q = select(WalletTransaction).where(WalletTransaction.wallet_id == w.id)
        if date_from:
            q = q.where(WalletTransaction.occurred_at >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            q = q.where(WalletTransaction.occurred_at < datetime.combine(date_to, datetime.max.time()))
        if biz_type:
            q = q.where(WalletTransaction.biz_type == biz_type)
        if hide_rebate:
            q = q.where(WalletTransaction.biz_type.notin_([b.value for b in CLIENT_HIDDEN_BIZ_TYPES]))

        txs = session.execute(q.order_by(WalletTransaction.seq)).scalars().all()
        inflow = sum((money(t.amount) for t in txs if t.direction is Direction.IN), Decimal("0"))
        outflow = sum((money(t.amount) for t in txs if t.direction is Direction.OUT), Decimal("0"))
        closing = money(opening + inflow - outflow)

        # 恒等式 vs 实际末笔余额（同一筛选条件下应一致）
        actual_closing = money(txs[-1].balance_after) if txs else opening
        conflicts = money(closing) != money(actual_closing)

        per_currency[w.currency] = {
            "currency": w.currency,
            "opening": str(money(opening)),
            "inflow": str(money(inflow)),
            "outflow": str(money(outflow)),
            "closing": str(closing),
            "actual_closing": str(actual_closing),
            "balanced": not conflicts,
            "tx_count": len(txs),
        }

    pending = _pending_reconcile_count(session, client_id, date_from, date_to)

    return {
        "per_currency": list(per_currency.values()),
        "has_conflict": any(not v["balanced"] for v in per_currency.values()),
        "pending_reconcile": pending,
    }


def _opening_balance(session: Session, wallet_id: int, date_from: date | None) -> Decimal:
    if not date_from:
        return Decimal("0")
    boundary = datetime.combine(date_from, datetime.min.time())
    last = session.execute(
        select(WalletTransaction.balance_after)
        .where(WalletTransaction.wallet_id == wallet_id, WalletTransaction.occurred_at < boundary)
        .order_by(WalletTransaction.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    return money(last) if last is not None else Decimal("0")


def _pending_reconcile_count(session: Session, client_id: int, date_from: date | None,
                             date_to: date | None) -> dict:
    """未核销的流水笔数（财务月度结账用）。"""
    q = (
        select(func.count())
        .select_from(WalletTransaction)
        .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
        .outerjoin(LedgerReconcile, LedgerReconcile.tx_id == WalletTransaction.id)
        .where(Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client_id,
               LedgerReconcile.id.is_(None), WalletTransaction.direction == Direction.IN)
    )
    if date_from:
        q = q.where(WalletTransaction.occurred_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q = q.where(WalletTransaction.occurred_at < datetime.combine(date_to, datetime.max.time()))
    count = session.execute(q).scalar_one()

    amt_q = (
        select(func.coalesce(func.sum(WalletTransaction.amount), 0))
        .select_from(WalletTransaction)
        .join(Wallet, Wallet.id == WalletTransaction.wallet_id)
        .outerjoin(LedgerReconcile, LedgerReconcile.tx_id == WalletTransaction.id)
        .where(Wallet.owner_type == OwnerType.CLIENT, Wallet.owner_id == client_id,
               LedgerReconcile.id.is_(None), WalletTransaction.direction == Direction.IN)
    )
    if date_from:
        amt_q = amt_q.where(WalletTransaction.occurred_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        amt_q = amt_q.where(WalletTransaction.occurred_at < datetime.combine(date_to, datetime.max.time()))
    amount = session.execute(amt_q).scalar_one()
    return {"count": int(count), "amount": str(money(amount))}


def global_ledger(
    session: Session,
    *,
    client_id: int | None = None,
    sales_owner: str | None = None,
    **filters,
) -> dict:
    """全量资金流水（财务主视图）。可按客户、业务类型、时间等任意组合筛选。"""
    limit = filters.pop("limit", 50)
    offset = filters.pop("offset", 0)
    hide_rebate = filters.pop("hide_rebate", False)

    stmt = _base_query(owner_type=OwnerType.CLIENT, **filters)
    if client_id:
        stmt = stmt.where(Wallet.owner_id == client_id)
    if sales_owner:
        stmt = stmt.where(Client.sales_owner == sales_owner)
    if hide_rebate:
        stmt = stmt.where(WalletTransaction.biz_type.notin_([b.value for b in CLIENT_HIDDEN_BIZ_TYPES]))

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(WalletTransaction.occurred_at.desc(), WalletTransaction.id.desc())
        .limit(limit).offset(offset)
    ).all()

    items = _mark_reversed(session, [_serialize(tx, w, c, hide_rebate) for tx, w, c in rows])

    # 全量汇总按币种
    sum_stmt = _base_query(owner_type=OwnerType.CLIENT, **filters)
    if client_id:
        sum_stmt = sum_stmt.where(Wallet.owner_id == client_id)
    if sales_owner:
        sum_stmt = sum_stmt.where(Client.sales_owner == sales_owner)
    all_rows = session.execute(sum_stmt).all()

    per_currency: dict[str, dict] = {}
    for tx, w, _c in all_rows:
        bucket = per_currency.setdefault(
            tx.currency, {"currency": tx.currency, "inflow": Decimal("0"), "outflow": Decimal("0"), "count": 0}
        )
        if tx.direction is Direction.IN:
            bucket["inflow"] += money(tx.amount)
        else:
            bucket["outflow"] += money(tx.amount)
        bucket["count"] += 1

    return {
        "total": int(total),
        "items": items,
        "summary": {
            "per_currency": [
                {**v, "inflow": str(money(v["inflow"])), "outflow": str(money(v["outflow"])),
                 "net": str(money(v["inflow"] - v["outflow"]))}
                for v in per_currency.values()
            ]
        },
    }


def account_ledger(
    session: Session,
    *,
    client_id: int | None = None,
    client_ids: list[int] | None = None,
    ad_account_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """账户消耗流水：这笔钱花在哪个账户、哪天花的。

    client_ids 用于销售角色（只能看自己名下客户）：传入白名单集合，避免逐客户循环查询。
    """
    stmt = (
        select(SpendDaily, AdAccount, Client)
        .join(AdAccount, AdAccount.id == SpendDaily.ad_account_id)
        .join(Client, Client.id == AdAccount.client_id)
    )
    if client_id:
        stmt = stmt.where(AdAccount.client_id == client_id)
    if client_ids is not None:
        stmt = stmt.where(AdAccount.client_id.in_(client_ids))
    if ad_account_id:
        stmt = stmt.where(SpendDaily.ad_account_id == ad_account_id)
    if date_from:
        stmt = stmt.where(SpendDaily.date >= date_from)
    if date_to:
        stmt = stmt.where(SpendDaily.date <= date_to)

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(SpendDaily.date.desc(), SpendDaily.id.desc()).limit(limit).offset(offset)
    ).all()

    items = [
        {
            "id": s.id,
            "date": s.date.isoformat(),
            "client_id": c.id,
            "client_name": c.company_name,
            "ad_account_id": a.id,
            "account_id": a.account_id,
            "platform": str(a.platform),
            "currency": s.currency,
            "spend": str(money(s.spend)),
            "base_spend": str(money(s.base_spend)),
            "source": s.source,
        }
        for s, a, c in rows
    ]

    sum_stmt = (
        select(SpendDaily.currency, func.coalesce(func.sum(SpendDaily.spend), 0), func.count())
        .join(AdAccount, AdAccount.id == SpendDaily.ad_account_id)
        .group_by(SpendDaily.currency)
    )
    if client_id:
        sum_stmt = sum_stmt.where(AdAccount.client_id == client_id)
    if client_ids is not None:
        sum_stmt = sum_stmt.where(AdAccount.client_id.in_(client_ids))
    if date_from:
        sum_stmt = sum_stmt.where(SpendDaily.date >= date_from)
    if date_to:
        sum_stmt = sum_stmt.where(SpendDaily.date <= date_to)
    summary = [
        {"currency": ccy, "spend": str(money(total_spend)), "days": int(cnt)}
        for ccy, total_spend, cnt in session.execute(sum_stmt).all()
    ]

    return {"total": int(total), "items": items, "summary": {"per_currency": summary}}


# ------------------------------------------------------------------ 核销

def mark_reconciled(
    session: Session, *, tx_ids: list[int], period: str, operator: User, remark: str | None = None
) -> dict:
    """标记核销。不改流水本身（流水不可改），单独记一条核销事实。"""
    if not tx_ids:
        raise InvalidAmount("请至少选择一条流水")
    if not (len(period) == 7 and period[4] == "-"):
        raise LedgerViewError("period 格式应为 YYYY-MM")

    existing = {
        r for (r,) in session.execute(
            select(LedgerReconcile.tx_id).where(LedgerReconcile.tx_id.in_(tx_ids))
        ).all()
    }
    created = 0
    for tx_id in tx_ids:
        if tx_id in existing:
            continue
        tx = session.get(WalletTransaction, tx_id)
        if tx is None:
            continue
        session.add(
            LedgerReconcile(
                tx_id=tx_id, period=period, reconciled_by=operator.display_name, remark=remark
            )
        )
        created += 1
    session.flush()
    return {"created": created, "skipped": len(tx_ids) - created}


def reconcile_status(session: Session, tx_ids: list[int]) -> dict[int, str | None]:
    if not tx_ids:
        return {}
    rows = session.execute(
        select(LedgerReconcile.tx_id, LedgerReconcile.period).where(LedgerReconcile.tx_id.in_(tx_ids))
    ).all()
    return {tx_id: period for tx_id, period in rows}


# ------------------------------------------------------------------ 冲正

def reverse_tx(session: Session, *, tx_id: int, operator: User, reason: str) -> WalletTransaction:
    """只能新增反向流水，不允许删除或修改历史。"""
    if not reason or not reason.strip():
        raise LedgerViewError("冲正必须填写原因")
    tx = session.get(WalletTransaction, tx_id)
    if tx is None:
        raise LedgerViewError(f"流水不存在（id={tx_id}）")

    existing = session.execute(
        select(WalletTransaction).where(WalletTransaction.idempotency_key == f"reverse:{tx.id}")
    ).scalar_one_or_none()
    if existing is not None:
        raise LedgerViewError(f"该流水已冲正过（冲正流水 #{existing.id}），不得重复冲正")

    require_wallet(session, tx.wallet_id)
    opposite = Direction.OUT if tx.direction is Direction.IN else Direction.IN
    new_tx, _ = post(
        session,
        wallet_id=tx.wallet_id,
        direction=opposite,
        amount=tx.amount,
        biz_type=tx.biz_type,
        idempotency_key=f"reverse:{tx.id}",
        ref_doctype=tx.ref_doctype,
        ref_id=tx.ref_id,
        biz_no=tx.biz_no,
        operator=operator.display_name,
        remark=f"冲正 #{tx.id}：{reason}",
        exchange_rate=tx.exchange_rate,
        allow_negative=True,
    )
    return new_tx


# ------------------------------------------------------------------ 导出

EXPORT_COLUMNS = [
    ("occurred_at", "发生时间"),
    ("client_no", "客户编号"),
    ("client_name", "客户"),
    ("biz_type_label", "业务类型"),
    ("direction_label", "方向"),
    ("amount", "金额"),
    ("currency", "币种"),
    ("balance_after", "变动后余额"),
    ("payer", "付款方"),
    ("biz_no", "关联单号"),
    ("operator", "操作人"),
    ("remark", "备注"),
]


def _export_rows(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        row = dict(it)
        row["direction_label"] = "进" if it["direction"] == "IN" else "出"
        out.append(row)
    return out


def export_csv(items: list[dict], summary_rows: list[dict] | None = None) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in EXPORT_COLUMNS])
    for row in _export_rows(items):
        writer.writerow([row.get(key, "") for key, _label in EXPORT_COLUMNS])
    if summary_rows:
        writer.writerow([])
        writer.writerow(["汇总"])
        for s in summary_rows:
            writer.writerow([f"{s['currency']} 期初", s["opening"], "进", s["inflow"], "出", s["outflow"], "期末", s["closing"]])
    return buf.getvalue().encode("utf-8-sig")


def export_xlsx(items: list[dict], summary_rows: list[dict] | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "资金流水"
    ws.append([label for _key, label in EXPORT_COLUMNS])
    for row in _export_rows(items):
        ws.append([row.get(key, "") for key, _label in EXPORT_COLUMNS])

    for col, width in zip("ABCDEFGHIJKL", (20, 16, 30, 12, 8, 14, 8, 16, 20, 20, 12, 24)):
        ws.column_dimensions[col].width = width

    if summary_rows:
        ws2 = wb.create_sheet("汇总")
        ws2.append(["币种", "期初", "本期进账", "本期出账", "期末"])
        for s in summary_rows:
            ws2.append([s["currency"], s["opening"], s["inflow"], s["outflow"], s["closing"]])
        for col, width in zip("ABCDE", (10, 16, 16, 16, 16)):
            ws2.column_dimensions[col].width = width

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ------------------------------------------------------------------ 账户消耗导出

SPEND_EXPORT_COLUMNS = [
    ("date", "日期"),
    ("client_name", "客户"),
    ("account_id", "账户 ID"),
    ("platform", "平台"),
    ("spend", "消耗"),
    ("currency", "币种"),
    ("base_spend", "本位币消耗"),
    ("source", "来源"),
]


def export_spend_csv(items: list[dict], summary: list[dict] | None = None) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _key, label in SPEND_EXPORT_COLUMNS])
    for row in items:
        writer.writerow([row.get(key, "") for key, _label in SPEND_EXPORT_COLUMNS])
    if summary:
        writer.writerow([])
        writer.writerow(["合计"])
        for s in summary:
            writer.writerow([s["currency"], s["spend"], f"{s['days']} 天"])
    return buf.getvalue().encode("utf-8-sig")


def export_spend_xlsx(items: list[dict], summary: list[dict] | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "账户消耗"
    ws.append([label for _key, label in SPEND_EXPORT_COLUMNS])
    for row in items:
        ws.append([row.get(key, "") for key, _label in SPEND_EXPORT_COLUMNS])
    if summary:
        ws.append([])
        ws.append(["合计"])
        for s in summary:
            ws.append([s["currency"], s["spend"], f"{s['days']} 天"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
