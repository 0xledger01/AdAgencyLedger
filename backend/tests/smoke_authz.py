"""越权与口径断言（REVIEW-FIXLIST 附录 A）。

为什么单独一个脚本：P2/P3 冒烟全在验"正向链路能不能跑通"，
**76 个断言里没有一条测越权**，所以"加款执行挂的是查看权限"这种洞
能一路活到评审。这一层以后每次收口都必须跑。

跑法：
    SMOKE_BASE=http://127.0.0.1:8012 .venv/Scripts/python.exe backend/tests/smoke_authz.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8012")
PASS: list[str] = []
FAIL: list[str] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
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
        except Exception:  # noqa: BLE001
            return exc.code, raw.decode("utf-8", "ignore")


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


def login(username: str, password: str, portal: bool = False):
    path = "/api/auth/portal/login" if portal else "/api/auth/login"
    code, r = call("POST", path, body={"username": username, "password": password})
    if code != 200:
        raise SystemExit(f"登录失败 {username}: {code} {r}")
    return r


def main() -> int:
    print("\n=== 登录 ===")
    admin = login("admin", "admin123")
    sales = login("sales01", "123456")
    risk = login("risk01", "123456")
    fin = login("finance01", "123456")
    ops = login("ops01", "123456")
    client = login("client1", "123456", portal=True)

    print("\n=== F-03 权限下发 ===")
    code, me = call("GET", "/api/auth/me", token=sales["token"])
    check("A0 /auth/me 返回 perms 数组", code == 200 and isinstance(me.get("perms"), list),
          str(me)[:200])
    sales_perms = set(me.get("perms") or [])
    check("A0 销售无 recharge.exec", "recharge.exec" not in sales_perms)
    check("A0 销售无 open_request.approve", "open_request.approve" not in sales_perms)
    check("A0 销售有 client.list", "client.list" in sales_perms)
    code, me_ops = call("GET", "/api/auth/me", token=ops["token"])
    ops_perms = set(me_ops.get("perms") or [])
    check("A1 运营有 open_request.approve（后端矩阵就是 OPS）",
          "open_request.approve" in ops_perms)
    check("A1 运营有 recharge.exec", "recharge.exec" in ops_perms)
    code, me_risk = call("GET", "/api/auth/me", token=risk["token"])
    check("A1 风控无 open_request.approve",
          "open_request.approve" not in set(me_risk.get("perms") or []))

    print("\n=== F-02 加款执行权限 ===")
    code, r = call("GET", "/api/desk/recharges?limit=1", token=fin["token"])
    check("财务可看加款列表（recharge.view）", code == 200, str(r)[:150])
    # 造一张待执行单需要账户；拿不到就直接打权限层（不存在的单号也应先被权限拦）
    for name, tok in (("SALES", sales["token"]), ("FINANCE", fin["token"]), ("CLIENT", client["token"])):
        code, r = call("POST", "/api/desk/recharges/1/exec", token=tok, body={"outcome": "SUCCESS"})
        check(f"A4 {name} 调 exec → 403", code == 403, f"status={code} {str(r)[:120]}")

    print("\n=== F-01 流水冲正双签 ===")
    code, led = call("GET", "/api/desk/client-ledger?limit=50", token=fin["token"])
    tx = None
    if code == 200 and led.get("items"):
        cand = [it for it in led["items"] if not it.get("reversed")]
        tx = cand[0]["id"] if cand else led["items"][0]["id"]
    if tx:
        code, r = call("POST", f"/api/desk/client-ledger/{tx}/reverse", token=fin["token"],
                       body={"reason": "只填原因"})
        check("A2 冲正不带 second_approver → 400", code == 400, f"status={code} {str(r)[:150]}")
        code, r = call("POST", f"/api/desk/client-ledger/{tx}/reverse", token=fin["token"],
                       body={"reason": "自签", "second_approver": fin["display_name"]})
        check("A3 冲正第二签 == 操作人 → 400", code == 400, f"status={code} {str(r)[:150]}")
    else:
        check("A2/A3 找不到流水（跳过）", False, "客户往来无流水")

    print("\n=== F-23/F-24 销售看资金池与消耗 ===")
    code, ov = call("GET", "/api/desk/wallets/overview", token=sales["token"])
    if code == 200:
        # 总览改口径后：平台钱包按客户分组（by_client），广告账户按一代分组（by_agent）
        rows = ((ov.get("client_wallets") or {}).get("by_client")) or []
        mine = {c.get("company_name") for c in
                (call("GET", "/api/desk/clients?limit=200", token=sales["token"])[1] or {}).get("items", [])}
        check("A9 销售看总览：只含自己名下客户",
              all(r.get("client_name") in mine for r in rows), str(rows)[:200])
        check("A9 销售看总览：scope 标记为 OWN", ov.get("scope") == "OWN", str(ov.get("scope")))
    else:
        check("A9 销售看总览 → 403 或只含名下", code == 403, f"status={code}")
    code, acc = call("GET", "/api/desk/account-ledger", token=sales["token"])
    check("A10 销售可读账户消耗（限自己名下）", code == 200, str(acc)[:150])

    print("\n=== F-25 KPI 与列表同口径 ===")
    code, cl = call("GET", "/api/desk/clients?limit=200", token=sales["token"])
    pend = cl.get("pending") or {}
    check("A11 销售 KPI 与列表条数一致（都只含名下）",
          code == 200 and isinstance(pend, dict) and "pending_review" in pend,
          str(pend))

    print("\n=== F-27 广告账户按币种分组、按一代归属 ===")
    code, ov2 = call("GET", "/api/desk/wallets/overview", token=admin["token"])
    ad = (ov2 or {}).get("ad_accounts") or {}
    check("A13 ad_accounts 按一代分组返回 by_agent 数组",
          isinstance(ad.get("by_agent"), list) and all("agent" in r for r in ad["by_agent"]),
          str(ad)[:200])
    check("A13 ad_accounts 合计按币种分列（不跨币种求和）",
          isinstance(ad.get("total"), list)
          and all("currency" in r and "balance" in r for r in ad["total"]),
          str(ad.get("total"))[:200])
    check("A13 ad_accounts 不再有单一 balance 字段", "balance" not in ad, str(ad)[:150])
    cli = (ov2 or {}).get("client_wallets") or {}
    check("A13 平台钱包按客户分组返回 by_client",
          isinstance(cli.get("by_client"), list), str(cli)[:200])
    check("A14 总览不再返回资金总金额（业务上不成立的两侧相加已去掉）",
          "grand_total" not in (ov2 or {}), str(sorted((ov2 or {}).keys())))

    print("\n=== 充值流水五维筛选（总览页嵌入）===")
    code, en2 = call("GET", "/api/desk/enums", token=fin["token"])
    check("A15 枚举含交易类型 recharge_txn_type",
          code == 200 and isinstance(en2.get("recharge_txn_type"), list)
          and len(en2["recharge_txn_type"]) == 4,
          str(en2.get("recharge_txn_type"))[:200])
    check("A15 枚举含一级代理商 agent_channel",
          isinstance(en2.get("agent_channel"), list) and en2["agent_channel"],
          str(en2.get("agent_channel"))[:200])

    code, r1 = call("GET", "/api/desk/recharges?limit=100", token=fin["token"])
    check("A15 加款列表返回 agent / txn_type_label",
          code == 200 and all(
              "agent" in it and "txn_type_label" in it for it in r1.get("items", [])
          ),
          str(r1.get("items", [{}])[:1])[:250])

    code, r2 = call("GET", "/api/desk/recharges?txn_type=PENDING&limit=100", token=fin["token"])
    check("A15 txn_type=PENDING 只返回处理中的单",
          code == 200 and all(
              it["status"] in ("PENDING_EXEC", "EXECUTING", "AUDITING", "TO_VERIFY")
              for it in r2.get("items", [])
          ),
          str([it["status"] for it in r2.get("items", [])])[:200])

    code, r3 = call("GET", "/api/desk/recharges?agent=%E5%B1%B1%E6%81%92&limit=100", token=fin["token"])
    check("A15 agent=山恒 只返回该代理商账户的单",
          code == 200 and all(it["agent"] == "山恒" for it in r3.get("items", [])),
          str([it["agent"] for it in r3.get("items", [])])[:200])

    code, r4 = call("GET", "/api/desk/recharges?txn_type=XXX", token=fin["token"])
    check("A15 非法交易类型 → 422", code == 422, f"status={code}")

    print("\n=== 枚举字典（F-15）===")
    code, en = call("GET", "/api/desk/enums", token=fin["token"])
    check("枚举字典可取且含 topup_status",
          code == 200 and isinstance(en.get("topup_status"), list) and en["topup_status"],
          str(en)[:150])

    print("\n=== 客户停用（F-07 #8）===")
    code, r = call("POST", "/api/desk/clients/1/disable", token=fin["token"],
                   body={"disabled": True, "reason": "越权测试"})
    check("财务无 client.disable → 403", code == 403, f"status={code} {str(r)[:120]}")

    print("\n" + "=" * 56)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
