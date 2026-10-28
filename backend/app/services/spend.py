"""D6 消耗：批量预校验 / 落库 / 异常检测 / 多维度报表。

铁律：**消耗只扣广告账户（媒体侧参照），不动客户钱包**（DESIGN 5 双算修正）。
账户余额允许扣成负数，不拦截 —— 那是媒体侧真实发生的花费。

报表口径（业务方按 CRM 原型拍定）：
- 总消耗 = GMV Max 总消耗 + BC 总消耗（`spend` 字段保留为这个总数）；
- 净消耗 = GMV Max 净消耗 + BC 净消耗；
- 曝光 / 点击 / 转化只挂在 BC 侧，千展 / 点击率 / 点击费用由 BC 消耗推算；
- **禁止跨币种求和**：报表一次只算一个币种，`currencies` 返回可选清单。
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models.base import money
from ..models.client import AdAccount, Client, SpendDaily
from ..models.enums import PLATFORM_LABEL, AdAccountStatus, Platform
from .errors import StateError

HISTORY_DAYS = 7
SPIKE_FACTOR = 3

GROUP_BYS = ("client", "account", "sales", "agent")
MODES = ("summary", "daily")
TOP_METRICS = ("total", "gmv", "bc")
MAX_REPORT_DAYS = 366

_PLATFORM_ORDER = {Platform.TIKTOK: 0, Platform.FB: 1, Platform.GOOGLE: 2, Platform.OTHER: 3}
_TWO_DP = Decimal("0.01")
_FOUR_DP = Decimal("0.0001")


def _parse_date(value) -> date | None:  # noqa: ANN001
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    # 兼容媒体后台常见的 2026/10/20 与 2026.10.20
    text = text.replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_amount(value) -> Decimal | None:  # noqa: ANN001
    if value is None or str(value).strip() == "":
        return None
    try:
        return money(value)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_count(value) -> int:
    """计数列：允许千分位逗号 / 前后空格；负数或小数直接判非法。"""
    if value is None or str(value).strip() == "":
        return 0
    text = str(value).strip().replace(",", "").replace(" ", "")
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("指标非法") from None
    if d != d.to_integral_value():
        raise ValueError("指标非法")
    if d < 0:
        raise ValueError("指标为负")
    return int(d)


def _history_avg_map(
    session: Session, pairs: list[tuple[int, date]]
) -> dict[tuple[int, date], Decimal | None]:
    """一次查出所有 (账户, 日期) 过去 7 天的日均消耗，避免逐行查库。

    返回 {(ad_account_id, day): 均值 or None}；None 表示没有历史，不算异常。
    """
    if not pairs:
        return {}
    acc_ids = {acc for acc, _day in pairs}
    lo = min(day for _acc, day in pairs) - timedelta(days=HISTORY_DAYS)
    hi = max(day for _acc, day in pairs)

    buckets: dict[int, dict[date, Decimal]] = {}
    for acc_id, day, spend in session.execute(
        select(SpendDaily.ad_account_id, SpendDaily.date, SpendDaily.spend).where(
            SpendDaily.ad_account_id.in_(acc_ids),
            SpendDaily.date >= lo,
            SpendDaily.date < hi,
        )
    ).all():
        buckets.setdefault(int(acc_id), {})[day] = money(spend)

    out: dict[tuple[int, date], Decimal | None] = {}
    for acc_id, day in pairs:
        start = day - timedelta(days=HISTORY_DAYS)
        values = [
            amt for d, amt in buckets.get(acc_id, {}).items() if start <= d < day
        ]
        out[(acc_id, day)] = (
            money(sum(values, Decimal("0")) / len(values)) if values else None
        )
    return out


def _detect_anomaly(
    account: AdAccount, spend: Decimal, avg: Decimal | None
) -> str | None:
    """异常判定。avg 由 _history_avg_map 预算好，本函数不查库。"""
    if avg is not None and spend > avg * SPIKE_FACTOR:
        return "SPIKE"
    if (
        spend == 0
        and money(account.balance) > 0
        and account.status is AdAccountStatus.NORMAL
    ):
        return "ZERO_SPEND"
    return None


def _split_breakdown(
    raw: dict, spend: Decimal
) -> tuple[Decimal, Decimal, str | None]:
    """把一行拆成 (GMV Max, BC)。

    老格式只有 `spend`：整体按 BC 口径落库，保证「GMV Max + BC = 总消耗」恒等。
    新格式允许只给总消耗 + 一侧明细，缺的一侧用总消耗补齐；两侧都给时
    允许 0.01 的四舍五入误差，超出即判「金额不一致」。
    """
    gmv = _parse_amount(raw.get("gmv_max_spend"))
    bc = _parse_amount(raw.get("bc_spend"))
    if gmv is not None and gmv < 0:
        return Decimal("0"), Decimal("0"), "金额为负"
    if bc is not None and bc < 0:
        return Decimal("0"), Decimal("0"), "金额为负"

    if gmv is None and bc is None:
        return Decimal("0"), spend, None
    if gmv is None:
        gmv = spend - bc  # type: ignore[operator]
    elif bc is None:
        bc = spend - gmv
    else:
        if abs((gmv + bc) - spend) > _TWO_DP:
            return Decimal("0"), Decimal("0"), "金额不一致"
        # 两侧明细都有时以明细为准（总消耗跟着走）
        spend = gmv + bc
    if gmv < 0 or bc < 0:
        return Decimal("0"), Decimal("0"), "金额不一致"
    return gmv, bc, None


def import_spend(
    session: Session, rows: list[dict], *, dry_run: bool = False
) -> dict:
    """按契约 6.1 的优先级逐行校验。dry_run 只校验不落库。"""
    today = date.today()
    report: dict = {
        "dry_run": bool(dry_run),
        "total": len(rows),
        "success": 0,
        "skipped": [],
        "failed": [],
        "anomalies": [],
    }

    # 第一遍：日期 / 账户 / 金额（优先级 1-3）
    pending: list[dict] = []
    for idx, raw in enumerate(rows, start=1):
        raw = raw or {}
        acc_text = str(raw.get("account_id") or "").strip()
        raw_date = raw.get("date")

        day = _parse_date(raw_date)
        if day is None or day > today:
            report["failed"].append(
                {"row": idx, "account_id": acc_text or None,
                 "date": str(raw_date) if raw_date is not None else None,
                 "reason": "日期非法"}
            )
            continue

        account = None
        if acc_text:
            account = session.execute(
                select(AdAccount).where(AdAccount.account_id == acc_text)
            ).scalar_one_or_none()
        if account is None:
            report["failed"].append(
                {"row": idx, "account_id": acc_text or None, "date": day.isoformat(),
                 "reason": "账户不存在"}
            )
            continue

        spend = _parse_amount(raw.get("spend"))
        gmv_hint = _parse_amount(raw.get("gmv_max_spend"))
        bc_hint = _parse_amount(raw.get("bc_spend"))
        if spend is None and (gmv_hint is not None or bc_hint is not None):
            spend = (gmv_hint or Decimal("0")) + (bc_hint or Decimal("0"))
        if spend is None:
            report["failed"].append(
                {"row": idx, "account_id": acc_text, "date": day.isoformat(),
                 "reason": "金额非法"}
            )
            continue
        if spend < 0:
            report["failed"].append(
                {"row": idx, "account_id": acc_text, "date": day.isoformat(),
                 "reason": "金额为负"}
            )
            continue

        gmv, bc, split_err = _split_breakdown(raw, spend)
        if split_err:
            report["failed"].append(
                {"row": idx, "account_id": acc_text, "date": day.isoformat(),
                 "reason": split_err}
            )
            continue
        spend = gmv + bc

        gmv_net = _parse_amount(raw.get("gmv_max_net_spend"))
        bc_net = _parse_amount(raw.get("bc_net_spend"))
        gmv_net = gmv if gmv_net is None else gmv_net
        bc_net = bc if bc_net is None else bc_net
        cash = _parse_amount(raw.get("bc_cash_spend"))
        grant = _parse_amount(raw.get("bc_grant_spend"))
        cash = bc if cash is None else cash
        grant = max(Decimal("0"), bc - cash) if grant is None else grant
        if gmv_net < 0 or bc_net < 0 or cash < 0 or grant < 0:
            report["failed"].append(
                {"row": idx, "account_id": acc_text, "date": day.isoformat(),
                 "reason": "金额为负"}
            )
            continue

        try:
            impressions = _parse_count(raw.get("impressions"))
            clicks = _parse_count(raw.get("clicks"))
            conversions = _parse_count(raw.get("conversions"))
        except ValueError as exc:
            report["failed"].append(
                {"row": idx, "account_id": acc_text, "date": day.isoformat(),
                 "reason": str(exc)}
            )
            continue

        rate = _parse_amount(raw.get("exchange_rate")) or Decimal("1.0000")
        pending.append(
            {
                "row": idx,
                "day": day,
                "account": account,
                "account_id": acc_text,
                "spend": spend,
                "gmv": gmv,
                "gmv_net": gmv_net,
                "bc": bc,
                "bc_net": bc_net,
                "cash": cash,
                "grant": grant,
                "impressions": impressions,
                "clicks": clicks,
                "conversions": conversions,
                "rate": rate,
                "currency": str(raw.get("currency") or account.currency),
            }
        )

    # 查重快照（必须在写入之前取，保证 4 优先于 5）
    existing: set[tuple[int, date]] = set()
    if pending:
        acc_ids = {p["account"].id for p in pending}
        days = {p["day"] for p in pending}
        for acc_id, d in session.execute(
            select(SpendDaily.ad_account_id, SpendDaily.date).where(
                SpendDaily.ad_account_id.in_(acc_ids), SpendDaily.date.in_(days)
            )
        ).all():
            existing.add((int(acc_id), d))

    # 历史均值一次算完（写库前算，批次内后行不受前行影响）
    avg_map = _history_avg_map(
        session, [(p["account"].id, p["day"]) for p in pending]
    )

    # 第二遍：查重（4 已存在 → 5 批次内重复）→ 写库
    seen: set[tuple[int, date]] = set()
    for p in pending:
        key = (p["account"].id, p["day"])
        if key in existing:
            report["skipped"].append(
                {"row": p["row"], "account_id": p["account_id"],
                 "date": p["day"].isoformat(), "reason": "已存在"}
            )
            continue
        if key in seen:
            report["skipped"].append(
                {"row": p["row"], "account_id": p["account_id"],
                 "date": p["day"].isoformat(), "reason": "批次内重复"}
            )
            continue
        seen.add(key)

        anomaly = _detect_anomaly(p["account"], p["spend"], avg_map.get(key))
        if anomaly:
            report["anomalies"].append(
                {"row": p["row"], "account_id": p["account_id"],
                 "date": p["day"].isoformat(), "spend": str(money(p["spend"])),
                 "reason": anomaly}
            )

        report["success"] += 1
        if dry_run:
            continue

        session.add(
            SpendDaily(
                date=p["day"],
                ad_account_id=p["account"].id,
                spend=p["spend"],
                currency=p["currency"],
                exchange_rate=p["rate"],
                base_spend=money(p["spend"] * p["rate"]),
                source="IMPORT",
                gmv_max_spend=p["gmv"],
                gmv_max_net_spend=p["gmv_net"],
                bc_spend=p["bc"],
                bc_net_spend=p["bc_net"],
                bc_cash_spend=p["cash"],
                bc_grant_spend=p["grant"],
                impressions=p["impressions"],
                clicks=p["clicks"],
                conversions=p["conversions"],
            )
        )
        # 消耗只扣广告账户；可为负，不拦截
        p["account"].balance = money(p["account"].balance) - p["spend"]

    if not dry_run:
        session.flush()
    return report


def list_spend(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    client_id: int | None = None,
    account_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    conds = []
    if date_from:
        conds.append(SpendDaily.date >= date_from)
    if date_to:
        conds.append(SpendDaily.date <= date_to)
    if client_id:
        conds.append(AdAccount.client_id == client_id)
    if account_id:
        conds.append(AdAccount.account_id == str(account_id).strip())

    stmt = (
        select(SpendDaily, AdAccount, Client)
        .join(AdAccount, AdAccount.id == SpendDaily.ad_account_id)
        .join(Client, Client.id == AdAccount.client_id)
        .where(*conds)
    )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(SpendDaily.date.desc(), SpendDaily.id.desc()).limit(limit).offset(offset)
    ).all()

    # 列表每行也跑一次异常判定（复用 import 用的同一套规则，均值一次查完）
    avg_map = _history_avg_map(session, [(a.id, s.date) for s, a, _c in rows])

    items = [
        {
            "id": s.id,
            "date": s.date.isoformat(),
            "account_id": a.account_id,
            "ad_account_id": a.id,
            "client_name": c.company_name,
            "platform": str(a.platform),
            "spend": str(money(s.spend)),
            "currency": s.currency,
            "base_spend": str(money(s.base_spend)),
            "source": s.source,
            "anomaly": _detect_anomaly(a, money(s.spend), avg_map.get((a.id, s.date))),
            # 报表字段：明细页核对 GMV Max / BC 拆分与投放指标
            "gmv_max_spend": str(money(s.gmv_max_spend)),
            "bc_spend": str(money(s.bc_spend)),
            "bc_cash_spend": str(money(s.bc_cash_spend)),
            "bc_grant_spend": str(money(s.bc_grant_spend)),
            "impressions": int(s.impressions or 0),
            "clicks": int(s.clicks or 0),
            "conversions": int(s.conversions or 0),
        }
        for s, a, c in rows
    ]

    totals_stmt = (
        select(SpendDaily.currency, func.coalesce(func.sum(SpendDaily.spend), 0))
        .select_from(SpendDaily)
        .join(AdAccount, AdAccount.id == SpendDaily.ad_account_id)
        .where(*conds)
        .group_by(SpendDaily.currency)
    )
    totals = [
        {"currency": ccy, "amount": str(money(total))}
        for ccy, total in session.execute(totals_stmt).all()
    ]

    return {"items": items, "totals": {"spend": totals}, "total": int(total)}


# ------------------------------------------------------------------ 报表


def _q2(value: Decimal) -> str:
    return str(money(value).quantize(_TWO_DP))


def _ratio_pct(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == 0:
        return None
    return str((Decimal(numerator) / Decimal(denominator) * 100).quantize(_TWO_DP))


def _per_unit(numerator: Decimal, denominator: Decimal, scale: Decimal = Decimal("1")) -> str | None:
    if denominator == 0:
        return None
    return str((Decimal(numerator) * scale / Decimal(denominator)).quantize(_FOUR_DP))


def _mom(cur: Decimal, prev: Decimal) -> float | None:
    if prev == 0:
        return 0.0 if cur == 0 else None
    return float(((cur - prev) / prev * 100).quantize(Decimal("0.1")))


def _blank_bucket() -> dict:
    return {
        "spend": Decimal("0"),
        "gmv": Decimal("0"),
        "gmv_net": Decimal("0"),
        "bc": Decimal("0"),
        "bc_net": Decimal("0"),
        "cash": Decimal("0"),
        "grant": Decimal("0"),
        "impressions": 0,
        "clicks": 0,
        "conversions": 0,
    }


def _add_bucket(bucket: dict, row: SpendDaily) -> None:
    bucket["spend"] += money(row.spend)
    bucket["gmv"] += money(row.gmv_max_spend)
    bucket["gmv_net"] += money(row.gmv_max_net_spend)
    bucket["bc"] += money(row.bc_spend)
    bucket["bc_net"] += money(row.bc_net_spend)
    bucket["cash"] += money(row.bc_cash_spend)
    bucket["grant"] += money(row.bc_grant_spend)
    bucket["impressions"] += int(row.impressions or 0)
    bucket["clicks"] += int(row.clicks or 0)
    bucket["conversions"] += int(row.conversions or 0)


def _metric_amount(bucket: dict, metric: str) -> Decimal:
    if metric == "gmv":
        return bucket["gmv"]
    if metric == "bc":
        return bucket["bc"]
    return bucket["spend"]


def _serialize_bucket(bucket: dict, *, currency: str) -> dict:
    return {
        "gmv_bc": {"total": _q2(bucket["spend"]), "net": _q2(bucket["gmv_net"] + bucket["bc_net"])},
        "gmv_max": {"total": _q2(bucket["gmv"]), "net": _q2(bucket["gmv_net"])},
        "bc": {
            "total": _q2(bucket["bc"]),
            "net": _q2(bucket["bc_net"]),
            "cash": _q2(bucket["cash"]),
            "grant": _q2(bucket["grant"]),
            "impressions": bucket["impressions"],
            "cpm": _per_unit(bucket["bc"], Decimal(bucket["impressions"]), Decimal("1000")),
            "clicks": bucket["clicks"],
            "ctr": _ratio_pct(Decimal(bucket["clicks"]), Decimal(bucket["impressions"])),
            "cpc": _per_unit(bucket["bc"], Decimal(bucket["clicks"])),
            "conversions": bucket["conversions"],
            "cvr": _ratio_pct(Decimal(bucket["conversions"]), Decimal(bucket["clicks"])),
        },
        "currency": currency,
    }


def _row_meta(group_by: str, account: AdAccount, client: Client) -> dict:
    """按分组维度给出本行的 label / 副标题 / 关联客户信息。"""
    platform = str(account.platform)
    base = {
        "platform": platform,
        "platform_label": PLATFORM_LABEL.get(account.platform, platform),
        "client_id": client.id,
        "client_name": client.company_name,
        "client_code": client.crm_code or client.biz_no,
        "sales_owner": client.sales_owner or "未分配",
        "agent": account.channel or "未归属渠道",
        "account_id": account.account_id,
        "ad_account_id": account.id,
    }
    if group_by == "account":
        base.update({"label": account.account_id, "sub_label": client.company_name})
    elif group_by == "sales":
        base.update({"label": client.sales_owner or "未分配", "sub_label": ""})
    elif group_by == "agent":
        base.update({"label": account.channel or "未归属渠道", "sub_label": ""})
    else:
        base.update({"label": client.company_name, "sub_label": client.crm_code or client.biz_no or ""})
    return base


def _group_key(group_by: str, account: AdAccount, client: Client, platform: str, currency: str) -> str:
    """分组键。

    客户 / 销售 / 代理商报表**按对象聚合**（一个客户一行，多平台显示「多平台」），
    账户报表天然按账户聚合。媒体筛选仍然生效，只是不再把同一客户拆成多行。
    """
    if group_by == "account":
        ident = f"account:{account.id}"
    elif group_by == "sales":
        ident = f"sales:{client.sales_owner or '未分配'}"
    elif group_by == "agent":
        ident = f"agent:{account.channel or '未归属渠道'}"
    else:
        ident = f"client:{client.id}"
    if group_by == "account":
        return f"{ident}|{platform}|{currency}"
    return f"{ident}|{currency}"


def _platform_fields(plats: set[str]) -> tuple[str, str]:
    """单个媒体直接显示；多平台聚合显示「多平台」，避免一行里只写第一个媒体。"""
    if len(plats) == 1:
        p = next(iter(plats))
        return p, _platform_label(p)
    return "", "多平台"


def _platform_label(code: str) -> str:
    try:
        return PLATFORM_LABEL.get(Platform(code), code)
    except ValueError:
        return code


def _platform_order(code: str) -> int:
    try:
        return _PLATFORM_ORDER.get(Platform(code), 9)
    except ValueError:
        return 9


def spend_report(
    session: Session,
    *,
    date_from: date,
    date_to: date,
    group_by: str = "client",
    mode: str = "summary",
    top_metric: str = "total",
    client_keyword: str | None = None,
    sales_owner: str | None = None,
    platform: str | None = None,
    currency: str | None = None,
    sales_scope: str | None = None,
    top_limit: int = 10,
) -> dict:
    if group_by not in GROUP_BYS:
        raise StateError(f"报表类型非法：{group_by}")
    if mode not in MODES:
        raise StateError(f"分组类型非法：{mode}")
    if top_metric not in TOP_METRICS:
        raise StateError(f"排行口径非法：{top_metric}")
    if date_to < date_from:
        raise StateError("结束日期早于开始日期")
    days = (date_to - date_from).days + 1
    if days > MAX_REPORT_DAYS:
        raise StateError(f"日期跨度不能超过 {MAX_REPORT_DAYS} 天")

    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)

    # 销售角色只能看自己名下（由 API 传 sales_scope，服务端兜底）
    if sales_scope is not None:
        sales_owner = sales_scope

    def base_conds() -> list:
        conds: list = []
        if client_keyword:
            kw = client_keyword.strip().lower()
            like = f"%{kw}%"
            conds.append(
                or_(
                    func.lower(Client.company_name).like(like),
                    func.lower(func.coalesce(Client.crm_code, "")).like(like),
                    func.lower(func.coalesce(Client.biz_no, "")).like(like),
                )
            )
        if sales_owner:
            conds.append(Client.sales_owner == sales_owner)
        if platform:
            conds.append(AdAccount.platform == platform)
        return conds

    conds = base_conds()

    # 可选币种：受其它筛选影响，但不受 currency 本身影响
    currency_rows = session.execute(
        select(SpendDaily.currency, func.coalesce(func.sum(SpendDaily.spend), 0))
        .select_from(SpendDaily)
        .join(AdAccount, AdAccount.id == SpendDaily.ad_account_id)
        .join(Client, Client.id == AdAccount.client_id)
        .where(SpendDaily.date >= date_from, SpendDaily.date <= date_to, *conds)
        .group_by(SpendDaily.currency)
        .order_by(func.coalesce(func.sum(SpendDaily.spend), 0).desc())
    ).all()
    currencies = [str(ccy) for ccy, _amt in currency_rows]
    if currency:
        currency = str(currency).upper()
    elif currencies:
        currency = currencies[0]
    else:
        currency = "USD"

    row_conds = [SpendDaily.date >= date_from, SpendDaily.date <= date_to,
                 SpendDaily.currency == currency, *conds]
    prev_conds = [SpendDaily.date >= prev_from, SpendDaily.date <= prev_to,
                  SpendDaily.currency == currency, *conds]

    stmt = (
        select(SpendDaily, AdAccount, Client)
        .join(AdAccount, AdAccount.id == SpendDaily.ad_account_id)
        .join(Client, Client.id == AdAccount.client_id)
    )
    current_rows = session.execute(
        stmt.where(*row_conds).order_by(SpendDaily.date.asc(), SpendDaily.id.asc())
    ).all()
    prev_rows = session.execute(stmt.where(*prev_conds)).all()

    total_bucket = _blank_bucket()
    buckets: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    platforms: dict[str, set[str]] = {}
    daily: dict[str, list[Decimal]] = {}
    trend: dict[str, list[Decimal]] = {}
    top: dict[int, dict] = {}

    for s, a, c in current_rows:
        _add_bucket(total_bucket, s)
        p = str(a.platform)
        key = _group_key(group_by, a, c, p, currency)
        bucket = buckets.setdefault(key, _blank_bucket())
        _add_bucket(bucket, s)
        if key not in meta:
            meta[key] = {**_row_meta(group_by, a, c), "date": s.date.isoformat(), "key": key}
        platforms.setdefault(key, set()).add(p)
        daily.setdefault(key, [Decimal("0")] * days)[(s.date - date_from).days] += money(s.spend)
        trend.setdefault(p, [Decimal("0")] * days)[(s.date - date_from).days] += money(s.spend)
        # TOP 客户始终按客户聚合：它不随报表 Tab 变化
        item = top.setdefault(
            c.id,
            {"client_id": c.id, "client_name": c.company_name,
             "client_code": c.crm_code or c.biz_no, "sales_owner": c.sales_owner or "未分配",
             "bucket": _blank_bucket()},
        )
        _add_bucket(item["bucket"], s)

    prev_buckets: dict[str, dict] = {}
    prev_total = _blank_bucket()
    prev_top: dict[int, dict] = {}
    for s, a, c in prev_rows:
        _add_bucket(prev_total, s)
        p = str(a.platform)
        key = _group_key(group_by, a, c, p, currency)
        _add_bucket(prev_buckets.setdefault(key, _blank_bucket()), s)
        item = prev_top.setdefault(c.id, {"bucket": _blank_bucket()})
        _add_bucket(item["bucket"], s)

    if mode == "daily":
        # 按日：同一对象同一天只出一行（多账户合并），日期列显示真实日期，环比不适用
        day_buckets: dict[tuple[str, str], dict] = {}
        day_meta: dict[tuple[str, str], dict] = {}
        day_platforms: dict[tuple[str, str], set[str]] = {}
        for s, a, c in current_rows:
            p = str(a.platform)
            key = _group_key(group_by, a, c, p, currency)
            dkey = (key, s.date.isoformat())
            _add_bucket(day_buckets.setdefault(dkey, _blank_bucket()), s)
            day_meta.setdefault(dkey, {**_row_meta(group_by, a, c), "date": s.date.isoformat()})
            day_platforms.setdefault(dkey, set()).add(p)
        rows: list[dict] = []
        for dkey, bucket in day_buckets.items():
            key, day_text = dkey
            plat, plat_label = _platform_fields(day_platforms.get(dkey, set()))
            rows.append(
                {
                    **day_meta[dkey],
                    "platform": plat,
                    "platform_label": plat_label,
                    "key": f"{key}|{day_text}",
                    **_serialize_bucket(bucket, currency=currency),
                    "spark": [],
                    "mom_pct": None,
                }
            )
        rows.sort(key=lambda r: (r["date"], Decimal(r["gmv_bc"]["total"])), reverse=True)
    else:
        rows = []
        for key, bucket in buckets.items():
            m = meta[key]
            prev = prev_buckets.get(key)
            plat, plat_label = _platform_fields(platforms.get(key, set()))
            rows.append(
                {
                    **m,
                    "platform": plat,
                    "platform_label": plat_label,
                    **_serialize_bucket(bucket, currency=currency),
                    "spark": [str(money(v)) for v in daily.get(key, [])],
                    "mom_pct": _mom(bucket["spend"], prev["spend"] if prev else Decimal("0")),
                }
            )
        rows.sort(key=lambda r: (Decimal(r["gmv_bc"]["total"]), r["label"]), reverse=True)

    top_clients = []
    rank_total = sum((_metric_amount(v["bucket"], top_metric) for v in top.values()), Decimal("0"))
    ranked = sorted(top.values(), key=lambda x: _metric_amount(x["bucket"], top_metric), reverse=True)
    for v in ranked[:top_limit]:
        amount = _metric_amount(v["bucket"], top_metric)
        prev = prev_top.get(v["client_id"])
        prev_amount = _metric_amount(prev["bucket"], top_metric) if prev else Decimal("0")
        top_clients.append(
            {
                "client_id": v["client_id"],
                "client_name": v["client_name"],
                "client_code": v["client_code"],
                "sales_owner": v["sales_owner"],
                "amount": _q2(amount),
                "share_pct": float((amount / rank_total * 100).quantize(Decimal("0.1"))) if rank_total else 0.0,
                "mom_pct": _mom(amount, prev_amount),
            }
        )

    series = []
    for p in sorted(trend.keys(), key=_platform_order):
        series.append(
            {
                "platform": p,
                "label": _platform_label(p),
                "points": [str(money(v)) for v in trend[p]],
            }
        )

    overview = _serialize_bucket(total_bucket, currency=currency)
    overview["mom_pct"] = _mom(total_bucket["spend"], prev_total["spend"])
    overview["prev_total"] = _q2(prev_total["spend"])
    # 扁平字段，前端 KPI 直接取
    overview.update(
        {
            "gmv_bc_total": overview["gmv_bc"]["total"],
            "gmv_bc_net": overview["gmv_bc"]["net"],
            "gmv_total": overview["gmv_max"]["total"],
            "gmv_net": overview["gmv_max"]["net"],
            "bc_total": overview["bc"]["total"],
            "bc_net": overview["bc"]["net"],
            "bc_cash": overview["bc"]["cash"],
            "bc_grant": overview["bc"]["grant"],
            "impressions": overview["bc"]["impressions"],
            "clicks": overview["bc"]["clicks"],
            "conversions": overview["bc"]["conversions"],
            "ctr": overview["bc"]["ctr"],
            "cpc": overview["bc"]["cpc"],
            "cpm": overview["bc"]["cpm"],
        }
    )

    totals = {
        "label": "合计",
        "sub_label": "",
        "platform": "",
        "platform_label": "",
        **_serialize_bucket(total_bucket, currency=currency),
        "mom_pct": overview["mom_pct"],
        "spark": [],
    }

    return {
        "range": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "days": days,
            "prev_from": prev_from.isoformat(),
            "prev_to": prev_to.isoformat(),
            "currency": currency,
            "currencies": currencies,
        },
        "group_by": group_by,
        "mode": mode,
        "top_metric": top_metric,
        "overview": overview,
        "top_clients": top_clients,
        "trend": {
            "days": [(date_from + timedelta(days=i)).isoformat() for i in range(days)],
            "series": series,
        },
        "rows": rows,
        "totals": totals,
    }


REPORT_HEADERS = [
    "媒体", "日期", "客户", "客户编号", "所属销售", "代理商",
    "GMV Max+BC总消耗", "GMV Max+BC净消耗",
    "GMV Max总消耗", "GMV Max净消耗",
    "BC总消耗", "BC净消耗", "BC现金消耗", "BC赠款消耗",
    "曝光数", "平均千展费用", "点击数", "点击率(%)", "平均点击费用", "转化数",
    "币种", "环比(%)",
]


def report_csv(report: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(REPORT_HEADERS)
    for r in report["rows"]:
        writer.writerow(
            [
                r["platform_label"] or "全部",
                "汇总" if report["mode"] == "summary" else r["date"],
                r["label"],
                r.get("client_code") or "",
                r.get("sales_owner") or "",
                r.get("agent") or "",
                r["gmv_bc"]["total"], r["gmv_bc"]["net"],
                r["gmv_max"]["total"], r["gmv_max"]["net"],
                r["bc"]["total"], r["bc"]["net"], r["bc"]["cash"], r["bc"]["grant"],
                r["bc"]["impressions"], r["bc"]["cpm"] or "",
                r["bc"]["clicks"], r["bc"]["ctr"] or "", r["bc"]["cpc"] or "",
                r["bc"]["conversions"], r["currency"],
                "" if r["mom_pct"] is None else f"{r['mom_pct']:.1f}",
            ]
        )
    t = report["totals"]
    writer.writerow(
        [
            "", "合计", "合计", "", "", "",
            t["gmv_bc"]["total"], t["gmv_bc"]["net"],
            t["gmv_max"]["total"], t["gmv_max"]["net"],
            t["bc"]["total"], t["bc"]["net"], t["bc"]["cash"], t["bc"]["grant"],
            t["bc"]["impressions"], t["bc"]["cpm"] or "",
            t["bc"]["clicks"], t["bc"]["ctr"] or "", t["bc"]["cpc"] or "",
            t["bc"]["conversions"], t["currency"], "",
        ]
    )
    return buf.getvalue()
