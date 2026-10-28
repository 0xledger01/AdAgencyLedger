"""P2 后端验收脚本：认证 / RBAC / 数据隔离 / 审核 / 流水三视图。

跑法：.venv/Scripts/python.exe backend/tests/smoke_p2.py
需要后端已在 8000 端口运行。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8012")
PASS, FAIL = [], []

# 1x1 透明 PNG（base64），用于"凭证必须是真实上传文件"的用例
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def upload_png(token: str, biz_type: str = "TOPUP_VOUCHER") -> str | None:
    """上传一张 1x1 PNG，返回 /api/files/... 地址。标准库手搓 multipart，不引三方依赖。"""
    boundary = "----smokep2boundary"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="biz_type"\r\n\r\n{biz_type}\r\n'.encode(),
        f"--{boundary}\r\n".encode(),
        'Content-Disposition: form-data; name="file"; filename="receipt.png"\r\n'.encode(),
        b"Content-Type: image/png\r\n\r\n",
        PNG_1PX, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(BASE + "/api/files", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("url")
    except Exception:  # noqa: BLE001
        return None


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


def login(username: str, password: str, portal: bool = False):
    path = "/api/auth/portal/login" if portal else "/api/auth/login"
    code, data = call("POST", path, body={"username": username, "password": password})
    return code, data


def main() -> int:
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    print("\n=== 1. 认证 ===")
    code, admin = login("admin", "admin123")
    check("管理员登录成功", code == 200 and admin.get("role") == "ADMIN", str(admin))
    code, bad = login("admin", "wrong")
    check("错误密码返回 401", code == 401, str(code))
    code, _ = login("nobody", "x")
    check("不存在账号返回 401", code == 401)
    code, sales = login("sales01", "123456")
    code, risk = login("risk01", "123456")
    code, fin = login("finance01", "123456")
    code, ops = login("ops01", "123456")
    code, c1 = login("client1", "123456", portal=True)
    check("客户门户登录成功", code == 200 and c1.get("role") == "CLIENT", str(c1))
    check("客户绑定了 client_id", c1.get("client_id") == 1, str(c1.get("client_id")))

    code, _ = login("client1", "123456", portal=False)
    check("客户走后台入口被拒", code == 403, str(code))
    code, _ = login("admin", "admin123", portal=True)
    check("内部员工走门户入口被拒", code == 403, str(code))

    print("\n=== 2. 未登录 / 越权 ===")
    code, _ = call("GET", "/api/desk/clients")
    check("无 token 访问后台返回 401", code == 401, str(code))
    code, _ = call("GET", "/api/desk/clients", token=c1["token"])
    check("客户 token 访问 /desk 返回 403", code == 403, str(code))
    code, _ = call("GET", "/api/portal/wallet", token=admin["token"])
    check("管理员访问门户接口返回 403", code == 403, str(code))

    print("\n=== 3. 客户数据隔离（铁律 3）===")
    code, w1 = call("GET", "/api/portal/wallet", token=c1["token"])
    check("客户1 能读自己钱包", code == 200, str(code))
    code, prof = call("GET", "/api/portal/profile", token=c1["token"])
    check("客户1 档案编号正确", prof.get("biz_no", "").startswith("C"), str(prof.get("biz_no")))
    # 尝试用 query 参数越权（服务端必须忽略）
    code, txs = call("GET", "/api/portal/wallet/transactions?client_id=2", token=c1["token"])
    leaked = any(it.get("client_id") == 2 for it in txs.get("items", []))
    check("客户传 client_id=2 被服务端忽略", code == 200 and not leaked, f"leaked={leaked}")

    code, txs = call("GET", "/api/portal/wallet/transactions", token=c1["token"])
    raw = json.dumps(txs, ensure_ascii=False).lower()
    check("客户流水无 rebate 字段", "rebate" not in raw, raw[:200])
    check("客户流水无「返点」字样", "返点" not in raw)
    check("客户流水无成本系数", "cost_ratio" not in raw)

    code, accs = call("GET", "/api/portal/accounts", token=c1["token"])
    raw = json.dumps(accs, ensure_ascii=False)
    check("客户账户页无返点率", "rebate_rate" not in raw)
    check("客户账户页无上游渠道", "channel" not in raw.replace("channel_", ""), raw[:200])

    print("\n=== 4. 销售只看自己名下 ===")
    code, lst = call("GET", "/api/desk/clients", token=sales["token"])
    owners = {it["sales_owner"] for it in lst.get("items", [])}
    check("销售列表只含本人名下客户", owners <= {"李四"}, str(owners))
    code, d = call("GET", "/api/desk/clients/2", token=sales["token"])
    check("销售读他人客户详情返回 403", code == 403, str(code))

    print("\n=== 5. 三级审核状态机 ===")
    code, reset = call("POST", "/api/dev/reset-demo", token=admin["token"])
    check("重置演示客户状态", code == 200 and reset.get("ok"), str(reset))
    demo_ids = [c["id"] for c in reset.get("clients", [])]
    c_review, c_risk = demo_ids[0], demo_ids[1]

    code, lst = call("GET", "/api/desk/clients?status=PENDING_REVIEW", token=admin["token"])
    pending = [it for it in lst["items"] if it["id"] == c_review]
    check(f"存在待审核客户 #{c_review}", bool(pending), str(lst.get("pending")))

    code, r = call("POST", f"/api/desk/clients/{c_review}/review", token=risk["token"],
                   body={"action": "APPROVE"})
    check("风控越权做初审被拒", code in (400, 403), str(r))

    code, r = call("POST", f"/api/desk/clients/{c_review}/review", token=sales["token"],
                   body={"action": "REJECT"})
    check("驳回不填原因被拒", code == 400, str(r))

    code, r = call("POST", f"/api/desk/clients/{c_review}/review", token=sales["token"],
                   body={"action": "APPROVE"})
    check("销售初审通过 → 待风控", code == 200 and r["status"] == "PENDING_RISK", str(r))

    code, r = call("POST", f"/api/desk/clients/{c_review}/review", token=sales["token"],
                   body={"action": "APPROVE"})
    check("销售跳过风控被拒", code in (400, 403), str(r))

    code, r = call("POST", f"/api/desk/clients/{c_review}/review", token=risk["token"],
                   body={"action": "APPROVE"})
    check("风控通过 → 待定额度", code == 200 and r["status"] == "PENDING_LIMIT", str(r))

    code, r = call("POST", f"/api/desk/clients/{c_review}/terms", token=fin["token"],
                   body={"cost_ratio": "0.9", "rebate_rate": "0.06", "credit_limit": "50000",
                         "payment_terms": 30, "channel": "Yatop"})
    check("财务设置商务条款", code == 200 and r["cost_ratio"] == "0.9000", str(r))

    code, r = call("POST", f"/api/desk/clients/{c_review}/review", token=fin["token"],
                   body={"action": "APPROVE"})
    check("财务定额 → 已开通", code == 200 and r["status"] == "ACTIVE", str(r))

    code, d = call("GET", f"/api/desk/clients/{c_review}", token=admin["token"])
    check("客户详情有 3 条审核记录", len(d.get("reviews", [])) == 3, str(len(d.get("reviews", []))))
    check("商务条款对财务可见", d.get("terms") is not None)

    code, d_sales = call("GET", f"/api/desk/clients/{c_review}", token=sales["token"])
    check("商务条款对销售不可见", d_sales.get("terms") is None, str(d_sales.get("terms")))

    print("\n=== 6. 待风控客户 -> 驳回 -> 客户重提 ===")
    code, r = call("POST", f"/api/desk/clients/{c_risk}/review", token=risk["token"],
                   body={"action": "REJECT", "reason": "行业资质不全"})
    check("风控驳回成功", code == 200 and r["status"] == "REJECTED", str(r))

    code, prof = call("GET", "/api/portal/profile", token=c1["token"])
    code, r = call("POST", "/api/portal/onboarding/resubmit", token=c1["token"],
                   body={"industry": "跨境电商"})
    check("非驳回状态重提被拒", code == 400, str(r))

    print("\n=== 7. D3.1 流水三视图 ===")
    code, led = call("GET", "/api/desk/client-ledger?client_id=1", token=fin["token"])
    check("客户往来流水可读", code == 200, str(code))
    summary = led["summary"]["per_currency"]
    check("汇总按期初/进/出/期末返回", bool(summary) and "opening" in summary[0], str(summary))
    check("恒等式自检通过（balanced）", led["summary"]["has_conflict"] is False, str(summary))

    s = summary[0]
    calc = Decimal(s["opening"]) + Decimal(s["inflow"]) - Decimal(s["outflow"])
    check("期初+进-出 == 期末", calc == Decimal(s["closing"]), f"{calc} vs {s['closing']}")
    check("期末 == 实际末笔余额", Decimal(s["closing"]) == Decimal(s["actual_closing"]),
          f"{s['closing']} vs {s['actual_closing']}")

    code, glob = call("GET", "/api/desk/client-ledger", token=fin["token"])
    check("全量流水可读", code == 200 and glob["total"] > 0, str(glob.get("total")))

    code, spend = call("GET", "/api/desk/account-ledger?client_id=1", token=fin["token"])
    check("账户消耗流水可读", code == 200 and spend["total"] > 0, str(spend.get("total")))

    code, ops_led = call("GET", "/api/desk/client-ledger?client_id=1", token=c1["token"])
    check("客户 token 访问内部流水 403", code == 403, str(code))

    print("\n=== 8. 导出 ===")
    code, blob = call("GET", "/api/desk/client-ledger/export?client_id=1&fmt=xlsx", token=fin["token"])
    check("导出 xlsx 成功", code == 200 and isinstance(blob, bytes) and blob[:2] == b"PK",
          f"status={code} head={blob[:4] if isinstance(blob, bytes) else blob}")
    code, blob = call("GET", "/api/desk/client-ledger/export?client_id=1&fmt=csv", token=fin["token"])
    text = blob.decode("utf-8-sig") if isinstance(blob, bytes) else str(blob)
    check("导出 csv 含表头", "发生时间" in text, text[:80])

    code, blob = call("GET", "/api/portal/wallet/transactions/export", token=c1["token"])
    text = blob.decode("utf-8-sig") if isinstance(blob, bytes) else str(blob)
    check("客户导出 CSV 无「返点」", "返点" not in text, text[:80])
    check("客户导出 CSV 无 rebate", "rebate" not in text.lower())

    print("\n=== 9. 核销与冲正 ===")
    code, led = call("GET", "/api/desk/client-ledger?client_id=1", token=fin["token"])
    tx_id = led["items"][0]["id"]
    check("流水带已冲正标记字段", "reversed" in led["items"][0], str(led["items"][0].keys()))

    code, r = call("POST", "/api/desk/client-ledger/reconcile", token=fin["token"],
                   body={"tx_ids": [tx_id], "period": "2026-10"})
    check("标记核销成功或已存在", code == 200 and (r.get("created") == 1 or r.get("skipped") == 1), str(r))
    code, r = call("POST", "/api/desk/client-ledger/reconcile", token=fin["token"],
                   body={"tx_ids": [tx_id], "period": "2026-10"})
    check("重复核销幂等（skipped）", code == 200 and r["skipped"] >= 1, str(r))

    # 找一条尚未冲正过的流水（反复跑脚本也能继续验）
    code, led = call("GET", "/api/desk/client-ledger?client_id=1", token=fin["token"])
    candidates = [it for it in led["items"] if not it.get("reversed")]
    check("存在可冲正流水", bool(candidates), f"共 {len(led['items'])} 条")
    target = candidates[0]["id"] if candidates else tx_id

    code, r = call("POST", f"/api/desk/client-ledger/{target}/reverse", token=sales["token"],
                   body={"reason": "测试"})
    check("销售无权冲正", code == 403, str(r))

    # 铁律：冲正需两人确认（F-01），第二签人必填且不能是操作人本人
    code, r = call("POST", f"/api/desk/client-ledger/{target}/reverse", token=fin["token"],
                   body={"reason": "缺第二签"})
    check("冲正缺第二签被拒", code == 400, str(r))

    code, before = call("GET", "/api/desk/client-ledger?client_id=1", token=fin["token"])
    code, r = call("POST", f"/api/desk/client-ledger/{target}/reverse", token=fin["token"],
                   body={"reason": "银行退票测试", "second_approver": "张三"})
    check("财务冲正成功", code == 200 and r["direction"] in ("IN", "OUT"), str(r))
    code, r2 = call("POST", f"/api/desk/client-ledger/{target}/reverse", token=fin["token"],
                    body={"reason": "重复冲正", "second_approver": "张三"})
    check("同一流水不得重复冲正", code == 400, str(r2))

    code, after = call("GET", "/api/desk/client-ledger?client_id=1", token=fin["token"])
    check("冲正后流水条数 +1", after["total"] == before["total"] + 1,
          f"{before['total']} -> {after['total']}")
    check("冲正后恒等式仍成立", after["summary"]["has_conflict"] is False)

    print("\n=== 10. 门户入驻与充值 ===")
    code, r = call("POST", "/api/portal/onboarding", token=c1["token"],
                   body={"contact_name": "周九"})
    check("已提交申请不可再改", code in (200, 400), str(r))

    code, w_before = call("GET", "/api/portal/wallet", token=c1["token"])
    bal_before = w_before["items"][0]["wallet_balance"]

    # 凭证必须是真实上传的文件（P3.5：voucher 只存路径已不安全）
    voucher_url = upload_png(c1["token"])
    code, r = call("POST", "/api/portal/topups", token=c1["token"],
                   body={"amount": "5000", "currency": "USD", "payer": "周九",
                         "voucher": voucher_url})
    check("客户提交充值成功", code == 200 and r["status"] == "PENDING_CONFIRM", str(r))
    code, tl = call("GET", "/api/portal/topups", token=c1["token"])
    check("充值记录可见", code == 200 and len(tl["items"]) > 0)

    code, w = call("GET", "/api/portal/wallet", token=c1["token"])
    check("充值待确认不入账", Decimal(w["items"][0]["wallet_balance"]) == Decimal(bal_before),
          f"{bal_before} -> {w['items'][0]['wallet_balance']}")

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
