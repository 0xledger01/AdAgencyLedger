"""USD→CNY 实时报价：取价、缓存、快照、漂移校验。

业务口径（和门户充值弹层一致）：
- 客户填的是**到账美金**，美元钱包按 1:1 入账；
- 客户实际要转的是**人民币**，按提交时的 USD/CNY 报价折算；
- 报价连同来源写进充值单（exchange_rate / payable_* / rate_source），
  财务确认到账时按这张快照核对，不在确认环节重新算汇率。

安全边界：
- 汇率只影响「应付人民币」，绝不参与美元钱包余额计算；
- 汇率超出合理区间视为脏数据；缓存超过 ``fx_max_stale_hours`` 直接停单；
- 客户提交的 expected_rate 与当前报价漂移超过阈值就抛 ``RateChanged``，
  让客户按新金额重新确认，避免"看到 668 却按 670 扣款"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.base import money, utcnow
from ..models.docs import SystemSetting
from .errors import InvalidAmount, RateChanged, RateUnavailable

CACHE_KEY = "fx:usd_cny:v1"
# 中国无夏令时，固定 UTC+8 即可，避免 Windows 缺 tzdata 时 ZoneInfo 直接炸。
BEIJING = timezone(timedelta(hours=8))

MIN_RATE = Decimal(str(settings.fx_usd_cny_min))
MAX_RATE = Decimal(str(settings.fx_usd_cny_max))
TOLERANCE = Decimal(str(settings.fx_drift_tolerance))
MAX_USD = Decimal(str(settings.fx_topup_max_usd))


@dataclass(frozen=True)
class FxQuote:
    rate: Decimal
    source: str
    quoted_at: datetime          # 供应商报价时间（UTC naive）
    fetched_at: datetime         # 本系统取到该报价的时间（UTC naive）
    stale: bool = False
    warning: str | None = None


def _valid_rate(value) -> Decimal | None:  # noqa: ANN001
    try:
        rate = money(value)
    except Exception:  # noqa: BLE001 - 脏数据一律当没取到
        return None
    if rate <= 0 or rate < MIN_RATE or rate > MAX_RATE:
        return None
    return rate


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read_cache(session: Session) -> FxQuote | None:
    row = session.execute(
        select(SystemSetting).where(SystemSetting.key == CACHE_KEY)
    ).scalar_one_or_none()
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rate = _valid_rate(data.get("rate"))
    fetched_at = _parse_dt(data.get("fetched_at"))
    if rate is None or fetched_at is None:
        return None
    return FxQuote(
        rate=rate,
        source=str(data.get("source") or "cache"),
        quoted_at=_parse_dt(data.get("quoted_at")) or fetched_at,
        fetched_at=fetched_at,
    )


def _write_cache(session: Session, quote: FxQuote) -> None:
    value = json.dumps(
        {
            "rate": str(quote.rate),
            "source": quote.source,
            "quoted_at": quote.quoted_at.isoformat(timespec="seconds"),
            "fetched_at": quote.fetched_at.isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )
    row = session.execute(
        select(SystemSetting).where(SystemSetting.key == CACHE_KEY)
    ).scalar_one_or_none()
    if row is None:
        session.add(SystemSetting(key=CACHE_KEY, value=value))
    else:
        row.value = value
    session.flush()


def _from_open_er_api(data: dict) -> tuple[Decimal, datetime] | None:
    if data.get("result") != "success":
        return None
    rate = _valid_rate((data.get("rates") or {}).get("CNY"))
    if rate is None:
        return None
    ts = data.get("time_last_update_unix")
    quoted_at = (
        datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
        if ts
        else utcnow()
    )
    return rate, quoted_at


def _from_pboc_mid(data: dict) -> tuple[Decimal, datetime] | None:
    """中国外汇交易中心（央行授权）人民币汇率中间价。

    接口一次返回全部币种，只看 USD/CNY；`lastDate` 是北京时间 9:15 的报价，
    落库前统一换成 UTC naive，出参时再由 ``_beijing`` 换回北京时间。
    """
    body = data.get("data") or {}
    # 交易中心把 records 放在顶层，lastDate 在 data 里（接口文档没写清）。
    records = data.get("records") or body.get("records") or []
    for row in records:
        if str(row.get("vrtEName") or "").upper() != "USD/CNY":
            continue
        rate = _valid_rate(row.get("price"))
        if rate is None:
            return None
        quoted_at = utcnow()
        try:
            quoted_at = datetime.strptime(
                str(body.get("lastDate")), "%Y-%m-%d %H:%M"
            ) - timedelta(hours=8)
        except (TypeError, ValueError):
            pass
        return rate, quoted_at
    return None


def _from_frankfurter(data: dict) -> tuple[Decimal, datetime] | None:
    rate = _valid_rate((data.get("rates") or {}).get("CNY"))
    if rate is None:
        return None
    quoted_at = utcnow()
    try:
        quoted_at = datetime.strptime(str(data["date"]), "%Y-%m-%d")
    except (KeyError, TypeError, ValueError):
        pass
    return rate, quoted_at


_PROVIDERS = (
    ("中国外汇交易中心（央行中间价）", "fx_pboc_url", _from_pboc_mid),
    ("open.er-api.com", "fx_provider_url", _from_open_er_api),
    ("api.frankfurter.dev", "fx_backup_provider_url", _from_frankfurter),
)


def fetch_live() -> FxQuote | None:
    """依次问两个供应商；全失败返回 None，不编汇率。"""
    for source, url_attr, parser in _PROVIDERS:
        url = str(getattr(settings, url_attr) or "").strip()
        if not url:
            continue
        try:
            with httpx.Client(timeout=6.0, follow_redirects=True) as client:
                resp = client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; ColdCoffeeFX/1.0)",
                        "Referer": "https://www.chinamoney.com.cn/",
                    },
                )
            resp.raise_for_status()
            parsed = parser(resp.json())
        except Exception:  # noqa: BLE001 - 单个供应商失败继续问下一个
            continue
        if parsed is None:
            continue
        rate, quoted_at = parsed
        return FxQuote(
            rate=rate,
            source=source,
            quoted_at=quoted_at,
            fetched_at=utcnow(),
        )
    return None


def _pboc_publish_cutoff(now_utc: datetime) -> datetime:
    """最近一次央行中间价应已发布的时间点（北京时间 09:15，UTC naive）。"""
    bj = now_utc.replace(tzinfo=timezone.utc).astimezone(BEIJING)
    cutoff = bj.replace(hour=9, minute=15, second=0, microsecond=0)
    if bj < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff.astimezone(timezone.utc).replace(tzinfo=None)


def get_quote(session: Session, *, force: bool = False) -> FxQuote:
    """返回当前可用报价；缓存新鲜直接复用，过期先刷新，刷新失败才降级。"""
    cached = _read_cache(session)
    now = utcnow()
    ttl = timedelta(seconds=max(int(settings.fx_cache_ttl_seconds), 60))
    # 央行每天 09:15 发新中间价：过了这个点，哪怕缓存未满 1 小时也重新取一次，
    # 否则客户上午会一直看到昨天的价。节假日供应商仍返回上一交易日，
    # 加 30 分钟重试间隔，避免一整天每次报价都打接口。
    outdated_publish = (
        cached is not None
        and cached.source.startswith("中国外汇交易中心")
        and cached.quoted_at < _pboc_publish_cutoff(now)
        and now - cached.fetched_at > timedelta(minutes=30)
    )
    if cached is not None and not force and not outdated_publish and now - cached.fetched_at <= ttl:
        return cached

    live = fetch_live()
    if live is not None:
        _write_cache(session, live)
        return live

    if cached is not None:
        max_age = timedelta(hours=max(int(settings.fx_max_stale_hours), 1))
        if now - cached.fetched_at <= max_age:
            minutes = max(int((now - cached.fetched_at).total_seconds() // 60), 1)
            return replace(
                cached,
                stale=True,
                warning=f"实时汇率暂不可用，沿用 {minutes} 分钟前的报价，请以财务确认的汇率为准",
            )
        raise RateUnavailable(
            "实时汇率超过 24 小时未更新，暂时无法生成人民币应付金额，请稍后再试或联系财务"
        )

    raise RateUnavailable("实时汇率暂不可用，暂时无法生成人民币应付金额，请稍后再试或联系财务")


def payable_cny(amount_usd, rate) -> Decimal:  # noqa: ANN001
    """人民币应付向上取整到分：宁可多转一分，也不让到账美金不足。"""
    usd = money(amount_usd)
    r = money(rate)
    return money((usd * r).quantize(Decimal("0.01"), rounding=ROUND_UP))


def validate_amount_usd(amount) -> Decimal:  # noqa: ANN001
    usd = money(amount)
    if usd <= 0:
        raise InvalidAmount("充值金额必须大于 0")
    if usd != usd.quantize(Decimal("0.01")):
        raise InvalidAmount("充值金额最多支持 2 位小数")
    if usd > MAX_USD:
        raise InvalidAmount(f"单笔充值不能超过 {money(MAX_USD)} USD")
    return usd


def resolve_order_quote(session: Session, amount_usd, expected_rate=None) -> FxQuote:  # noqa: ANN001
    """下单取价 + 漂移校验；expected_rate 为空时只取当前报价。"""
    quote = get_quote(session)
    if expected_rate is not None:
        try:
            expected = money(expected_rate)
        except Exception as exc:  # noqa: BLE001
            raise InvalidAmount("汇率取值非法") from exc
        if expected <= 0:
            raise InvalidAmount("汇率取值非法")
        drift = abs(quote.rate - expected) / expected
        if drift > TOLERANCE:
            raise RateChanged(
                f"汇率已更新：当前 1 USD = {quote.rate} CNY，您看到的是 {expected} CNY。"
                "应付人民币金额已变化，请确认新金额后再提交"
            )
    return quote


def source_label(quote: FxQuote) -> str:
    """写进充值单的汇率来源，长度受 rate_source(160) 限制。"""
    text = (
        f"USD/CNY {quote.rate} · {quote.source} · "
        f"{quote.quoted_at:%Y-%m-%d %H:%M} UTC"
        + (" · 缓存报价" if quote.stale else "")
    )
    return text[:160]


def _beijing(value: datetime) -> str:
    aware = value.replace(tzinfo=timezone.utc)
    return aware.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")


def quote_payload(quote: FxQuote, amount_usd) -> dict:  # noqa: ANN001
    usd = validate_amount_usd(amount_usd)
    cny = payable_cny(usd, quote.rate)
    return {
        "amount_usd": str(usd),
        "rate": str(quote.rate),
        "payable_amount": str(cny),
        "payable_currency": "CNY",
        "rate_source": quote.source,
        "rate_source_label": source_label(quote),
        "quoted_at": _beijing(quote.quoted_at),
        "fetched_at": _beijing(quote.fetched_at),
        "stale": quote.stale,
        "warning": quote.warning,
        "tolerance": str(TOLERANCE),
    }
