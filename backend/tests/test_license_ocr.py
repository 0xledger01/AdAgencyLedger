"""证照 OCR：字段抽取与降级路径。

这些用例不跑真实推理（秒级、且依赖模型文件），而是喂「版面坐标 + 文本」，
守的是抽取规则：名称/证号/注册地/地址/日期从哪来、抽不到时怎么诚实降级。
真实图片回归见 docs/DESIGN.md「证照 OCR」一节的手工验收步骤。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.models.client import Client
from backend.app.services import license_ocr, openreq, storage


def _line(text: str, x0: float, y0: float, x1: float, y1: float, score: float = 0.95):
    return license_ocr.OcrLine(text, x0, y0, x1, y1, score)


# ---------------------------------------------------------------- 信用代码

def test_uscc_checksum_ok() -> None:
    # 18 位、字符集合规、校验位正确（测试构造，非真实企业）
    assert license_ocr.uscc_checksum_ok("91310115MA1K4T357R")


def test_uscc_checksum_rejects_bad_last_char() -> None:
    assert not license_ocr.uscc_checksum_ok("91310115MA1K4T357X")
    assert not license_ocr.uscc_checksum_ok("91310115MA1K4T357")


def test_uscc_recovers_o_i_confusion() -> None:
    """OCR 常把 0 认成 O、1 认成 I，先纠正再验校验位。"""
    code, ok = license_ocr._find_uscc("统一社会信用代码 9I31O115MA1K4T357R")
    assert ok is True
    assert code == "91310115MA1K4T357R"


# ---------------------------------------------------------------- 日期

def test_norm_date_variants() -> None:
    assert license_ocr._norm_date("20/07/2025") == "2025-07-20"
    assert license_ocr._norm_date("2025年7月20日") == "2025-07-20"
    assert license_ocr._norm_date("2025-07-20") == "2025-07-20"
    assert license_ocr._norm_date("长期") == ""


# ---------------------------------------------------------------- 香港 BR

def _hk_lines() -> list[license_ocr.OcrLine]:
    """按 3174×4490 的 BR 缩到 990×1400 后的版式复刻。"""
    rows = [
        ("Business XXXXXRegistration Certificate", 65, 189, 400, 205, 0.86),
        ("業務／法團所用名稱", 67, 241, 160, 258, 0.70),
        ("某某公司", 186, 241, 400, 258, 0.76),
        ("Name of Business/", 67, 252, 220, 270, 0.88),
        ("EXAMPLE TRADING CO., LIMITED", 187, 252, 420, 270, 0.92),
        ("Corporation", 68, 269, 140, 286, 0.90),
        ("地址", 65, 364, 100, 380, 0.67),
        ("RM 1012 10/F FRONT BLOCK MING", 186, 364, 470, 380, 0.92),
        ("Address", 65, 379, 130, 396, 0.87),
        ("SANG INDUSTRIAL BUILDING", 187, 386, 470, 402, 0.91),
        ("19 HING YIP STREET KWUN TONG", 185, 404, 470, 420, 0.86),
        ("KL", 185, 425, 300, 441, 0.66),
        ("業務性質", 64, 447, 160, 463, 0.75),
        ("服裝，飾品銷售", 183, 447, 400, 463, 0.70),
        ("法律地位", 63, 508, 130, 524, 0.77),
        ("BODY CORPORATE", 185, 508, 400, 524, 0.87),
        ("Date of Commencement", 63, 552, 180, 568, 0.90),
        ("Date of Expiry", 201, 551, 275, 567, 0.85),
        ("Certificate No.", 313, 554, 430, 570, 0.87),
        ("20/07/2025", 63, 569, 150, 585, 0.84),
        ("19/07/2026", 202, 570, 290, 586, 0.90),
        ("75531145-000-07-25-0", 314, 571, 520, 587, 0.83),
        ("Fee and Levy", 551, 553, 700, 569, 0.87),
    ]
    return [_line(*row) for row in rows]


def test_hk_br_fields() -> None:
    result = license_ocr.extract_fields(_hk_lines(), (990, 1400))
    assert result["doc_type"] == "HK_BR"
    assert result["parsed"] is True
    assert result["name"] == "某某公司"
    assert result["name_en"] == "EXAMPLE TRADING CO., LIMITED"
    assert result["credit_code"] == "75531145-000-07-25-0"
    assert result["registered_place"] == "HK"
    assert result["address"].startswith("RM 1012 10/F FRONT BLOCK MING")
    assert result["address"].endswith("KL")
    assert result["business_nature"] == "服裝，飾品銷售"
    assert result["valid_from"] == "2025-07-20"
    assert result["valid_to"] == "2026-07-19"


def test_hk_br_missing_code_warns_not_fabricates() -> None:
    lines = [ln for ln in _hk_lines() if "75531145" not in ln.text]
    result = license_ocr.extract_fields(lines, (990, 1400))
    assert result["name"] == "某某公司"
    assert result["credit_code"] == ""
    assert any("登记证号" in w for w in result["warnings"])


# ---------------------------------------------------------------- 大陆营业执照

def _cn_lines() -> list[license_ocr.OcrLine]:
    code = "91310115MA1K4T357R"
    rows = [
        ("营业执照", 60, 40, 260, 80, 0.93),
        ("统一社会信用代码", 60, 120, 300, 150, 0.90),
        (code, 330, 120, 900, 150, 0.91),
        ("名称", 60, 180, 140, 210, 0.95),
        ("某某公司", 330, 180, 900, 210, 0.94),
        ("类型", 60, 240, 200, 270, 0.92),
        ("有限责任公司", 330, 240, 700, 270, 0.93),
        ("住所", 60, 300, 200, 330, 0.92),
        ("上海市浦东新区示例路1号", 330, 300, 900, 330, 0.90),
        ("法定代表人", 60, 360, 220, 390, 0.91),
        ("张三", 330, 360, 500, 390, 0.95),
        ("注册资本", 60, 420, 220, 450, 0.90),
        ("人民币100万元", 330, 420, 700, 450, 0.89),
        ("成立日期", 60, 480, 220, 510, 0.92),
        ("2020年01月08日", 330, 480, 700, 510, 0.93),
        ("营业期限", 60, 540, 220, 570, 0.90),
        ("2020年01月08日至2040年01月07日", 330, 540, 900, 570, 0.88),
        ("经营范围", 60, 600, 220, 630, 0.90),
        ("广告设计、代理；广告发布", 330, 600, 900, 630, 0.87),
    ]
    return [_line(*row) for row in rows]


def test_cn_license_fields() -> None:
    result = license_ocr.extract_fields(_cn_lines(), (1400, 1000))
    assert result["doc_type"] == "CN_LICENSE"
    assert result["name"] == "某某公司"
    assert result["credit_code"] == "91310115MA1K4T357R"
    assert result["registered_place"] == "CN"
    assert result["address"] == "上海市浦东新区示例路1号"
    assert result["legal_person"] == "张三"
    assert result["capital"] == "人民币100万元"
    assert result["valid_from"] == "2020-01-08"
    assert result["valid_to"] == "2040-01-07"


def test_unknown_document_stays_empty() -> None:
    lines = [_line("随便一张小票", 10, 10, 200, 30), _line("合计 25 元", 10, 40, 200, 60)]
    result = license_ocr.extract_fields(lines, (600, 400))
    assert result["doc_type"] == "UNKNOWN"
    assert result["parsed"] is False
    assert result["name"] == ""
    assert result["credit_code"] == ""


def test_cn_merged_label_and_value_boxes() -> None:
    """扫描件窄的时候 OCR 会把「名称」和值并成一个框，也要能取到。"""
    rows = [
        ("营业执照", 60, 40, 260, 80),
        ("统一社会信用代码 91310115MA1K4T357R", 60, 120, 900, 150),
        ("名称 某某公司", 60, 180, 900, 210),
        ("住所 上海市浦东新区示例路1号", 60, 300, 900, 330),
    ]
    result = license_ocr.extract_fields([_line(*r) for r in rows], (1400, 1000))
    assert result["doc_type"] == "CN_LICENSE"
    assert result["name"] == "某某公司"
    assert result["credit_code"] == "91310115MA1K4T357R"
    assert result["address"] == "上海市浦东新区示例路1号"


# ---------------------------------------------------------------- 降级路径

def test_parse_bytes_disabled(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(license_ocr.settings, "ocr_enabled", False)
    result = license_ocr.parse_bytes(b"\x89PNG\r\n\x1a\n")
    assert result["parsed"] is False
    assert result["source"] == "disabled"
    assert "OCR" in result["note"]


def test_parse_file_missing(tmp_path) -> None:  # noqa: ANN001
    result = license_ocr.parse_file(tmp_path / "nope.png")
    assert result["parsed"] is False
    assert result["source"] == "missing"


def test_parse_bytes_engine_missing_degrades(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(license_ocr, "_get_engine", lambda: None)
    monkeypatch.setattr(license_ocr, "_engine_error", "ImportError: no engine")
    result = license_ocr.parse_bytes(b"not-an-image")
    assert result["parsed"] is False
    assert result["source"] == "unavailable"
    assert "手动填写" in result["note"]


def test_parse_bytes_retries_once_on_empty_ocr(monkeypatch) -> None:  # noqa: ANN001
    """偶发一次推理空转要重试，不能直接判「没有文字」。"""
    calls: list[int | None] = []

    def fake_read(data, *, max_side=None):  # noqa: ANN001, ANN202
        calls.append(max_side)
        if len(calls) == 1:
            return [], (900, 1400)
        return _hk_lines(), (990, 1400)

    monkeypatch.setattr(license_ocr, "read_lines", fake_read)
    result = license_ocr.parse_bytes(b"fake-image")
    assert len(calls) == 2
    assert result["parsed"] is True
    assert result["name"] == "某某公司"


# ---------------------------------------------------------------- 与开户单衔接

def _upload_license(db: Session, client: Client):  # noqa: ANN001, ANN202
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    obj, _ = storage.save(
        db,
        data=png,
        original_name="执照.png",
        declared_type="image/png",
        biz_type="LICENSE",
        uploader_id=None,
        client_id=client.id,
    )
    return obj


def test_license_hints_prefers_ocr_and_fills_from_profile(db: Session, client: Client, monkeypatch) -> None:  # noqa: ANN001
    obj = _upload_license(db, client)
    client.license_url = storage.url_of(obj)
    client.credit_code = "91310115MA1K4T357R"
    client.registered_place = "CN"
    db.flush()

    monkeypatch.setattr(
        license_ocr,
        "parse_file",
        lambda *a, **k: {
            "parsed": True,
            "source": "ocr",
            "doc_type": "CN_LICENSE",
            "doc_type_label": "中国大陆营业执照",
            "name": "某某公司",
            "credit_code": "",
            "registered_place": "",
            "note": "OCR 识别为中国大陆营业执照，名称已带出，请核对后提交",
        },
    )
    result = openreq.license_hints(db, client=client, file_obj=obj)
    assert result["source"] == "ocr"
    assert result["name"] == "某某公司"
    # OCR 空着的字段按档案补齐
    assert result["credit_code"] == "91310115MA1K4T357R"
    assert result["registered_place"] == "CN"
    assert "档案补齐" in result["note"]


def test_license_hints_falls_back_to_profile(db: Session, client: Client, monkeypatch) -> None:  # noqa: ANN001
    obj = _upload_license(db, client)
    client.license_url = storage.url_of(obj)
    client.credit_code = "91310115MA1K4T357R"
    db.flush()

    monkeypatch.setattr(
        license_ocr,
        "parse_file",
        lambda *a, **k: {"parsed": False, "note": "未识别到证照文字", "warnings": []},
    )
    result = openreq.license_hints(db, client=client, file_obj=obj)
    assert result["source"] == "profile"
    assert result["name"] == client.company_name
    assert result["credit_code"] == "91310115MA1K4T357R"
    assert "未识别" in result["note"]
