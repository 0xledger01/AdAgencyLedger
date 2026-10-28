"""P3.5 后端验收脚本（纯黑盒）：文件上传 / 读取安全 / 凭证闸口 / 门户补传 / 开户资质。

对应 docs/CONTRACT-P3.5.md 第 7 节的 15 条断言。
只通过 HTTP 打接口，不 import 任何 backend.app.* 模块、不直连数据库。

跑法：SMOKE_BASE=http://127.0.0.1:8012 .venv/Scripts/python.exe backend/tests/smoke_p3_5.py
返回码 0 = 全过，1 = 有失败。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8012")
CURRENCY = "USD"
Q4 = Decimal("0.0001")
PASS, FAIL = [], []

# 1x1 合法 PNG
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB"
    "AAH8i7wAAAAASUVORK5CYII="
)


# ---------------------------------------------------------------- 基础工具

def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype:
                return resp.status, raw
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode(errors="replace")


def raw_get(path: str, token: str | None = None):
    """拿原始字节，用来验「响应体里不该出现 SQLite 头」。"""
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def upload(token: str | None, data: bytes = PNG_1PX, filename: str = "回单.png",
           biz_type: str = "TOPUP_VOUCHER", content_type: str = "image/png"):
    boundary = "----smoke35boundary1c8f"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="biz_type"\r\n\r\n{biz_type}\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(BASE + "/api/files", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode(errors="replace")


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {short(detail)}")


def short(obj, n: int = 300) -> str:
    if isinstance(obj, bytes):
        s = obj.decode(errors="replace")
    elif isinstance(obj, str):
        s = obj
    else:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    return s[:n]


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Q4)


def note(msg: str) -> None:
    print(f"  ... {msg}")


def login(username: str, password: str, portal: bool = False):
    path = "/api/auth/portal/login" if portal else "/api/auth/login"
    return call("POST", path, body={"username": username, "password": password})


def client_wallet(client_id: int, token: str) -> dict | None:
    code, d = call("GET", f"/api/desk/clients/{client_id}", token=token)
    if code != 200 or not isinstance(d, dict):
        return None
    for w in d.get("wallets", []):
        if w.get("currency") == CURRENCY:
            return {"balance": money(w["balance"]), "frozen": money(w.get("frozen", "0"))}
    return None


def pick_client(token: str) -> int | None:
    code, d = call("GET", "/api/desk/clients?status=ACTIVE&limit=50", token=token)
    if code != 200 or not isinstance(d, dict):
        return None
    for c in d.get("items", []):
        if c.get("settle_currency") == CURRENCY:
            return c.get("id")
    return None


# ---------------------------------------------------------------- 主流程

def main() -> int:
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    print("\n=== 0. 登录 ===")
    code, admin = login("admin", "admin123")
    code2, fin = login("finance01", "123456")
    code3, ops = login("ops01", "123456")
    code4, c1 = login("client1", "123456", portal=True)
    code5, c2 = login("client2", "123456", portal=True)
    check("管理员登录", code == 200 and admin.get("role") == "ADMIN", short(admin))
    check("财务登录", code2 == 200 and fin.get("role") == "FINANCE", short(fin))
    check("运营登录", code3 == 200 and ops.get("role") == "OPS", short(ops))
    check("客户1 门户登录", code4 == 200 and c1.get("role") == "CLIENT", short(c1))
    if not (admin.get("token") and fin.get("token")):
        print("\n登录失败，无法继续")
        print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
        return 1

    T_ADMIN = admin["token"]
    T_FIN = fin["token"]
    T_OPS = ops["token"]
    T_C1 = c1.get("token") if code4 == 200 else None
    T_C2 = c2.get("token") if code5 == 200 else None

    # ------------------------------------------------ 文件上传（#1/#3/#4/#5）
    print("\n=== 文件上传 ===")
    code, up = upload(T_FIN)
    ok_up = code == 200 and isinstance(up, dict)
    check("#1 上传合法 PNG → 200 且返回 key/url/sha256",
          ok_up and up.get("key") and up.get("url") and up.get("sha256")
          and str(up.get("url")).startswith("/api/files/"),
          f"status={code} {short(up)}")
    v_url = up.get("url") if ok_up else None

    code, body = upload(T_FIN, data=b"MZ\x90\x00" + b"\x00" * 200, filename="evil.exe")
    check("#3 上传 exe（MZ 头）→ 400 且文案点明内容/类型",
          code == 400 and ("文件内容" in short(body, 600) or "文件类型" in short(body, 600)),
          f"status={code} {short(body)}")

    code, body = upload(T_FIN, data=b"this is plain text, definitely not an image" * 5,
                        filename="fake.png", content_type="image/png")
    check("#4 声明 image/png 但内容是文本 → 400（魔数嗅探生效）",
          code == 400 and "文件内容" in short(body, 600), f"status={code} {short(body)}")

    code, body = upload(T_FIN, data=b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024),
                        filename="big.png")
    check("#5 上传 >5MB → 400 且含「5MB」",
          code == 400 and "5MB" in short(body, 600), f"status={code} {short(body)}")

    code, body = upload(None)
    check("未登录上传 → 401", code == 401, f"status={code} {short(body)}")

    # ------------------------------------------------ 读取与安全（#2/#6/#7/#8）
    print("\n=== 读取与安全 ===")
    if v_url:
        code, content = raw_get(v_url, T_FIN)
        check("#2 GET 文件 → 200 且字节与原文件一致",
              code == 200 and content == PNG_1PX, f"status={code} len={len(content)}")
    else:
        check("#2 GET 文件 → 200 且字节一致", False, "上传前置失败")

    # 路径穿越：多写法都要挡住，且响应体绝不能出现 SQLite 头
    traversals = [
        "/api/files/../../data/app.db",
        "/api/files/%2e%2e%2f%2e%2e%2fdata%2fapp.db",
        "/api/files/....//....//data/app.db",
        "/api/files/2026/10/../../../data/app.db",
        "/api/files/" + urllib.parse.quote("C:/Windows/win.ini"),
        "/api/files/" + urllib.parse.quote("..\\..\\data\\app.db"),
    ]
    leaked, passed = [], []
    for path in traversals:
        code, content = raw_get(path, T_FIN)
        leaked_flag = b"SQLite format 3" in content[:64] or b"[fonts]" in content[:200]
        if leaked_flag:
            leaked.append(f"{path} -> {code} 泄漏内容")
        if code in (400, 403, 404) and not leaked_flag:
            passed.append(path)
    check("#6 路径穿越全部被拒且不泄漏文件内容",
          not leaked and len(passed) == len(traversals),
          f"通过 {len(passed)}/{len(traversals)}；泄漏={leaked}")

    code, content = raw_get(v_url) if v_url else (0, b"")
    check("#7 未登录读取文件 → 401", code == 401, f"status={code}")

    if T_C1 and T_C2 and v_url:
        code, up_c1 = upload(T_C1, data=PNG_1PX, filename="客户回单.png")
        c1_url = up_c1.get("url") if code == 200 and isinstance(up_c1, dict) else None
        if c1_url:
            code_a, _ = raw_get(c1_url, T_C1)
            code_b, body_b = raw_get(c1_url, T_C2)
            check("#8 客户1 能读自己的文件", code_a == 200, f"status={code_a}")
            check("#8 客户2 读客户1 的文件 → 403", code_b == 403, f"status={code_b} {short(body_b)}")
        else:
            check("#8 客户越权读取 → 403", False, f"客户1 上传失败 status={code} {short(up_c1)}")
    else:
        check("#8 客户越权读取 → 403", False, "客户门户账号不足（需要 client1/client2）")

    # ------------------------------------------------ 凭证闸口（#9/#10/#11/#12）
    print("\n=== 凭证闸口 ===")
    cid = pick_client(T_FIN)
    check("找到可用测试客户", cid is not None, f"cid={cid}")
    if cid is None:
        print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
        return 1

    # #9 无凭证 → 400 且余额不变
    code, tp = call("POST", "/api/desk/topups", token=T_FIN, body={
        "client_id": cid, "amount": "300", "currency": CURRENCY,
        "payment_method": "BANK_TRANSFER", "payer": "无凭证测试",
        "fund_source_type": "SELF", "voucher": None, "remark": "smoke_p3_5 无凭证",
    })
    tid_no = tp.get("id") if isinstance(tp, dict) else None
    w_before = client_wallet(cid, T_FIN)
    code, r = call("POST", f"/api/desk/topups/{tid_no}/confirm", token=T_FIN, body={"remark": "无凭证"})
    w_after = client_wallet(cid, T_FIN)
    check("#9 无凭证 confirm → 400 且文案含「凭证」",
          code == 400 and "凭证" in short(r, 800), f"status={code} {short(r)}")
    check("#9 被拒后余额不变",
          w_before is not None and w_after is not None
          and w_after["balance"] == w_before["balance"],
          f"{w_before} -> {w_after}")

    # #10 无凭证 + 有效原因 → 放行 + 审计
    reason = "线下现金收款，客户当面签字确认"
    code, r = call("POST", f"/api/desk/topups/{tid_no}/confirm", token=T_FIN,
                   body={"remark": "现金", "no_voucher_reason": reason})
    w_after2 = client_wallet(cid, T_FIN)
    check("#10 无凭证但填原因 → 200 且入账",
          code == 200 and isinstance(r, dict) and r.get("status") == "RECEIVED"
          and r.get("no_voucher") is True,
          f"status={code} {short(r)}")
    check("#10 入账金额正确",
          w_before is not None and w_after2 is not None
          and w_after2["balance"] == w_before["balance"] + money("300"),
          f"{w_before} -> {w_after2}")

    code, det = call("GET", f"/api/desk/topups/{tid_no}", token=T_FIN)
    ledger = det.get("ledger", []) if isinstance(det, dict) else []
    hit = [x for x in ledger if reason in (x.get("remark") or "")]
    check("#10 流水备注带上无凭证原因", bool(hit), f"ledger={short(ledger)}")

    # #11 有凭证 → 正常入账
    code, tp2 = call("POST", "/api/desk/topups", token=T_FIN, body={
        "client_id": cid, "amount": "260", "currency": CURRENCY,
        "payment_method": "BANK_TRANSFER", "payer": "有凭证测试",
        "fund_source_type": "SELF", "voucher": v_url, "remark": "smoke_p3_5 有凭证",
    })
    tid_ok = tp2.get("id") if isinstance(tp2, dict) else None
    check("代录带凭证的充值单 → PENDING_CONFIRM",
          code == 200 and isinstance(tp2, dict) and tp2.get("status") == "PENDING_CONFIRM"
          and tp2.get("has_voucher") is True,
          f"status={code} {short(tp2)}")

    w1 = client_wallet(cid, T_FIN)
    code, r = call("POST", f"/api/desk/topups/{tid_ok}/confirm", token=T_FIN, body={"remark": "已核对"})
    w2 = client_wallet(cid, T_FIN)
    check("#11 有凭证 confirm → 200 且余额 +金额",
          code == 200 and w1 is not None and w2 is not None
          and w2["balance"] == w1["balance"] + money("260"),
          f"status={code} {w1} -> {w2} {short(r)}")

    # #12 幂等回归
    code, det1 = call("GET", f"/api/desk/topups/{tid_ok}", token=T_FIN)
    n_before = len(det1.get("ledger", [])) if isinstance(det1, dict) else 0
    tx_first = r.get("tx_id") if isinstance(r, dict) else None
    code, r2 = call("POST", f"/api/desk/topups/{tid_ok}/confirm", token=T_FIN, body={"remark": "重复"})
    w3 = client_wallet(cid, T_FIN)
    code, det2 = call("GET", f"/api/desk/topups/{tid_ok}", token=T_FIN)
    n_after = len(det2.get("ledger", [])) if isinstance(det2, dict) else 0
    check("#12 重复 confirm 仍 200 且 tx_id 相同",
          code == 200 and isinstance(r2, dict) and r2.get("tx_id") == tx_first,
          f"status={code} {short(r2)}")
    check("#12 重复 confirm 余额不变", w2 is not None and w3 is not None
          and w3["balance"] == w2["balance"], f"{w2} -> {w3}")
    check("#12 重复 confirm 流水不新增", n_after == n_before, f"{n_before} -> {n_after}")

    # ------------------------------------------------ 门户补传（#13/#15）
    print("\n=== 门户补传凭证 ===")
    if T_C1:
        code, pt = call("POST", "/api/portal/topups", token=T_C1, body={
            "amount": "150", "currency": CURRENCY, "payment_method": "BANK_TRANSFER",
            "payer": "门户客户", "voucher": None, "remark": "smoke_p3_5 门户无凭证",
        })
        biz = pt.get("biz_no") if isinstance(pt, dict) else None
        check("#13 门户提交无凭证 → PENDING_PAY",
              code == 200 and isinstance(pt, dict) and pt.get("status") == "PENDING_PAY",
              f"status={code} {short(pt)}")

        code, up_c1 = upload(T_C1, data=PNG_1PX, filename="补传回单.png")
        c1_voucher = up_c1.get("url") if code == 200 and isinstance(up_c1, dict) else None
        if biz and c1_voucher:
            code, r = call("POST", f"/api/portal/topups/{biz}/voucher", token=T_C1,
                           body={"voucher": c1_voucher})
            check("#13 补传凭证 → PENDING_CONFIRM",
                  code == 200 and isinstance(r, dict) and r.get("status") == "PENDING_CONFIRM",
                  f"status={code} {short(r)}")
            code, r = call("POST", f"/api/portal/topups/{biz}/voucher", token=T_C1,
                           body={"voucher": c1_voucher})
            check("#13 再补一次 → 400", code == 400, f"status={code} {short(r)}")
        else:
            check("#13 补传凭证 → PENDING_CONFIRM", False, "客户上传或单号缺失")

        # #15 凭证只能是上传接口的地址
        code, r = call("POST", "/api/portal/topups", token=T_C1, body={
            "amount": "99", "currency": CURRENCY, "payment_method": "BANK_TRANSFER",
            "payer": "注入测试", "voucher": "../../etc/passwd", "remark": "任意路径注入",
        })
        check("#15 门户提交非 /api/files/ 凭证 → 400",
              code == 400 and "上传" in short(r, 600), f"status={code} {short(r)}")
    else:
        check("#13 门户提交无凭证 → PENDING_PAY", False, "client1 登录失败")
        check("#15 门户提交非 /api/files/ 凭证 → 400", False, "client1 登录失败")

    # ------------------------------------------------ 开户资质（#14）
    print("\n=== 开户资质 ===")
    code, lst = call("GET", "/api/desk/open-requests?limit=1", token=T_OPS)
    orid = None
    if code == 200 and isinstance(lst, dict) and lst.get("items"):
        orid = lst["items"][0].get("id")
    if orid is None:
        code, created = call("POST", "/api/desk/open-requests", token=T_OPS, body={
            "client_id": cid, "platform": "TIKTOK", "bm_id": "BM-SMOKE-35",
            "timezone": "Asia/Shanghai", "currency": CURRENCY, "daily_limit": "1000",
        })
        orid = created.get("id") if isinstance(created, dict) else None

    if orid:
        code, det = call("GET", f"/api/desk/open-requests/{orid}", token=T_OPS)
        ok = code == 200 and isinstance(det, dict)
        client_obj = det.get("client") if ok else None
        check("#14 开户详情返回 client 资质",
              ok and isinstance(client_obj, dict)
              and client_obj.get("company_name")
              and "credit_code" in client_obj and "license_url" in client_obj,
              f"status={code} {short(det)}")
        check("#14 开户详情返回 existing_accounts",
              ok and isinstance(det.get("existing_accounts"), list),
              f"status={code} {short(det)}")
        check("#14 item 与列表结构一致",
              ok and det.get("item", {}).get("biz_no") == (det.get("item") or {}).get("biz_no"),
              short(det)[:200])
    else:
        check("#14 开户详情返回 client 资质", False, "拿不到开户单")
        check("#14 开户详情返回 existing_accounts", False, "拿不到开户单")

    # ------------------------------------------------ 收尾
    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    else:
        print("全部通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
