"""证照 OCR：营业执照 / 香港商业登记证（BR）读图并抽字段。

设计取舍：

* 引擎用 ``rapidocr-onnxruntime``（PP-OCRv3 的 ONNX 版，模型随 wheel 附带，
  **完全离线**，不需要 Key、不上传第三方）。没装引擎时 ``parsed=False``，
  由调用方退回文件名/档案提示 —— 绝不编造识别结果。
* 只读图，不落中间文件；结果按文件 ``sha256`` 做进程内小缓存（同一张图
  重复选择历史文件时不再跑一遍 8 秒推理）。
* 版面假设：字段名在左列、值在右侧同一行或紧随其下的块。字段名与值同框
  （``名称：xxx``）时也能取到。
* 抽不到的字段一律留空字符串，并写进 ``warnings``，前端提示人工核对。
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from ..config import settings

ENGINE_NAME = "rapidocr-onnxruntime"

# 证照识别是单张图的小任务，别让 BLAS 按核数开一堆线程把内存吃光
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 结果缓存

_CACHE_LIMIT = 32
_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()

_engine = None
_engine_error: str | None = None
_engine_lock = threading.Lock()


@dataclass(frozen=True)
class OcrLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


# ---------------------------------------------------------------- 文本工具

_CJK = re.compile(r"[\u3400-\u9fff]")


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _compact(text: str) -> str:
    """比对用：去空白、去分隔符、转小写。"""
    text = _nfkc(text).lower()
    return re.sub(r"[\s\u3000:：.。,，、;；/／|·•\-—_()（）\[\]【】]+", "", text)


def _clean_value(text: str) -> str:
    text = _nfkc(text).strip()
    text = re.sub(r"^[\s:：;；,，.。、|]+", "", text)
    text = re.sub(r"[\s\u3000]+", " ", text)
    # NFKC 会把中文全角逗号压成半角，两个汉字之间的还原回去，别把原文改花
    text = re.sub(r"(?<=[\u4e00-\u9fff]),(?=[\u4e00-\u9fff])", "，", text)
    return text.strip()


def _join(parts: list[str]) -> str:
    """把同一块的碎片拼起来：中文之间不留空格，英文之间留一个。"""
    out = ""
    for raw in parts:
        part = _clean_value(raw)
        if not part:
            continue
        if not out:
            out = part
        elif _CJK.search(out[-1]) or _CJK.match(part):
            out += part
        else:
            out += " " + part
    return out


# ---------------------------------------------------------------- 引擎


def _get_engine():
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
            _engine_error = None
        except Exception as exc:  # noqa: BLE001 - 引擎缺失要降级，不能炸接口
            _engine_error = f"{type(exc).__name__}: {exc}"
            _engine = None
    return _engine


def engine_status() -> dict:
    """给运维看的探针，不做业务判断。"""
    engine = _get_engine()
    return {
        "engine": ENGINE_NAME,
        "available": engine is not None,
        "error": _engine_error,
    }


def _prepare_image(data: bytes, max_side: int | None = None):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 30_000_000
    img = Image.open(io.BytesIO(data))
    # 先看头部尺寸再解码：防「5MB 的 PNG 解出 2 亿像素」把内存吃光
    if img.size[0] * img.size[1] > 30_000_000:
        raise ValueError("图片像素过大，请压缩后再上传")
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    img = img.convert("RGB")

    w, h = img.size
    long_side = max(w, h)
    limit = max(600, int(max_side or getattr(settings, "ocr_max_side", 1400)))
    scale = 1.0
    if long_side > limit:
        scale = limit / long_side
    elif long_side < 1000:  # 小图放大，检测框更稳
        scale = min(2.0, 1200 / long_side)
    if abs(scale - 1.0) > 0.02:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return img


def _lines_from_result(result, min_score: float) -> list[OcrLine]:
    lines: list[OcrLine] = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, ValueError, IndexError):
            continue
        text = _nfkc(str(text)).strip()
        if not text:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        lines.append(OcrLine(text, min(xs), min(ys), max(xs), max(ys), score))
    return lines


def read_lines(data: bytes, *, max_side: int | None = None) -> tuple[list[OcrLine], tuple[int, int]]:
    """跑一次 OCR，返回（文本行，图像尺寸）。引擎缺失时抛 RuntimeError。"""
    engine = _get_engine()
    if engine is None:
        raise RuntimeError(_engine_error or "OCR 引擎未安装")

    img = _prepare_image(data, max_side)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result, _elapse = engine(buf.getvalue())
    min_score = float(getattr(settings, "ocr_min_score", 0.45))
    return _lines_from_result(result, min_score), img.size


# ---------------------------------------------------------------- 标签识别

# 英文 / 繁体 / 简体三套写法都收，双语证照任一侧被识别到都能锚定
LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "Name of Business", "NameofBusiness", "Corporation",
        "業務／法團所用名稱", "業務/法團所用名稱", "业务／法团所用名称",
        "名稱", "名称", "企业名称", "公司名称",
    ),
    "branch": ("Business/Branch Name", "BranchName", "分行名稱", "分行名称"),
    "address": ("Address", "地址", "住所", "营业场所", "經營場所", "经营场所", "注册地址"),
    "nature": ("Nature of Business", "業務性質", "业务性质", "经营范围"),
    "status": ("Status", "法律地位", "主体类型", "类型"),
    "commence": ("Date of Commencement", "生效日期", "成立日期", "注册日期"),
    "expiry": ("Date of Expiry", "屆滿日期", "届满日期", "营业期限", "经营期限"),
    "cert": ("Certificate No.", "CertificateNo", "登記證號碼", "登记证号码", "统一社会信用代码", "信用代码"),
    "legal": ("法定代表人", "法人代表", "经营者", "负责人", "投资人"),
    "capital": ("注册资本", "註冊資本", "出资额"),
    "scope": ("经营范围", "經營範圍"),
    "authority": ("登记机关", "發照機關", "发照机关"),
    "fee": ("Fee and Levy", "登記費及徵費", "登记费及征费"),
    "form": ("Business Registration", "商業登記", "商业登记", "营业执照", "營業執照"),
}

_ALL_ALIASES = tuple(dict.fromkeys(a for group in LABEL_ALIASES.values() for a in group))


def _label_quality(text: str, alias: str) -> float:
    """1.0 = 整框就是字段名；0.3 = 长句里顺带出现，不能当锚点。"""
    c = _compact(text)
    a = _compact(alias)
    if not a or not c:
        return 0.0
    if c == a:
        return 1.0
    if c.startswith(a) and len(c) - len(a) <= 2:
        return 0.9
    # 「名称：深圳市xxx有限公司」这类字段名和值同框，仍算合法锚点
    if c.startswith(a) and len(c) - len(a) <= 40:
        return 0.8
    if len(a) >= 4 and a in c and len(c) <= len(a) + 4:
        return 0.75
    if len(a) >= 4 and a in c:
        return 0.3
    return 0.0


def _find_label(lines: list[OcrLine], keys: tuple[str, ...], width: float) -> OcrLine | None:
    best: tuple[float, float, float, OcrLine] | None = None
    for ln in lines:
        quality = max((_label_quality(ln.text, a) for a in keys), default=0.0)
        if quality < 0.7:
            continue
        left = 1.0 if ln.x0 < width * 0.40 else 0.0
        rank = (quality, left, ln.score)
        if best is None or rank > best[:3]:
            best = (*rank, ln)
    return best[3] if best else None


def _is_label_like(line: OcrLine) -> bool:
    return max((_label_quality(line.text, a) for a in _ALL_ALIASES), default=0.0) >= 0.7


def _line_height(lines: list[OcrLine]) -> float:
    heights = [ln.height for ln in lines if ln.height > 2]
    return median(heights) if heights else 20.0


def _value_in_same_row(label: OcrLine, lines: list[OcrLine], width: float) -> list[OcrLine]:
    """只认同一条基线：HK 的字段名常常纵向跨两行，把下面那行值也算进来就跑偏了。"""
    x_min = label.x0 + max(60.0, width * 0.035)
    center_tol = min(label.height, 24.0) * 0.35
    picked: list[OcrLine] = []
    for ln in lines:
        if ln is label or _is_label_like(ln):
            continue
        if ln.x0 < x_min:
            continue
        overlap = min(ln.y1, label.y1) - max(ln.y0, label.y0)
        if overlap < min(ln.height, label.height) * 0.6:
            continue
        if abs(ln.cy - label.cy) > center_tol:
            continue
        picked.append(ln)
    picked.sort(key=lambda l: l.x0)
    return picked


def _value_block(label: OcrLine, lines: list[OcrLine], width: float, lh: float) -> list[OcrLine]:
    column_tol = max(18.0, width * 0.02)
    below = [
        ln.y0
        for ln in lines
        if ln is not label
        and abs(ln.x0 - label.x0) <= column_tol
        and ln.y0 > label.y1 + 2
    ]
    # 没有下一个字段名时最多往下吃 3 行，免得把相邻区块（费用说明、下一栏）吞进来
    fallback_end = label.y1 + 3.0 * lh
    end = min(below) if below else fallback_end
    end = min(end, fallback_end)
    start = label.y0 - 1.1 * lh  # 值有时比字段名略高（HK 地址块）
    x_min = label.x0 + max(60.0, width * 0.035)

    picked = [
        ln
        for ln in lines
        if ln is not label
        and not _is_label_like(ln)
        and ln.x0 >= x_min
        and start <= ln.cy <= end
    ]
    picked.sort(key=lambda l: (round(l.cy / max(lh, 1.0)), l.x0))
    return picked


def _label_text(label: OcrLine, keys: tuple[str, ...]) -> str:
    """字段名和值同框时，取字段名后面的那段。"""
    text = _nfkc(label.text)
    compact_full = _compact(text)
    for alias in sorted(keys, key=len, reverse=True):
        a = _compact(alias)
        if not a or not compact_full.startswith(a):
            continue
        # 逐字符累计 compact，找到刚好覆盖到别名结尾的位置
        acc = ""
        cut = 0
        for i, ch in enumerate(text):
            acc += _compact(ch)
            if len(acc) >= len(a):
                cut = i + 1
                break
        remainder = _clean_value(text[cut:])
        if remainder and len(remainder) >= 2:
            return remainder
    return ""


def _field_text(
    lines: list[OcrLine],
    key: str,
    width: float,
    lh: float,
    *,
    same_row_only: bool = False,
) -> str:
    keys = LABEL_ALIASES[key]
    label = _find_label(lines, keys, width)
    if label is None:
        return ""
    inline = _label_text(label, keys)
    if inline:
        return inline
    row = _value_in_same_row(label, lines, width)
    if row:
        return _join([ln.text for ln in row])
    if same_row_only:
        return ""
    block = _value_block(label, lines, width, lh)
    return _join([ln.text for ln in block])


# ---------------------------------------------------------------- 字段清洗

_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{4})"),
)

# 香港 BR：20/07/2025；大陆执照：2025年07月20日。两个正则分开收，别互相误吃。
_DMY_RE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\b")
_YMD_RE = re.compile(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")


def _norm_date(text: str) -> str:
    """任意写法 → YYYY-MM-DD；非法返回空。"""
    s = _nfkc(text or "").strip()
    if not s:
        return ""
    for pat in _DATE_PATTERNS:
        m = pat.search(s)
        if m:
            y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
            if 1900 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


_USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_USCC_OCR_FIX = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "Z": "2", "V": "U"})


def uscc_checksum_ok(code: str) -> bool:
    code = (code or "").strip().upper()
    if len(code) != 18 or any(ch not in _USCC_CHARSET for ch in code):
        return False
    total = sum(_USCC_CHARSET.index(ch) * w for ch, w in zip(code[:17], _USCC_WEIGHTS))
    expect = (31 - total % 31) % 31
    return _USCC_CHARSET[expect] == code[-1]


def _find_uscc(text: str) -> tuple[str, bool]:
    """返回（代码，校验位是否通过）。OCR 易把 0/1/5 认成 O/I/S，先纠正再验。"""
    compact = re.sub(r"[\s\-]", "", _nfkc(text).upper())
    # 英文抬头里也能凑出 18 个字母数字，必须要求足够多的数字才像信用代码
    candidates = [c for c in re.findall(r"[0-9A-Z]{18}", compact) if sum(ch.isdigit() for ch in c) >= 8]
    for candidate in candidates:
        if uscc_checksum_ok(candidate):
            return candidate, True
    for candidate in candidates:
        fixed = candidate.translate(_USCC_OCR_FIX)
        if uscc_checksum_ok(fixed):
            return fixed, True
        if all(ch in _USCC_CHARSET for ch in fixed[:17]):
            return fixed, False
    m = re.search(r"\b\d{15}\b", compact)  # 老版 15 位注册号
    if m:
        return m.group(0), False
    return "", False


def _find_br_no(text: str) -> str:
    compact = re.sub(r"[\s]", "", _nfkc(text))
    m = re.search(r"(\d{8})-(\d{3})-(\d{2})-(\d{2})-(\d)", compact)
    if m:
        return "-".join(m.groups())
    return ""


def _ordered_date_tokens(lines: list[OcrLine], pattern: re.Pattern[str], lh: float) -> list[str]:
    """按阅读顺序收集日期串，去掉重复。"""
    seen: list[str] = []
    for ln in sorted(lines, key=lambda l: (round(l.cy / max(lh, 1.0)), l.x0)):
        for m in pattern.finditer(_nfkc(ln.text)):
            token = _norm_date(m.group(0))
            if token and token not in seen:
                seen.append(token)
    return seen


_CN_SUFFIX = re.compile(
    r"(有限公司|有限责任公司|股份有限公司|集团有限公司|集团|公司|商行|商店|经营部|厂|中心|工作室|合作社|事务所|研究院)"
)
_EN_NAME = re.compile(r"^[A-Z0-9&.,'()/\- ]{4,90}(LIMITED|LTD\.?|CO\.?|COMPANY|CORP\.?)$")
_NAME_PREFIX = re.compile(r"^\s*(企业名称|公司名称|名称|商号|商號)\s*[:：]?\s*")


def _looks_like_cn_name(text: str) -> bool:
    t = _NAME_PREFIX.sub("", _clean_value(text))
    if not (2 <= len(t) <= 48):
        return False
    if not _CJK.search(t):
        return False
    if not _CN_SUFFIX.search(t):
        return False
    # 版面里的说明性长句不算
    return not re.search(r"[，。；]", t)


def _pick_company_name(lines: list[OcrLine], width: float, doc_type: str) -> tuple[str, str]:
    """返回（中文名，英文名）。中文优先，因为开户主体用的是注册中文全称。"""
    cn = ""
    en = ""

    label = _find_label(lines, LABEL_ALIASES["name"], width)
    if label is not None:
        inline = _label_text(label, LABEL_ALIASES["name"])
        if inline and _looks_like_cn_name(inline):
            cn = inline
        elif inline and _EN_NAME.match(inline.upper()):
            en = inline

        if not cn or not en:
            lh = _line_height(lines)
            near = _value_in_same_row(label, lines, width) + _value_block(label, lines, width, lh)
            for ln in sorted(near, key=lambda l: (round(l.cy), l.x0)):
                text = _clean_value(ln.text)
                if not cn and _looks_like_cn_name(text):
                    cn = text
                elif not en and _EN_NAME.match(text.upper()):
                    en = text

    if not cn:
        for ln in sorted(lines, key=lambda l: (l.y0, l.x0)):
            text = _NAME_PREFIX.sub("", _clean_value(ln.text))
            if _is_label_like(ln):
                continue
            if _looks_like_cn_name(text) and not re.search(r"(地址|住所|经营范围)", text):
                cn = text
                break

    if not en:
        for ln in sorted(lines, key=lambda l: (l.y0, l.x0)):
            text = _clean_value(ln.text).upper()
            if _EN_NAME.match(text):
                en = _clean_value(ln.text)
                break

    return cn, en


# ---------------------------------------------------------------- 抽取主流程


def _doc_type(compact: str) -> str:
    if (
        re.search(r"business.{0,10}registration", compact)
        or "商業登記" in compact
        or "商业登记" in compact
        or re.search(r"\d{8}-\d{3}-\d{2}-\d{2}-\d", compact)
    ):
        return "HK_BR"
    if "营业执照" in compact or "營業執照" in compact or "统一社会信用代码" in compact:
        return "CN_LICENSE"
    return "UNKNOWN"


DOC_TYPE_LABEL = {
    "HK_BR": "香港商业登记证（BR）",
    "CN_LICENSE": "中国大陆营业执照",
    "UNKNOWN": "未识别证照类型",
}


def _place_from(compact: str, doc_type: str) -> str:
    if doc_type == "HK_BR":
        return "HK"
    if re.search(r"香港|hongkong|kowloon|newterritories", compact):
        return "HK"
    if re.search(r"台湾|臺灣|taiwan", compact):
        return "TW"
    if re.search(r"新加坡|singapore", compact):
        return "SG"
    if doc_type == "CN_LICENSE":
        return "CN"
    return ""


def extract_fields(lines: list[OcrLine], size: tuple[int, int]) -> dict:
    width, height = size
    lh = _line_height(lines)
    text = " ".join(ln.text for ln in lines)
    compact = _compact(text)
    doc_type = _doc_type(compact)

    warnings: list[str] = []
    fields: dict[str, str] = {}

    cn_name, en_name = _pick_company_name(lines, width, doc_type)
    name = cn_name or en_name
    if not name:
        warnings.append("未定位到企业名称")

    credit = ""
    credit_ok = None
    if doc_type == "HK_BR":
        credit = _find_br_no(text) or _field_text(lines, "cert", width, lh, same_row_only=False)
        credit = credit.strip()
        if not credit:
            warnings.append("未识别到登记证号")
    else:
        credit, credit_ok = _find_uscc(text)
        if not credit:
            credit = _field_text(lines, "cert", width, lh)
            credit = re.sub(r"[\s]", "", credit)
        if not credit:
            warnings.append("未识别到统一社会信用代码/注册号")
        elif credit_ok is False:
            warnings.append("信用代码校验位不匹配，请逐位核对")

    address = _field_text(lines, "address", width, lh)
    if not address:
        warnings.append("未识别到地址")

    if doc_type == "HK_BR":
        # BR 的生效/届满两个日期就在同一行或相邻一行，按阅读顺序取前两个最稳；
        # 几何锚点只在顺序法失败时兜底（繁体标签常被 OCR 认花）。
        dates = _ordered_date_tokens(lines, _DMY_RE, lh)
        valid_from = dates[0] if dates else _norm_date(_field_text(lines, "commence", width, lh))
        valid_to = dates[1] if len(dates) >= 2 else ""
        if valid_from and valid_to and valid_from > valid_to:
            valid_from, valid_to = valid_to, valid_from
    else:
        valid_from = _norm_date(_field_text(lines, "commence", width, lh))
        term = _field_text(lines, "expiry", width, lh)
        term_dates = [_norm_date(x) for x in _YMD_RE.findall(_nfkc(term))]
        term_dates = [d for d in term_dates if d]
        valid_to = "" if "长期" in term else (term_dates[-1] if len(term_dates) >= 2 else "")
        if not valid_from:
            any_dates = _ordered_date_tokens(lines, _YMD_RE, lh)
            if any_dates:
                valid_from = any_dates[0]
        if not valid_from:
            warnings.append("未识别到成立日期")

    legal = _field_text(lines, "legal", width, lh)
    capital = _field_text(lines, "capital", width, lh)
    nature = _field_text(lines, "nature", width, lh)
    if doc_type == "HK_BR":
        nature = nature or ""
    status_text = _field_text(lines, "status", width, lh)

    place = _place_from(compact, doc_type)

    parsed = bool(name or credit)
    if parsed and not (name and credit):
        warnings.append("名称与证号未同时识别，请补齐")

    scores = [ln.score for ln in lines]
    confidence = round(sum(scores) / len(scores), 3) if scores else 0.0

    fields = {
        "name": name,
        "name_en": en_name if en_name and en_name != name else "",
        "credit_code": credit,
        "registered_place": place,
        "address": address,
        "legal_person": legal,
        "capital": capital,
        "business_nature": nature,
        "entity_status": status_text,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }

    return {
        "parsed": parsed,
        "source": "ocr",
        "doc_type": doc_type,
        "doc_type_label": DOC_TYPE_LABEL[doc_type],
        "confidence": confidence,
        "warnings": warnings,
        "lines": [ln.text for ln in sorted(lines, key=lambda l: (round(l.cy / max(lh, 1)), l.x0))],
        **fields,
    }


# ---------------------------------------------------------------- 对外入口


def _note_of(result: dict) -> str:
    if not result.get("parsed"):
        return "已读取证照但未识别到关键信息，请手动填写"
    label = result.get("doc_type_label") or "证照"
    bits = []
    if result.get("name"):
        bits.append("名称")
    if result.get("credit_code"):
        bits.append("证号" if result.get("doc_type") == "HK_BR" else "信用代码")
    head = f"OCR 识别为{label}"
    if bits:
        head += "，" + "/".join(bits) + "已带出"
    if result.get("warnings"):
        head += "；" + "；".join(result["warnings"][:3])
    return head + "，请核对后提交"


def _empty(source: str, note: str, error: str = "") -> dict:
    return {
        "parsed": False,
        "source": source,
        "doc_type": "UNKNOWN",
        "doc_type_label": DOC_TYPE_LABEL["UNKNOWN"],
        "name": "",
        "name_en": "",
        "credit_code": "",
        "registered_place": "",
        "address": "",
        "legal_person": "",
        "capital": "",
        "business_nature": "",
        "entity_status": "",
        "valid_from": "",
        "valid_to": "",
        "confidence": 0.0,
        "warnings": [],
        "lines": [],
        "note": note,
        "engine": ENGINE_NAME,
        "engine_error": error,
    }


def parse_bytes(data: bytes, *, sha256: str | None = None) -> dict:
    """对图片字节跑 OCR + 抽取。任何失败都返回带 note 的降级结果。"""
    if not getattr(settings, "ocr_enabled", True):
        return _empty("disabled", "本机未启用证照 OCR，请手动填写")
    if not data:
        return _empty("empty", "文件为空，无法识别")

    key = sha256 or ""
    if key:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None:
                _cache.move_to_end(key)
                cached = dict(hit)
                cached["cached"] = True
                return cached

    started = time.perf_counter()
    try:
        lines, size = read_lines(data)
        if len(lines) < 5:
            # 偶发一次推理空转（CPU/内存抖动）不能当成「这张证照没有字」。
            # 换小一点再试一次，仍为空才降级。
            log.warning("证照 OCR 首次只出 %d 行，缩小重试一次", len(lines))
            retry_lines, retry_size = read_lines(data, max_side=1100)
            if len(retry_lines) > len(lines):
                lines, size = retry_lines, retry_size
    except RuntimeError as exc:
        return _empty("unavailable", f"本机未就绪 OCR 引擎（{exc}），请手动填写", str(exc))
    except Exception as exc:  # noqa: BLE001 - 坏图/解码失败都按未识别处理
        return _empty("error", f"证照读图失败（{type(exc).__name__}），请手动填写", str(exc))

    result = extract_fields(lines, size)
    result["note"] = _note_of(result)
    result["engine"] = ENGINE_NAME
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    result["image_size"] = list(size)
    result["lines"] = result.get("lines", [])[:120]

    if key:
        with _cache_lock:
            _cache[key] = result
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
    return dict(result)


def parse_file(path: Path | str, *, sha256: str | None = None) -> dict:
    """按扩展名/内容分流：图片直接读，PDF 需要 pypdfium2（可选）。"""
    path = Path(path)
    if not path.is_file():
        return _empty("missing", "证照文件不在服务器上，请重新上传")
    data = path.read_bytes()

    if data[:5] == b"%PDF-":
        return _parse_pdf(data, sha256=sha256)
    return parse_bytes(data, sha256=sha256)


def _parse_pdf(data: bytes, *, sha256: str | None) -> dict:
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception:  # noqa: BLE001
        return _empty(
            "unavailable",
            "PDF 证照需要先把第一页导出为图片（本机未装 PDF 渲染组件）",
        )
    try:
        pdf = pdfium.PdfDocument(data)
        if len(pdf) == 0:
            return _empty("empty", "PDF 没有页面")
        page = pdf[0]
        bitmap = page.render(scale=2.0)
        img = bitmap.to_pil().convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = parse_bytes(buf.getvalue(), sha256=sha256)
        result["pdf_first_page_only"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        return _empty("error", f"PDF 渲染失败（{type(exc).__name__}）", str(exc))
