"""P3 后端验收脚本（纯黑盒）：D3 充值 / D2 开户 / D5 加款 / D6 消耗 / 权限矩阵。

对应 docs/CONTRACT-P3.md 第 8 节的 15 条断言。
只通过 HTTP 打接口，不 import 任何 backend.app.* 模块、不直连数据库。

跑法：.venv/Scripts/python.exe backend/tests/smoke_p3.py
      SMOKE_BASE=http://127.0.0.1:8012（默认）
返回码 0 = 全过，1 = 有失败。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8012")
CURRENCY = "USD"
PASS, FAIL = [], []
Q4 = Decimal("0.0001")

# seed 账号（只从 backend/app/seed.py 取账号密码，不读其他后端源码）
ADMIN = ("admin", "admin123")           # display_name 张三
FINANCE = ("finance01", "123456")       # display_name 赵六
OPS = ("ops01", "123456")               # display_name 钱七
CLIENT = ("client1", "123456")          # 门户账号，client_id=1


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


def check(name: str, cond: bool, detail="") -> None:
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {short(detail)}")


def login(username: str, password: str, portal: bool = False):
    path = "/api/auth/portal/login" if portal else "/api/auth/login"
    code, data = call("POST", path, body={"username": username, "password": password})
    return code, data


# ---------------------------------------------------------------- 小工具

def short(obj, n: int = 300) -> str:
    """失败上下文：响应体截断到 n 字符。"""
    if isinstance(obj, bytes):
        s = obj.decode(errors="replace")
    elif isinstance(obj, str):
        s = obj
    else:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    return s[:n]


def money(v) -> Decimal:
    """金额一律 Decimal 量化，禁止 float 相等比较。"""
    return Decimal(str(v)).quantize(Q4)


def note(msg: str) -> None:
    print(f"  ... {msg}")


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB"
    "AAH8i7wAAAAASUVORK5CYII="
)


def upload_file(token: str, data: bytes = PNG_1PX, filename: str = "回单.png",
                biz_type: str = "TOPUP_VOUCHER", content_type: str = "image/png"):
    """标准库手搓 multipart，不引第三方依赖。返回 (status, json_or_text)。"""
    boundary = "----smokeboundary7d1f9a"
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
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode(errors="replace")


def voucher_url(token: str) -> str | None:
    """上传一张合法凭证，返回可直接填进充值单的 url。"""
    code, d = upload_file(token)
    return d.get("url") if code == 200 and isinstance(d, dict) else None


def client_wallet(client_id: int, token: str) -> dict | None:
    """从 /api/desk/clients/{id} 的 wallets 里取指定币种：balance/frozen/available。"""
    code, d = call("GET", f"/api/desk/clients/{client_id}", token=token)
    if code != 200 or not isinstance(d, dict):
        return None
    for w in d.get("wallets", []):
        if w.get("currency") == CURRENCY:
            return {
                "balance": money(w["balance"]),
                "frozen": money(w.get("frozen", "0")),
                "available": money(w.get("available", "0")),
            }
    return None


def client_accounts(client_id: int, token: str) -> list:
    code, d = call("GET", f"/api/desk/clients/{client_id}", token=token)
    if code != 200 or not isinstance(d, dict):
        return []
    return d.get("accounts", [])


def account_balance(client_id: int, account_id: int, token: str) -> Decimal | None:
    for a in client_accounts(client_id, token):
        if a.get("id") == account_id:
            return money(a["balance"])
    return None


def pick_client(token: str) -> int | None:
    """挑一个已开通、USD 结算、钱包有余额的客户；不假设任何固定 id。"""
    code, d = call("GET", "/api/desk/clients?status=ACTIVE&limit=50", token=token)
    if code != 200:
        note(f"客户列表不可读：{short(d)}")
        return None
    for it in d.get("items", []):
        if it.get("settle_currency") != CURRENCY:
            continue
        if money(it.get("wallet_balance") or "0") <= 0:
            continue
        w = client_wallet(it["id"], token)
        if w and w["available"] > 0:
            return it["id"]
    note("没有找到可用（USD、有余额）的已开通客户")
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
    code, admin = login(*ADMIN)
    check("管理员登录", code == 200 and admin.get("role") == "ADMIN", short(admin))
    code, fin = login(*FINANCE)
    check("财务登录", code == 200 and fin.get("role") == "FINANCE", short(fin))
    code, ops = login(*OPS)
    check("运营登录", code == 200 and ops.get("role") == "OPS", short(ops))
    code, cli = login(*CLIENT, portal=True)
    check("客户门户登录", code == 200 and cli.get("role") == "CLIENT", short(cli))
    if not (isinstance(admin, dict) and admin.get("token")
            and isinstance(cli, dict) and cli.get("token")):
        print("登录失败，后续无法执行")
        print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
        return 1
    T_ADMIN, T_FIN, T_OPS, T_CLI = admin["token"], fin["token"], ops["token"], cli["token"]

    cid = pick_client(T_FIN)
    check("找到可用测试客户", cid is not None, f"cid={cid}")
    if cid is None:
        print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
        return 1
    note(f"测试客户 client_id={cid}")

    # ------------------------------------------------------------ 权限（14/15）
    print("\n=== 权限 ===")
    code, _ = call("GET", "/api/desk/topups", token=T_CLI)
    check("#14 客户角色访问 /api/desk/topups → 403", code == 403, f"status={code}")

    code, perm_topup = call("POST", "/api/desk/topups", token=T_FIN, body={
        "client_id": cid, "amount": "10", "currency": CURRENCY,
        "payment_method": "BANK_TRANSFER", "payer": "权限测试",
        "fund_source_type": "SELF", "remark": "smoke_p3 权限用例",
    })
    perm_id = perm_topup.get("id") if isinstance(perm_topup, dict) else None
    check("财务代录权限测试单", code == 200 and perm_id is not None, f"status={code} {short(perm_topup)}")
    if perm_id:
        w_before = client_wallet(cid, T_FIN)
        code, r = call("POST", f"/api/desk/topups/{perm_id}/confirm", token=T_OPS, body={"remark": "越权"})
        w_after = client_wallet(cid, T_FIN)
        check("#15 运营调 TOPUP_CONFIRM（确认到账）→ 403", code == 403, f"status={code} {short(r)}")
        check("#15 越权确认未动余额",
              w_before is not None and w_after is not None and w_before["balance"] == w_after["balance"],
              f"{w_before} -> {w_after}")

    # ------------------------------------------------------------ D3 充值（1/2/3/4）
    print("\n=== D3 充值 ===")
    w0 = client_wallet(cid, T_FIN)
    check("读取客户钱包成功", w0 is not None, "")
    bal0 = w0["balance"] if w0 else Decimal("0")

    amount = Decimal("5000")
    v_url = voucher_url(T_FIN)
    check("上传充值凭证（真实文件）", bool(v_url), f"voucher={v_url}")
    code, tp = call("POST", "/api/desk/topups", token=T_FIN, body={
        "client_id": cid, "amount": str(amount), "currency": CURRENCY,
        "payment_method": "BANK_TRANSFER", "payer": "张三",
        "fund_source_type": "SELF", "voucher": v_url, "remark": "smoke_p3 到账用例",
    })
    check("代录充值单 → PENDING_CONFIRM",
          code == 200 and isinstance(tp, dict) and tp.get("status") == "PENDING_CONFIRM"
          and tp.get("status_label") == "待确认" and tp.get("biz_no"),
          f"status={code} {short(tp)}")
    tid = tp.get("id") if isinstance(tp, dict) else None

    if tid:
        w_before = client_wallet(cid, T_FIN)
        code, r = call("POST", f"/api/desk/topups/{tid}/confirm", token=T_FIN, body={"remark": "已核对银行流水"})
        w_after = client_wallet(cid, T_FIN)
        tx_id = r.get("tx_id") if isinstance(r, dict) else None
        check("确认到账 → RECEIVED",
              code == 200 and isinstance(r, dict) and r.get("status") == "RECEIVED"
              and r.get("status_label") == "已到账" and tx_id,
              f"status={code} {short(r)}")
        check("#1 确认后客户钱包余额 = 原余额 + 充值额",
              w_before is not None and w_after is not None
              and w_after["balance"] == w_before["balance"] + amount,
              f"{w_before} + {amount} -> {w_after}")
        check("#1 响应 balance 与钱包一致",
              isinstance(r, dict) and money(r.get("balance", "0")) == (w_after or {}).get("balance"),
              f"resp={short(r)} wallet={w_after}")

        code, det = call("GET", f"/api/desk/topups/{tid}", token=T_FIN)
        ledger = det.get("ledger", []) if isinstance(det, dict) else []
        hit = [x for x in ledger
               if x.get("direction") == "IN" and x.get("biz_type") == "TOPUP"
               and money(x.get("amount", "0")) == amount]
        check("#1 能查到 TOPUP 进账流水",
              code == 200 and bool(hit) and money(hit[0].get("balance_after", "0")) == (w_after or {}).get("balance"),
              f"status={code} ledger={short(ledger)}")
        check("#1 流水字段白名单齐全",
              bool(hit) and all(k in hit[0] for k in
                                ("id", "seq", "direction", "amount", "currency", "balance_after",
                                 "biz_type", "operator", "remark", "occurred_at")),
              short(hit[0] if hit else {}))

        n_before = len(ledger)
        w1 = client_wallet(cid, T_FIN)
        code, r2 = call("POST", f"/api/desk/topups/{tid}/confirm", token=T_FIN, body={"remark": "重复确认"})
        w2 = client_wallet(cid, T_FIN)
        code, det2 = call("GET", f"/api/desk/topups/{tid}", token=T_FIN)
        ledger2 = det2.get("ledger", []) if isinstance(det2, dict) else []
        check("#2 重复确认返回 200 且 tx_id 相同",
              code == 200 and isinstance(r2, dict) and r2.get("tx_id") == tx_id,
              f"status={code} {short(r2)} | 首次 tx={tx_id}")
        check("#2 重复确认余额不变",
              w1 is not None and w2 is not None and w1["balance"] == w2["balance"],
              f"{w1} -> {w2}")
        check("#2 重复确认流水不新增", len(ledger2) == n_before,
              f"{n_before} -> {len(ledger2)}")

    # 退回
    code, tp2 = call("POST", "/api/desk/topups", token=T_FIN, body={
        "client_id": cid, "amount": "1000", "currency": CURRENCY,
        "payment_method": "BANK_TRANSFER", "payer": "李四", "fund_source_type": "SELF",
        "voucher": v_url,
    })
    tid2 = tp2.get("id") if isinstance(tp2, dict) else None
    if tid2:
        wb = client_wallet(cid, T_FIN)
        code, r = call("POST", f"/api/desk/topups/{tid2}/reject", token=T_FIN,
                       body={"reason": "凭证金额不符"})
        wa = client_wallet(cid, T_FIN)
        check("#3 退回后状态 RETURNED",
              code == 200 and isinstance(r, dict) and r.get("status") == "RETURNED"
              and r.get("status_label") == "已退回",
              f"status={code} {short(r)}")
        check("#3 退回余额不变",
              wb is not None and wa is not None and wa["balance"] == wb["balance"],
              f"{wb} -> {wa}")
        code, r = call("POST", f"/api/desk/topups/{tid2}/reject", token=T_FIN,
                       body={"reason": ""})
        check("#3 非待确认/空原因退回被拒", code == 400, f"status={code} {short(r)}")
    else:
        note(f"退回用例前置失败：{short(tp2)}")

    # 冲正
    code, tp3 = call("POST", "/api/desk/topups", token=T_FIN, body={
        "client_id": cid, "amount": "800", "currency": CURRENCY,
        "payment_method": "BANK_TRANSFER", "payer": "王五", "fund_source_type": "SELF",
        "voucher": v_url,
    })
    tid3 = tp3.get("id") if isinstance(tp3, dict) else None
    if tid3:
        code, r = call("POST", f"/api/desk/topups/{tid3}/confirm", token=T_FIN, body={})
        check("冲正前置：确认到账", code == 200, f"status={code} {short(r)}")
        w_c = client_wallet(cid, T_FIN)

        code, r = call("POST", f"/api/desk/topups/{tid3}/reverse", token=T_FIN,
                       body={"reason": "银行退票", "second_approver": ""})
        check("#4 second_approver 为空 → 400", code == 400, f"status={code} {short(r)}")
        code, r = call("POST", f"/api/desk/topups/{tid3}/reverse", token=T_FIN,
                       body={"reason": "银行退票", "second_approver": "赵六"})
        check("#4 second_approver == 操作人 → 400", code == 400, f"status={code} {short(r)}")
        w_mid = client_wallet(cid, T_FIN)
        check("#4 被拒冲正未动余额",
              w_c is not None and w_mid is not None and w_c["balance"] == w_mid["balance"],
              f"{w_c} -> {w_mid}")

        code, r = call("POST", f"/api/desk/topups/{tid3}/reverse", token=T_FIN,
                       body={"reason": "银行退票", "second_approver": "张三"})
        w_d = client_wallet(cid, T_FIN)
        check("#4 正常冲正 → REVERSED",
              code == 200 and isinstance(r, dict) and r.get("status") == "REVERSED"
              and r.get("status_label") == "已冲正" and r.get("reverse_tx_id"),
              f"status={code} {short(r)}")
        check("#4 冲正后余额回退 800",
              w_c is not None and w_d is not None and w_d["balance"] == w_c["balance"] - Decimal("800"),
              f"{w_c} -> {w_d}")
        code, det = call("GET", f"/api/desk/topups/{tid3}", token=T_FIN)
        ledger = det.get("ledger", []) if isinstance(det, dict) else []
        out = [x for x in ledger if x.get("direction") == "OUT" and money(x.get("amount", "0")) == Decimal("800")]
        check("#4 原进账流水仍在（追加红字，不删原单）",
              code == 200 and len(ledger) >= 2
              and any(x.get("direction") == "IN" for x in ledger) and bool(out),
              f"ledger={short(ledger)}")

    # ------------------------------------------------------------ D2 开户（5/6/7）
    print("\n=== D2 开户 ===")
    acc_suffix = str(int(time.time()))[-8:]
    new_account_id = f"act_p3_{acc_suffix}"

    # 开户提交/审批按 PRD 3.2 矩阵属运营，财务无 OPEN_REQUEST_SUBMIT
    code, ors = call("POST", "/api/desk/open-requests", token=T_OPS, body={
        "client_id": cid, "platform": "TIKTOK", "bm_id": f"BM-P3-{acc_suffix}",
        "timezone": "Asia/Shanghai", "currency": CURRENCY, "daily_limit": "1000",
    })
    orid = ors.get("id") if isinstance(ors, dict) else None
    check("代客户提交开户 → PENDING_APPROVAL",
          code == 200 and isinstance(ors, dict) and ors.get("status") == "PENDING_APPROVAL"
          and ors.get("status_label") == "待审批",
          f"status={code} {short(ors)}")

    if orid:
        code, r = call("POST", f"/api/desk/open-requests/{orid}/submit", token=T_OPS, body={})
        check("#5 未审批直接提交 → 400 且含「未审批通过」",
              code == 400 and "未审批通过" in short(r, 1000),
              f"status={code} {short(r)}")

        code, r = call("POST", f"/api/desk/open-requests/{orid}/approve", token=T_ADMIN,
                       body={"checks": {"subject": True, "industry": True, "bm": True,
                                        "duplicate": False, "channel": True}})
        check("#6 核对清单含 false → 400", code == 400, f"status={code} {short(r)}")
        code, r = call("POST", f"/api/desk/open-requests/{orid}/approve", token=T_ADMIN,
                       body={"checks": {"subject": True, "industry": True, "channel": True}})
        detail = short(r, 500)
        check("#6 核对清单缺项 → 400", code == 400, f"status={code} {detail}")
        check("#6 缺项文案指名缺的 key", code == 400 and ("bm" in detail or "duplicate" in detail),
              detail)

        code, r = call("POST", f"/api/desk/open-requests/{orid}/approve", token=T_ADMIN, body={
            "checks": {"subject": True, "industry": True, "bm": True,
                       "duplicate": True, "channel": True}, "remark": "核对通过"})
        check("五项全勾 → PENDING_SUBMIT",
              code == 200 and isinstance(r, dict) and r.get("status") == "PENDING_SUBMIT"
              and r.get("status_label") == "待提交",
              f"status={code} {short(r)}")

        code, r = call("POST", f"/api/desk/open-requests/{orid}/submit", token=T_OPS, body={})
        check("提交到一代 → AGENT_REVIEWING + 回执",
              code == 200 and isinstance(r, dict) and r.get("status") == "AGENT_REVIEWING"
              and r.get("agent_receipt"),
              f"status={code} {short(r)}")

        code, r = call("POST", f"/api/desk/open-requests/{orid}/receipt", token=T_ADMIN,
                       body={"result": "OPENED", "account_id": new_account_id})
        ad_id = r.get("ad_account_id") if isinstance(r, dict) else None
        check("#7 Mock 回执 OPENED → 已开户",
              code == 200 and isinstance(r, dict) and r.get("status") == "OPENED"
              and r.get("status_label") == "已开户" and ad_id,
              f"status={code} {short(r)}")
        if ad_id:
            accs = client_accounts(cid, T_FIN)
            created = [a for a in accs if a.get("id") == ad_id]
            check("#7 生成 AdAccount 且归属该客户/平台/币种",
                  bool(created) and created[0].get("account_id") == new_account_id
                  and created[0].get("platform") == "TIKTOK" and created[0].get("currency") == CURRENCY,
                  short(created))
            check("#7 AdAccount 状态 NORMAL",
                  bool(created) and created[0].get("status") == "NORMAL", short(created))
        else:
            ad_id = None

        code, r = call("POST", f"/api/desk/open-requests/{orid}/receipt", token=T_ADMIN,
                       body={"result": "OPENED", "account_id": new_account_id})
        check("#7 重复回执被拒（非 AGENT_REVIEWING）", code == 400, f"status={code} {short(r)}")
    else:
        ad_id = None
        note(f"开户用例前置失败：{short(ors)}")

    # ------------------------------------------------------------ D5 加款（8/9/10/11）
    print("\n=== D5 加款 ===")
    rc_amount = Decimal("100")
    if not ad_id:
        note("没有新开广告账户，跳过 D5 主链路（#8/#9/#10/#11 记为失败）")
        for nm in ("#8 加款发起后冻结增加、余额不变",
                   "#9 UNKNOWN → TO_VERIFY 且余额/冻结不变",
                   "#10 核查 FAILED → 冻结归零、余额不变",
                   "#11 SUCCESS → 钱包−X / 广告账户+X / RECHARGE 出账"):
            check(nm, False, "D2 未产出 ad_account_id，无法构造前置数据")
    else:
        code, cw = call("GET", "/api/desk/recharges/circuit", token=T_FIN)
        check("熔断开关可读", code == 200 and isinstance(cw, dict) and "items" in cw,
              f"status={code} {short(cw)}")
        # 熔断开关属 RECHARGE_RESOLVE（运营/管理员），财务无此权限
        code, r = call("POST", "/api/desk/recharges/circuit", token=T_OPS,
                       body={"platform": "TIKTOK", "enabled": False})
        check("熔断开关可写（关闭 TikTok）", code == 200, f"status={code} {short(r)}")

        w_b = client_wallet(cid, T_FIN)
        acc_b = account_balance(cid, ad_id, T_FIN)
        req_no = f"smoke-{acc_suffix}-main"
        code, rc = call("POST", "/api/desk/recharges", token=T_FIN,
                        body={"ad_account_id": ad_id, "amount": str(rc_amount), "remark": "客户要求",
                              "request_no": req_no})
        rid = rc.get("id") if isinstance(rc, dict) else None
        w_a = client_wallet(cid, T_FIN)
        check("发起加款 → PENDING_EXEC 且 initiated_by 有值",
              code == 200 and isinstance(rc, dict) and rc.get("status") == "PENDING_EXEC"
              and rc.get("biz_no") and rc.get("initiated_by"),
              f"status={code} {short(rc)}")
        check("#8 加款发起后 frozen +X",
              w_b is not None and w_a is not None and w_a["frozen"] == w_b["frozen"] + rc_amount,
              f"{w_b} -> {w_a}")
        check("#8 加款发起后 balance 不变",
              w_b is not None and w_a is not None and w_a["balance"] == w_b["balance"],
              f"{w_b} -> {w_a}")

        # #16 铁律 3：资金接口幂等——同一 request_no 再提交一次，不得重复冻结
        code_d, rc_d = call("POST", "/api/desk/recharges", token=T_FIN,
                            body={"ad_account_id": ad_id, "amount": str(rc_amount),
                                  "remark": "客户要求", "request_no": req_no})
        w_d = client_wallet(cid, T_FIN)
        check("#16 重复 request_no → 返回原单且不重复冻结",
              code_d == 200 and isinstance(rc_d, dict) and rc_d.get("deduped") is True
              and rc_d.get("id") == rid and w_a is not None and w_d is not None
              and w_d["frozen"] == w_a["frozen"],
              f"status={code_d} {short(rc_d)} frozen {w_a} -> {w_d}")

        code_m, r_m = call("POST", "/api/desk/recharges", token=T_FIN,
                           body={"ad_account_id": ad_id, "amount": "10", "remark": "缺幂等键"})
        check("#16 缺 request_no → 422", code_m == 422, f"status={code_m} {short(r_m)}")

        if rid:
            code, r = call("POST", f"/api/desk/recharges/{rid}/exec", token=T_OPS,
                           body={"outcome": "UNKNOWN"})
            w_u = client_wallet(cid, T_FIN)
            check("#9 UNKNOWN → TO_VERIFY",
                  code == 200 and isinstance(r, dict) and r.get("status") == "TO_VERIFY"
                  and r.get("status_label") == "待核查",
                  f"status={code} {short(r)}")
            check("#9 UNKNOWN 后 balance 不变",
                  w_b is not None and w_u is not None and w_u["balance"] == w_b["balance"],
                  f"{w_b} -> {w_u}")
            check("#9 UNKNOWN 后 frozen 不释放",
                  w_a is not None and w_u is not None and w_u["frozen"] == w_a["frozen"],
                  f"{w_a} -> {w_u}")
            code, det = call("GET", f"/api/desk/recharges?status=TO_VERIFY&limit=200", token=T_FIN)
            items = det.get("items", []) if isinstance(det, dict) else []
            hit = [x for x in items if x.get("id") == rid]
            check("#9 TO_VERIFY 可被列表检索到（核查链完好）",
                  code == 200 and bool(hit) and hit[0].get("retry_count") is not None,
                  f"status={code} {short(hit)}")
            check("#9 unknown 回执标注 mock", bool(hit) and "mock" in short(hit[0].get("receipt"), 800).lower(),
                  short(hit[0].get("receipt") if hit else {}))

            code, r = call("POST", f"/api/desk/recharges/{rid}/resolve", token=T_ADMIN,
                           body={"result": "FAILED", "reason": "媒体后台未到账"})
            w_f = client_wallet(cid, T_FIN)
            check("#10 核查 FAILED → REJECTED",
                  code == 200 and isinstance(r, dict) and r.get("status") == "REJECTED",
                  f"status={code} {short(r)}")
            check("#10 核查 FAILED 后 frozen 归零",
                  w_f is not None and w_f["frozen"] == (w_b["frozen"] if w_b else Decimal("0")),
                  f"{w_u} -> {w_f}")
            check("#10 核查 FAILED 后 balance 不变",
                  w_b is not None and w_f is not None and w_f["balance"] == w_b["balance"],
                  f"{w_b} -> {w_f}")

            code, r = call("POST", f"/api/desk/recharges/{rid}/retry", token=T_ADMIN, body={})
            check("REJECTED 可重试 → PENDING_EXEC 且 retry_count+1",
                  code == 200 and isinstance(r, dict) and r.get("status") == "PENDING_EXEC"
                  and r.get("retry_count") == 1,
                  f"status={code} {short(r)}")

            w_s = client_wallet(cid, T_FIN)
            acc_s = account_balance(cid, ad_id, T_FIN)
            code, r = call("POST", f"/api/desk/recharges/{rid}/exec", token=T_OPS,
                           body={"outcome": "SUCCESS"})
            w_t = client_wallet(cid, T_FIN)
            acc_t = account_balance(cid, ad_id, T_FIN)
            check("加款 SUCCESS → 状态成功 + serial_no",
                  code == 200 and isinstance(r, dict) and r.get("status") == "SUCCESS"
                  and r.get("serial_no"),
                  f"status={code} {short(r)}")
            check("#11 客户钱包 −X",
                  w_s is not None and w_t is not None
                  and w_t["balance"] == w_s["balance"] - rc_amount,
                  f"{w_s} -> {w_t}")
            check("#11 冻结释放",
                  w_t is not None and w_t["frozen"] == (w_b["frozen"] if w_b else Decimal("0")),
                  f"{w_s} -> {w_t}")
            check("#11 广告账户 +X",
                  acc_s is not None and acc_t is not None and acc_t == acc_s + rc_amount,
                  f"{acc_s} -> {acc_t}")
            # 门户流水白名单不含 biz_type（P2 设计），改用后台主账本按业务类型过滤
            code, tx = call(
                "GET", f"/api/desk/client-ledger?client_id={cid}&biz_type=RECHARGE&limit=50",
                token=T_FIN,
            )
            rows = tx.get("items", []) if isinstance(tx, dict) else []
            hit = [x for x in rows if x.get("biz_type") == "RECHARGE" and x.get("direction") == "OUT"
                   and money(x.get("amount", "0")) == rc_amount]
            check("#11 能查到 RECHARGE 出账流水", bool(hit), f"status={code} {short(rows[:3])}")

            # 熔断生效
            code, r = call("POST", "/api/desk/recharges/circuit", token=T_ADMIN,
                           body={"platform": "TIKTOK", "enabled": True})
            code2, rc2 = call("POST", "/api/desk/recharges", token=T_FIN,
                              body={"ad_account_id": ad_id, "amount": "10", "remark": "熔断用例",
                                    "request_no": f"smoke-{acc_suffix}-circuit"})
            rid2 = rc2.get("id") if isinstance(rc2, dict) else None
            if rid2:
                code3, r3 = call("POST", f"/api/desk/recharges/{rid2}/exec", token=T_OPS,
                                 body={"outcome": "SUCCESS"})
                check("熔断开启后执行 → 400 且含「已熔断」",
                      code3 == 400 and "熔断" in short(r3, 500), f"status={code3} {short(r3)}")
            else:
                note(f"熔断用例前置失败：{short(rc2)}")
            call("POST", "/api/desk/recharges/circuit", token=T_ADMIN,
                 body={"platform": "TIKTOK", "enabled": False})

            # #16 撤销：待执行单撤掉后冻结必须释放，余额不变
            if rid2:
                w_c1 = client_wallet(cid, T_FIN)
                code4, r4 = call("POST", f"/api/desk/recharges/{rid2}/cancel", token=T_OPS,
                                 body={"reason": "smoke：熔断用例撤单"})
                w_c2 = client_wallet(cid, T_FIN)
                check("#16 撤销待执行单 → CANCELLED / 已撤销",
                      code4 == 200 and isinstance(r4, dict) and r4.get("status") == "CANCELLED"
                      and r4.get("status_label") == "已撤销",
                      f"status={code4} {short(r4)}")
                check("#16 撤销后冻结回落 10、余额不变",
                      w_c1 is not None and w_c2 is not None
                      and w_c2["frozen"] == w_c1["frozen"] - money("10")
                      and w_c2["balance"] == w_c1["balance"],
                      f"{w_c1} -> {w_c2}")
                code5, r5 = call("POST", f"/api/desk/recharges/{rid2}/cancel", token=T_OPS,
                                 body={"reason": "重复撤销"})
                check("#16 已撤销再撤 → 400", code5 == 400, f"status={code5} {short(r5)}")
        else:
            note(f"加款用例前置失败：{short(rc)}")

    # ------------------------------------------------------------ D6 消耗（12/13）
    print("\n=== D6 消耗 ===")
    if not ad_id:
        for nm in ("#12 同日重复导入 → 全部 skipped 且含「已存在」",
                   "#13 消耗导入不动客户钱包余额"):
            check(nm, False, "D2 未产出 ad_account_id，无法构造前置数据")
    else:
        d_day = (date.today() - timedelta(days=1)).isoformat()
        w_before = client_wallet(cid, T_FIN)
        acc_before = account_balance(cid, ad_id, T_FIN)

        rows = [
            {"date": d_day, "account_id": new_account_id, "spend": "50", "currency": CURRENCY,
             "exchange_rate": "1"},
            {"date": d_day, "account_id": f"NO_SUCH_{acc_suffix}", "spend": "10",
             "currency": CURRENCY, "exchange_rate": "1"},
        ]
        code, rep = call("POST", "/api/desk/spend/import", token=T_FIN,
                         body={"dry_run": False, "rows": rows})
        check("消耗导入返回报告结构",
              code == 200 and isinstance(rep, dict) and rep.get("total") == 2
              and rep.get("success") == 1 and isinstance(rep.get("failed"), list),
              f"status={code} {short(rep)}")
        check("账户不存在的行进 failed",
              isinstance(rep, dict) and any("账户不存在" in short(x.get("reason", ""), 100)
                                            for x in rep.get("failed", [])),
              short(rep))

        code, rep2 = call("POST", "/api/desk/spend/import", token=T_FIN,
                          body={"dry_run": False, "rows": rows[:1]})
        skipped = rep2.get("skipped", []) if isinstance(rep2, dict) else []
        check("#12 同日重复导入 → 全部 skipped",
              code == 200 and isinstance(rep2, dict) and rep2.get("success") == 0
              and len(skipped) == 1,
              f"status={code} {short(rep2)}")
        check("#12 skipped reason 含「已存在」",
              bool(skipped) and "已存在" in short(skipped[0].get("reason", ""), 100),
              short(skipped))

        code, lst = call("GET", f"/api/desk/spend?date_from={d_day}&date_to={d_day}"
                                f"&account_id={new_account_id}&limit=50", token=T_FIN)
        items = lst.get("items", []) if isinstance(lst, dict) else []
        check("消耗列表可查（source=IMPORT）",
              code == 200 and len(items) == 1 and items[0].get("source") == "IMPORT",
              f"status={code} {short(items)}")

        w_after = client_wallet(cid, T_FIN)
        acc_after = account_balance(cid, ad_id, T_FIN)
        check("#13 消耗导入不动客户钱包余额",
              w_before is not None and w_after is not None
              and w_after["balance"] == w_before["balance"],
              f"{w_before} -> {w_after}")
        check("消耗只扣广告账户（ad_account -50）",
              acc_before is not None and acc_after is not None
              and acc_after == acc_before - Decimal("50"),
              f"{acc_before} -> {acc_after}")

        code, rep3 = call("POST", "/api/desk/spend/import", token=T_FIN,
                          body={"dry_run": True,
                                "rows": [{"date": d_day, "account_id": new_account_id,
                                          "spend": "70", "currency": CURRENCY,
                                          "exchange_rate": "1"}]})
        w_dry = client_wallet(cid, T_FIN)
        check("dry_run 只预校验不落库",
              code == 200 and isinstance(rep3, dict) and w_dry is not None
              and w_dry["balance"] == (w_before["balance"] if w_before else Decimal("0")),
              f"status={code} {short(rep3)}")

    # ------------------------------------------------------------ 汇总
    print("\n" + "=" * 56)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


# ---------------------------------------------------------------- 契约歧义清单
#
# 写脚本过程中发现契约不明确、实现方可能各写一套的地方（可直接拿去做对齐）：
#
# A1. 冲正的状态前置（3.6）：契约只说「只新增红字流水」，没写哪些状态可冲正。
#     建议判定：仅 RECEIVED 可冲正；PENDING_CONFIRM / RETURNED / 已冲正 → 400 STATE_ERROR。
#
# A2. second_approver 的身份口径（3.6/3.7）：契约说「≠ 当前登录用户 display_name」，
#     但前端只能拿到 token 里的用户名/显示名，字段究竟比对 username 还是 display_name 没写死。
#     建议判定：比对 user.display_name；脚本用 finance01（赵六）操作、传「张三」，
#     这两个值同时不等于 username 也不等于 display_name，避免误伤。
#
# A3. 重复确认的边界（3.4）：契约保证幂等「200 + tx_id 相同」，但没说已 RETURNED / REVERSED
#     的单据再次 confirm 该返回什么。建议判定：非 RECEIVED 但仍可入账之外的状态一律
#     400 STATE_ERROR；RECEIVED 重复调用才走 200 幂等。
#
# A4. 幂等键口径不一致（5.2 vs 5.3）：5.2 写 `idempotency_key = recharge:{biz_no}`，
#     5.3 出账写 `recharge:{order.id}`，一个用业务号一个用主键，且 5.5 重试要求「复用同一个 key」。
#     建议判定：冻结与出账各自独立 key（freeze:recharge:{id} / recharge:{id}），全链路统一用 id，
#     并在契约里写明，否则「重试复用同一个 key」无法验。
#
# A5. 5.3 响应金额语义（5.3）：`{"balance": "…", "account_balance": "…"}` 没说是客户钱包
#     balance 还是 available、account_balance 是媒体余额还是加款后差额。
#     建议判定：balance = 客户钱包可用余额（available），account_balance = AdAccount.balance，
#     并补 available/frozen 字段方便前端。
#
# A6. 4.5 submit 错误文案位置：契约要求「文案必须含『未审批通过』」，没说在 detail/message 还是
#     errors[] 里。建议判定：统一 `{"detail": "..."}`（与 P2 错误结构一致），脚本按整包字符串匹配。
#
# A7. 4.7 回执重复调用与 AGENT_REJECTED 的字段落点：契约只写「仅 AGENT_REVIEWING 可回执」，
#     没说重复回执的码，也没说 AGENT_REJECTED 是否要写 reject_reason 字段。
#     建议判定：重复回执 400 STATE_ERROR；AGENT_REJECTED 写 `reject_reason`，且 receipt 保留原文。
#
# A8. 6.1 细节缺失：`row` 是 1 起始还是 0 起始没有写；`exchange_rate` 缺省值未定义；
#     「未来日期」按服务器时区还是 UTC 判定。示例里 row:2/row:3 暗示 1 起始。
#     建议判定：row 1 起始、exchange_rate 缺省 "1"、日期按服务器本地日期判定，
#     并在契约里注明（否则跨时区 CI 会在月末/凌晨随机失败）。
#
# 另附一条实现提示（不算歧义但会卡验收）：#9 里「verify 链完好」没有可断言的接口字段，
# 脚本暂时退化为「列表 status=TO_VERIFY 可检索 + retry_count 存在 + receipt 含 mock」。
# 若实现方愿意补 `verify_count` / `verified_by` 字段，可把这一条断言换成精确字段校验。


if __name__ == "__main__":
    sys.exit(main())
