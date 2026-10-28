"""P3.5：文件上传安全 + 凭证闸口（契约 docs/CONTRACT-P3.5.md）。

这些用例守的是「钱能不能在无证据的情况下记进账本」，比页面好不好看重要得多。
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy.orm import Session

from backend.app.models.base import money
from backend.app.models.client import ClientTopUp
from backend.app.models.docs import AuditLog
from backend.app.models.enums import TopUpStatus
from backend.app.models.wallet import WalletTransaction
from backend.app.services import storage
from backend.app.services.errors import InvalidFile, StateError
from backend.app.services.topup import confirm_topup, create_topup

# 1x1 透明 PNG，合法文件头 + 最小数据
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB"
    "AAH8i7wAAAAASUVORK5CYII="
)


def _upload(db: Session, data: bytes = PNG_1PX, name: str = "回单.png", biz: str = "TOPUP_VOUCHER"):
    return storage.save(
        db, data=data, original_name=name, declared_type="image/png",
        biz_type=biz, uploader_id=None, client_id=None,
    )


# ---------------------------------------------------------------- 魔数嗅探

def test_accepts_real_png_and_reports_canonical_type(db: Session) -> None:  # noqa: ANN001
    obj, deduped = _upload(db)
    assert obj.content_type == "image/png"
    assert obj.size == len(PNG_1PX)
    assert deduped is False
    assert storage.safe_resolve(obj.key).is_file()


def test_rejects_exe_disguised_as_png(db: Session) -> None:  # noqa: ANN001
    with pytest.raises(InvalidFile) as exc:
        _upload(db, data=b"MZ\x90\x00" + b"\x00" * 200, name="evil.png")
    assert "文件内容" in str(exc.value)


def test_rejects_text_declared_as_png(db: Session) -> None:  # noqa: ANN001
    """不信 Content-Type：声明 image/png 但内容是纯文本，必须拒。"""
    with pytest.raises(InvalidFile):
        _upload(db, data=b"hello, this is not an image at all" * 10)


def test_rejects_blocked_extension(db: Session) -> None:  # noqa: ANN001
    with pytest.raises(InvalidFile) as exc:
        _upload(db, data=b"#!/bin/sh\nrm -rf /\n", name="run.sh")
    assert "类型不允许" in str(exc.value)


def test_rejects_empty_and_oversize(db: Session) -> None:  # noqa: ANN001
    with pytest.raises(InvalidFile):
        _upload(db, data=b"")
    with pytest.raises(InvalidFile) as exc:
        _upload(db, data=b"\x89PNG\r\n\x1a\n" + b"\x00" * (storage.MAX_SIZE + 1))
    assert "5MB" in str(exc.value)


def test_pdf_by_magic(db: Session) -> None:  # noqa: ANN001
    obj, _ = _upload(db, data=b"%PDF-1.4\n" + b"\x00" * 100, name="账单.pdf")
    assert obj.content_type == "application/pdf"


def test_same_content_same_biz_is_reused(db: Session) -> None:  # noqa: ANN001
    first, d1 = _upload(db)
    second, d2 = _upload(db)
    assert d1 is False and d2 is True
    assert first.key == second.key


def test_dedup_does_not_cross_owner(db: Session, client) -> None:  # noqa: ANN001
    """同内容不同归属绝不能复用 —— 否则客户会拿到别人的文件 URL，读的时候 403。

    这个洞曾经真实存在：内部员工先传了一张图，客户再传同样的图就命中前者的记录，
    客户"上传成功"但点开是 403。
    """
    internal, d1 = _upload(db)  # client_id=None
    customer, d2 = storage.save(
        db, data=PNG_1PX, original_name="客户回单.png", declared_type="image/png",
        biz_type="TOPUP_VOUCHER", uploader_id=None, client_id=client.id,
    )
    assert d1 is False and d2 is False, "不同归属不应被判为重复"
    assert internal.key != customer.key
    assert internal.client_id is None
    assert customer.client_id == client.id


def test_dedup_within_same_owner_still_works(db: Session, client) -> None:  # noqa: ANN001
    a, d1 = storage.save(
        db, data=PNG_1PX, original_name="a.png", declared_type="image/png",
        biz_type="TOPUP_VOUCHER", uploader_id=None, client_id=client.id,
    )
    b, d2 = storage.save(
        db, data=PNG_1PX, original_name="b.png", declared_type="image/png",
        biz_type="TOPUP_VOUCHER", uploader_id=None, client_id=client.id,
    )
    assert d1 is False and d2 is True
    assert a.key == b.key


# ---------------------------------------------------------------- 路径穿越

@pytest.mark.parametrize(
    "key",
    [
        "../../data/app.db",
        "../app.db",
        "2026/10/../../../data/app.db",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "",
    ],
)
def test_safe_resolve_rejects_escape(key: str) -> None:
    with pytest.raises(InvalidFile):
        storage.safe_resolve(key)


def test_safe_resolve_rejects_encoded_traversal() -> None:
    """unquote 之后仍要拦住 %2e%2e%2f。"""
    with pytest.raises(InvalidFile):
        storage.safe_resolve("%2e%2e%2f%2e%2e%2fdata%2fapp.db")


def test_safe_resolve_accepts_normal_key(db: Session) -> None:  # noqa: ANN001
    obj, _ = _upload(db)
    assert storage.safe_resolve(obj.key).is_file()


# ---------------------------------------------------------------- 凭证闸口

def _topup(db: Session, client, **kw):  # noqa: ANN001, ANN003
    return create_topup(db, client_id=client.id, amount="800", currency="USD", **kw)


def test_confirm_without_voucher_is_blocked(db: Session, client, wallet) -> None:  # noqa: ANN001
    t = _topup(db, client)
    assert t.status is TopUpStatus.PENDING_PAY
    with pytest.raises(StateError) as exc:
        confirm_topup(db, t.id, operator="财务")
    assert "凭证" in str(exc.value)
    assert money(wallet.balance) == money("0")
    assert db.query(WalletTransaction).filter_by(wallet_id=wallet.id).count() == 0


def test_confirm_without_voucher_short_reason_still_blocked(db: Session, client) -> None:  # noqa: ANN001
    t = _topup(db, client)
    with pytest.raises(StateError):
        confirm_topup(db, t.id, operator="财务", no_voucher_reason="现金")  # 2 字，不够


def test_confirm_without_voucher_but_valid_reason_passes_and_audits(db: Session, client, wallet) -> None:  # noqa: ANN001
    t = _topup(db, client)
    confirm_topup(db, t.id, operator="财务-A", no_voucher_reason="线下现金收款，客户已签字确认")

    assert money(wallet.balance) == money("800")
    tx = db.query(WalletTransaction).filter_by(wallet_id=wallet.id).one()
    assert "无凭证确认" in (tx.remark or "")
    log = db.query(AuditLog).filter_by(action="CONFIRM_NO_VOUCHER", doc_id=t.id).one()
    assert "线下现金" in (log.reason or "")
    assert log.actor == "财务-A"


def test_confirm_with_voucher_passes(db: Session, client, wallet) -> None:  # noqa: ANN001
    obj, _ = _upload(db)
    t = _topup(db, client, voucher=storage.url_of(obj))
    confirm_topup(db, t.id, operator="财务")
    assert money(wallet.balance) == money("800")
    tx = db.query(WalletTransaction).filter_by(wallet_id=wallet.id).one()
    assert tx.voucher == storage.url_of(obj)


def test_confirm_with_missing_voucher_file_is_blocked(db: Session, client) -> None:  # noqa: ANN001
    """凭证指向的文件被删了 → 不得确认，否则账本里留下一条打不开的凭证。"""
    obj, _ = _upload(db)
    storage.safe_resolve(obj.key).unlink()
    t = _topup(db, client, voucher=storage.url_of(obj))
    with pytest.raises(StateError) as exc:
        confirm_topup(db, t.id, operator="财务")
    assert "已丢失" in str(exc.value)


def test_legacy_voucher_path_skips_existence_check(db: Session, client, wallet) -> None:  # noqa: ANN001
    """历史数据（如 seed/voucher.png）不做存在性校验，否则老单永远确认不了。"""
    t = _topup(db, client, voucher="seed/voucher.png")
    confirm_topup(db, t.id, operator="财务")
    assert money(wallet.balance) == money("800")


def test_idempotent_confirm_still_works_and_ignores_voucher_rule(db: Session, client, wallet) -> None:  # noqa: ANN001
    """已到账的单据重复确认：幂等返回，不能被新的凭证规则反复拦。"""
    obj, _ = _upload(db)
    t = _topup(db, client, voucher=storage.url_of(obj))
    confirm_topup(db, t.id, operator="财务-A")
    t.voucher = None  # 模拟事后凭证被清空
    confirm_topup(db, t.id, operator="财务-B")
    assert money(wallet.balance) == money("800")
    assert db.query(WalletTransaction).filter_by(wallet_id=wallet.id).count() == 1


def test_voucher_url_helpers() -> None:
    assert storage.is_file_url("/api/files/2026/10/a.png") is True
    assert storage.is_file_url("seed/a.png") is False
    assert storage.is_file_url(None) is False
    assert storage.key_from_url("/api/files/2026/10/a.png") == "2026/10/a.png"
    with pytest.raises(InvalidFile):
        storage.key_from_url("seed/a.png")
    # 非 file_url 视为「不做校验」
    assert storage.exists_on_disk("seed/a.png") is True
    assert storage.exists_on_disk("/api/files/2026/10/nope.png") is False
