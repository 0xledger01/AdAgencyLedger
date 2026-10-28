"""种子数据：启动即可点。全部走账本内核，不塞假余额。"""

from __future__ import annotations

import base64
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    AdAccount,
    AgentChannel,
    Client,
    ClientBcid,
    ClientLead,
    ClientLeadStatus,
    ClientStatus,
    ClientTopUp,
    FundAccountType,
    FundOpType,
    OwnerType,
    PaymentMethod,
    Platform,
    PortalTicket,
    Role,
    SpendDaily,
    User,
    Wallet,
)
from .models.base import money, utcnow
from .models.docs import OpenRequest, OpenRequestStatus, RechargeOrder
from .services.auth import hash_password
from .services.fund_app import approve_application, create_application
from .services.ledger import get_or_create_wallet, post
from .services.recharge import create_recharge
from .services.topup import confirm_topup, create_topup

AGENTS = [
    ("Yatop", "YATOP", True),
    ("山恒", "SHANHENG", True),
    ("山恒点款", "SH_DK", False),
    ("芸星跨境", "YUNXING", False),
    ("富莱", "FULAI", False),
    ("众易", "ZHONGYI", False),
]

# 本地开发账号（上线前必须改密码）
USERS = [
    ("admin", "admin123", "张三", Role.ADMIN, None),
    ("sales01", "123456", "李四", Role.SALES, None),
    ("risk01", "123456", "王五", Role.RISK, None),
    ("finance01", "123456", "赵六", Role.FINANCE, None),
    ("ops01", "123456", "钱七", Role.OPS, None),
]


def seed_users(session: Session) -> int:
    """单独灌登录账号：库里已有客户时也要能补账号（升级场景）。"""
    if session.execute(select(func.count()).select_from(User)).scalar_one() > 0:
        return 0

    created = 0
    for username, pwd, display, role, client_id in USERS:
        session.add(
            User(
                username=username,
                password_hash=hash_password(pwd),
                display_name=display,
                role=role,
                client_id=client_id,
            )
        )
        created += 1

    clients = session.execute(
        select(Client).where(Client.status == ClientStatus.ACTIVE).order_by(Client.id)
    ).scalars().all()
    for idx, c in enumerate(clients, start=1):
        session.add(
            User(
                username=f"client{idx}",
                password_hash=hash_password("123456"),
                display_name=c.contact_name or c.company_name,
                role=Role.CLIENT,
                client_id=c.id,
                is_primary=True,
            )
        )
        created += 1

    session.flush()
    return created


def seed_fund_apps(session: Session) -> int:
    """线下汇款演示单据：3 张待审核 + 1 张审核通过（走真实入账）。

    幂等判据是账单 ID 前缀 CRT。授信账户要先有额度才能演示，首次补数据时
    给第二个演示客户补额度（只补 0 值，不覆盖人工改过的数）。
    """
    exists = session.execute(
        select(func.count())
        .select_from(ClientTopUp)
        .where(ClientTopUp.biz_no.like("CRT%"))
    ).scalar_one()
    if exists:
        return 0

    clients = session.execute(
        select(Client).where(Client.status == ClientStatus.ACTIVE).order_by(Client.id)
    ).scalars().all()
    if len(clients) < 2:
        return 0

    cash_client, credit_client = clients[0], clients[1]
    if money(credit_client.credit_limit) <= 0:
        credit_client.credit_limit = money("50000")
    if money(credit_client.credit_used) <= 0:
        credit_client.credit_used = money("2000")

    # 演示凭证用真实文件：财务点开能看见，审核通过也走得通
    voucher = _seed_voucher_url(session, cash_client.id)

    created = 0
    pending = [
        # 充值只做美金：应付金额与申请金额同币种、1:1，不再填汇率
        (cash_client, FundAccountType.CASH, FundOpType.INCREASE, "0.10",
         "0.10", "BANK-20261023-001", "李四", "演示：线下汇款待审核（小额）"),
        (cash_client, FundAccountType.CASH, FundOpType.INCREASE, "1000.00",
         "1000.00", "BANK-20261023-002", "李四", "演示：线下汇款待审核（大额）"),
        # 授信账户减款：占用授信额度（不动现金）
        (credit_client, FundAccountType.CREDIT, FundOpType.DECREASE, "5.00",
         "5.00", None, "孙八", "演示：授信账户减款待审核"),
    ]
    for client, account, op, amount, received, txn_no, applicant, remark in pending:
        create_application(
            session,
            client_id=client.id,
            fund_account_type=account,
            op_type=op,
            amount=amount,
            received_amount=received,
            txn_no=txn_no,
            voucher=voucher if account is FundAccountType.CASH else None,
            remark=remark,
            operator=applicant,
        )
        created += 1

    approved = create_application(
        session,
        client_id=cash_client.id,
        fund_account_type=FundAccountType.CASH,
        op_type=FundOpType.INCREASE,
        amount="0.10",
        received_amount="0.10",
        txn_no="BANK-20261022-009",
        voucher=voucher,
        remark="演示：线下汇款审核通过样张",
        operator="李四",
    )
    approve_application(
        session, approved.id, operator="赵六", note="演示：已核对银行流水"
    )
    created += 1

    session.flush()
    return created


DEMO_PENDING_CLIENTS = [
    ("某某公司", "91350200MA3XXXXX04", "王十二", "13800000004", ClientStatus.PENDING_REVIEW),
    ("某某公司", "91510100MA6XXXXX05", "冯十三", "13800000005", ClientStatus.PENDING_RISK),
]

# 客户管理列表的演示对照数据（CRM 客户名/编号、负责 AE、创建人）。
# 只给这 5 个演示客户补，真实客户不猜、不编。
# 键用统一社会信用代码：公司名已统一脱敏成「某某公司」，不能再拿来当唯一键。
DEMO_CLIENT_PROFILE = {
    "91440300MA5XXXXX01": ("某某公司", "H269151", "褚十五", "李四"),
    "91330100MA2XXXXX02": ("某某公司", "H269152", "卫十六", "沈十八"),
    "91440100MA5XXXXX03": ("某某公司", "H269153", "蒋十七", "李四"),
    "91350200MA3XXXXX04": ("某某公司", "H269154", "褚十五", "沈十八"),
    "91510100MA6XXXXX05": ("某某公司", "H269155", "卫十六", "李四"),
}


def backfill_demo_client_profile(session: Session) -> int:
    """老库升级：给已知演示客户补齐 P6 新增列（缺失才补，不覆盖已有值）。"""
    rows = session.execute(
        select(Client).where(Client.credit_code.in_(list(DEMO_CLIENT_PROFILE)))
    ).scalars().all()
    touched = 0
    for idx, c in enumerate(rows):
        profile = DEMO_CLIENT_PROFILE.get(c.credit_code or "")
        if profile is None:
            continue
        crm_name, crm_code, ae_owner, created_by = profile
        changed = False
        if not c.crm_name:
            c.crm_name, changed = crm_name, True
        if not c.crm_code:
            c.crm_code, changed = crm_code, True
        if not c.ae_owner:
            c.ae_owner, changed = ae_owner, True
        if not c.created_by:
            c.created_by, changed = created_by, True
        if c.status is ClientStatus.ACTIVE and c.last_active_at is None:
            c.last_active_at, changed = utcnow() - timedelta(days=idx + 1), True
        if changed:
            touched += 1
    session.flush()
    return touched


def seed_pending_clients(session: Session) -> int:
    """补 2 个待审核客户，用于演示三级审核流程。

    幂等判据是**统一社会信用代码**，不是状态，也不是公司名（演示客户的公司名统一
    脱敏成「某某公司」，已经不能当唯一键）：这两个客户被演示审到 ACTIVE 之后再启动，
    按状态判会认为"没有待审客户"又灌一对，每跑一次演示就多一对重名客户。
    """
    codes = [c[1] for c in DEMO_PENDING_CLIENTS]
    exists = session.execute(
        select(func.count()).select_from(Client).where(Client.credit_code.in_(codes))
    ).scalar_one()
    if exists:
        return 0

    created = 0
    for i, (company, code, contact, phone, st) in enumerate(DEMO_PENDING_CLIENTS, start=1):
        crm_name, crm_code, ae_owner, created_by = DEMO_CLIENT_PROFILE[code]
        c = Client(
            company_name=company, credit_code=code, contact_name=contact, phone=phone,
            email=f"pending-contact{i}@example.com", settle_currency="USD",
            source_channel="展会", sales_owner="李四", platforms="FB,GOOGLE",
            industry="跨境电商", target_regions="北美,东南亚",
            registered_place="中国", founded_at="2021-03-15",
            monthly_spend_expect=money("20000"), settle_intent="PREPAID",
            license_url="seed/license.png", idcard_url="seed/idcard.png",
            cost_ratio=money("0.93"), rebate_rate=money("0.04"), channel="Yatop",
            crm_name=crm_name, crm_code=crm_code, ae_owner=ae_owner, created_by=created_by,
            status=st,
        )
        session.add(c)
        session.flush()
        c.biz_no = f"C{utcnow():%Y%m%d}{c.id:04d}"
        created += 1
    session.flush()
    return created


def _seed_voucher_url(session: Session, client_id: int) -> str:
    """生成一张真实的凭证 PNG 并登记，让演示数据的「查看凭证」真的能点开。

    同内容会走 storage 的去重复用，多次启动不会堆文件。
    """
    from .services import storage

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB"
        "AAH8i7wAAAAASUVORK5CYII="
    )
    obj, _ = storage.save(
        session,
        data=png,
        original_name="转账回单.png",
        declared_type="image/png",
        biz_type="TOPUP_VOUCHER",
        uploader_id=None,
        client_id=client_id,
    )
    return storage.url_of(obj)


def seed_spend_demo(session: Session) -> int:
    """给 SEED 消耗补 GMV Max / BC 拆分与投放指标，并让趋势有起伏。

    只动 `source=SEED` 且 `impressions=0` 的行；真实导入的数据一律不碰。
    幂等：补过之后 impressions > 0，再启动不会重复改。
    """
    rows = session.execute(
        select(SpendDaily).where(
            SpendDaily.source == "SEED", SpendDaily.impressions == 0
        )
    ).scalars().all()
    for r in rows:
        # 老种子是每天固定 1200，报表趋势会是一条直线；按账户/日期做一条确定的波浪线
        if money(r.spend) == money("1200"):
            r.spend = money(700 + ((r.date.toordinal() * 13 + r.ad_account_id * 29) % 9) * 110)
        total = money(r.spend)
        r.base_spend = money(total * money(r.exchange_rate))
        gmv = money(total * Decimal("0.7"))
        bc = money(total - gmv)
        r.gmv_max_spend = gmv
        r.gmv_max_net_spend = money(gmv * Decimal("0.96"))
        r.bc_spend = bc
        r.bc_net_spend = money(bc * Decimal("0.98"))
        r.bc_cash_spend = money(bc * Decimal("0.9"))
        r.bc_grant_spend = money(bc - r.bc_cash_spend)
        # 千展 ≈ 8，点击率 ≈ 1.8%，转化率 ≈ 6%（只为演示曲线，不是统计结论）
        r.impressions = max(1, int(bc / Decimal("8") * 1000))
        r.clicks = int(Decimal(r.impressions) * Decimal("0.018"))
        r.conversions = int(Decimal(r.clicks) * Decimal("0.06"))
    session.flush()
    return len(rows)


def seed(session: Session) -> bool:
    if session.execute(select(func.count()).select_from(Client)).scalar_one() > 0:
        seed_users(session)
        seed_pending_clients(session)
        seed_p3_demo(session)
        seed_bcids(session)
        backfill_demo_client_profile(session)
        seed_fund_apps(session)
        seed_spend_demo(session)
        seed_portal_tickets(session)
        seed_client_leads(session)
        return False

    for name, code, settled in AGENTS:
        session.add(AgentChannel(name=name, code=code, settled=settled))
    session.flush()

    # 平台钱包（资产）：充值只做美金
    usd = get_or_create_wallet(session, owner_type=OwnerType.PLATFORM, owner_id=None, currency="USD")
    # 一代钱包（外部参照）
    agents = session.execute(select(AgentChannel)).scalars().all()
    for a in agents:
        get_or_create_wallet(
            session, owner_type=OwnerType.AGENT, owner_id=a.id, currency="USD"
        )

    # 平台初始资金：银行到账 200,000 USD
    post(
        session, wallet_id=usd.id, direction="IN", amount="200000", biz_type="TOPUP",
        idempotency_key="seed:platform:usd", operator="seed", remark="初始资金",
    )
    # 打给 Yatop 80,000 USD
    yatop = next(a for a in agents if a.code == "YATOP")
    yatop_wallet = session.execute(
        select(Wallet).where(Wallet.owner_type == OwnerType.AGENT, Wallet.owner_id == yatop.id)
    ).scalar_one()
    post(
        session, wallet_id=yatop_wallet.id, direction="IN", amount="80000", biz_type="TRANSFER",
        idempotency_key="seed:agent:yatop:in", operator="seed", remark="预存到一代",
    )
    post(
        session, wallet_id=usd.id, direction="OUT", amount="80000", biz_type="TRANSFER",
        idempotency_key="seed:platform:usd:out", operator="seed", remark="打给 Yatop",
    )

    clients_spec = [
        ("某某公司", "91440300MA5XXXXX01", "周九", "13800000001", "USD", "销售开拓", "李四", ClientStatus.ACTIVE),
        ("某某公司", "91330100MA2XXXXX02", "吴十", "13800000002", "USD", "转介绍", "陈十四", ClientStatus.ACTIVE),
        ("某某公司", "91440100MA5XXXXX03", "郑十一", "13800000003", "USD", "官网", "赵六", ClientStatus.ACTIVE),
        # 待审核客户：用于演示三级审核流程
        ("某某公司", "91350200MA3XXXXX04", "王十二", "13800000004", "USD", "展会", "李四", ClientStatus.PENDING_REVIEW),
        ("某某公司", "91510100MA6XXXXX05", "冯十三", "13800000005", "USD", "老客复购", "李四", ClientStatus.PENDING_RISK),
    ]
    created = []
    for i, (company, code, contact, phone, ccy, source, sales, st) in enumerate(clients_spec, start=1):
        crm_name, crm_code, ae_owner, created_by = DEMO_CLIENT_PROFILE[code]
        c = Client(
            company_name=company,
            credit_code=code,
            contact_name=contact,
            phone=phone,
            email=f"contact{i}@example.com",
            settle_currency=ccy,
            source_channel=source,
            sales_owner=sales,
            platforms="FB,GOOGLE",
            industry="跨境电商",
            target_regions="北美,东南亚",
            registered_place="中国",
            founded_at="2020-06-01",
            monthly_spend_expect=money("20000"),
            settle_intent="PREPAID",
            license_url="seed/license.png",
            idcard_url="seed/idcard.png",
            cost_ratio=money("0.92"),
            rebate_rate=money("0.05"),
            channel="Yatop",
            crm_name=crm_name,
            crm_code=crm_code,
            ae_owner=ae_owner,
            created_by=created_by,
            status=st,
            activated_at=utcnow() if st is ClientStatus.ACTIVE else None,
            last_active_at=(utcnow() - timedelta(days=i)) if st is ClientStatus.ACTIVE else None,
        )
        session.add(c)
        session.flush()
        c.biz_no = f"C{utcnow():%Y%m%d}{c.id:04d}"
        created.append(c)

    active = created[:3]

    # 广告账户
    acct_spec = [
        (active[0], "act_1001", Platform.FB, "USD", "12000.00", "Yatop", "America/New_York"),
        (active[0], "act_1002", Platform.GOOGLE, "USD", "3000.00", "Yatop", "America/Los_Angeles"),
        (active[1], "act_2001", Platform.FB, "USD", "8500.50", "山恒", "America/Chicago"),
        (active[2], "act_3001", Platform.TIKTOK, "USD", "26000.00", "Yatop", "Asia/Shanghai"),
    ]
    for c, acc_id, plat, ccy, bal, chan, tz in acct_spec:
        session.add(
            AdAccount(
                account_id=acc_id, platform=plat, currency=ccy, client_id=c.id,
                channel=chan, balance=money(bal), daily_limit=money("5000"),
                timezone=tz, opened_at=utcnow(),
            )
        )
    session.flush()

    # 充值 → 确认到账（走真实流水）
    # 凭证是真的文件：财务点开能看见，而不是一个打不开的字符串
    seed_voucher = _seed_voucher_url(session, active[0].id)
    topup_spec = [
        (active[0], "50000", "USD", PaymentMethod.BANK_TRANSFER, "周九", True),
        (active[1], "30000", "USD", PaymentMethod.THIRD_PARTY, "某某供应链", True),
        (active[2], "80000", "USD", PaymentMethod.BANK_TRANSFER, "郑十一", True),
        (active[0], "12000", "USD", PaymentMethod.BANK_TRANSFER, "周九", False),  # 待确认，不入账
    ]
    for c, amt, ccy, method, payer, confirmed in topup_spec:
        t: ClientTopUp = create_topup(
            session, client_id=c.id, amount=amt, currency=ccy,
            payment_method=method, payer=payer, voucher=seed_voucher,
        )
        if confirmed:
            confirm_topup(session, t.id, operator="赵六")

    # 近 7 天消耗
    accounts = session.execute(select(AdAccount)).scalars().all()
    for a in accounts:
        for d in range(1, 8):
            session.add(
                SpendDaily(
                    date=date.today() - timedelta(days=d),
                    ad_account_id=a.id,
                    spend=money("1200"),
                    currency=a.currency,
                    exchange_rate=money("1"),
                    base_spend=money("1200"),
                    source="SEED",
                )
            )

    # 登录账号：内部 5 个角色 + 每个已开通客户一个账号
    for username, pwd, display, role, client_id in USERS:
        session.add(
            User(
                username=username,
                password_hash=hash_password(pwd),
                display_name=display,
                role=role,
                client_id=client_id,
            )
        )
    for idx, c in enumerate(active, start=1):
        session.add(
            User(
                username=f"client{idx}",
                password_hash=hash_password("123456"),
                display_name=c.contact_name or c.company_name,
                role=Role.CLIENT,
                client_id=c.id,
                is_primary=True,
            )
        )

    session.flush()
    seed_p3_demo(session)  # 全新库也要有 P3 演示单据，否则那四页是空的
    seed_bcids(session)  # BC 管理：演示客户的默认绑定与手工添加项
    seed_spend_demo(session)
    seed_fund_apps(session)  # 线下汇款申请单：待审核 3 张 + 审核通过 1 张
    seed_portal_tickets(session)  # 问题工单：四张不同状态的演示单
    seed_client_leads(session)  # 门户开户申请：待联系 / 已联系 / 已转客户
    return True


def seed_bcids(session: Session) -> int:
    """BC 管理演示数据：三条手工绑定，第一条是开户默认项。幂等，只补不覆盖。"""
    client = session.execute(
        select(Client).where(Client.status == ClientStatus.ACTIVE).order_by(Client.id)
    ).scalars().first()
    if client is None:
        return 0
    existing = session.execute(
        select(func.count()).select_from(ClientBcid).where(ClientBcid.client_id == client.id)
    ).scalar_one()
    if existing:
        return 0
    rows = [
        ("765557932675017934", True),
        ("710141103420000001", False),
        ("710141103420000002", False),
    ]
    for bcid, is_default in rows:
        session.add(
            ClientBcid(
                client_id=client.id,
                bcid=bcid,
                source="MANUAL",
                source_detail="MANUAL",
                is_default=is_default,
                created_by="钱七",
            )
        )
    session.flush()
    return len(rows)


def seed_p3_demo(session: Session) -> int:
    """P3 演示数据：一张待审批开户单 + 一张待执行加款单。幂等，只补不覆盖。"""
    if session.execute(select(func.count()).select_from(OpenRequest)).scalar_one() > 0:
        return 0
    if session.execute(select(func.count()).select_from(RechargeOrder)).scalar_one() > 0:
        return 0

    client = session.execute(
        select(Client).where(Client.status == ClientStatus.ACTIVE).order_by(Client.id)
    ).scalars().first()
    if client is None:
        return 0

    created = 0
    req = OpenRequest(
        client_id=client.id,
        platform=Platform.FB,
        bm_id="BM-DEMO-0001",
        timezone="America/New_York",
        currency=client.settle_currency,
        daily_limit=money("5000"),
        source="CLIENT",
        subject=client.company_name,
        apply_count=1,
        completed_count=0,
        open_method="AUTO",
        applicant=client.contact_name,
        remark="演示数据：客户门户提交的开户申请",
        status=OpenRequestStatus.PENDING_APPROVAL,
    )
    session.add(req)
    session.flush()
    req.biz_no = f"OR{utcnow():%Y%m%d}{req.id:05d}"
    session.flush()
    created += 1

    # 加款单必须有人"点一下"才存在：这里由种子模拟运营发起，走 freeze 冻结占位
    account = session.execute(
        select(AdAccount).where(AdAccount.client_id == client.id).order_by(AdAccount.id)
    ).scalars().first()
    if account is not None:
        create_recharge(
            session, ad_account_id=account.id, amount="500",
            request_no="SEED-DEMO-RECHARGE-001",
            remark="演示：运营手动发起，等待 RPA 执行", operator="钱七",
        )
        created += 1

    session.flush()
    return created


def seed_portal_tickets(session: Session) -> int:
    """问题工单演示数据：待处理 / 处理中 / 已解决 / 已关闭各一张。

    幂等判据是"工单表为空"：只要有人真的提过工单，就不再往库里塞演示单，
    避免每次重启都在客户列表里多出几条假问题。
    """
    if session.execute(select(func.count()).select_from(PortalTicket)).scalar_one() > 0:
        return 0

    clients = session.execute(
        select(Client).where(Client.status == ClientStatus.ACTIVE).order_by(Client.id)
    ).scalars().all()
    if not clients:
        return 0

    now = utcnow()
    specs = [
        (
            clients[0],
            "账户余额对不上",
            "后台显示余额 12,000 USD，但广告账户后台只剩 9,800 USD，麻烦核对一下是不是有未同步的消耗。",
            "PENDING",
            "HIGH",
            None,
            None,
            None,
        ),
        (
            clients[min(1, len(clients) - 1)],
            "想给新开的广告账户加预算",
            "新开户的 act_2001 想先加 5,000 USD 跑测试，需要走什么流程？",
            "PROCESSING",
            "NORMAL",
            f"[{now - timedelta(hours=3):%Y-%m-%d %H:%M} 钱七]\n已收到，正在核对账户状态，确认后会在充值管理里发起加款。",
            "钱七",
            now - timedelta(hours=3),
        ),
        (
            clients[min(2, len(clients) - 1)],
            "发票抬头需要修改",
            "上月对账单的发票抬头写错了，应该是「某某公司」，请重开一张。",
            "RESOLVED",
            "NORMAL",
            f"[{now - timedelta(days=1):%Y-%m-%d %H:%M} 赵六]\n已按新抬头重开，电子发票发到你邮箱了，请查收。",
            "赵六",
            now - timedelta(days=1),
        ),
        (
            clients[0],
            "对账单怎么导出",
            "想把近三个月的流水导出来给财务，页面上没找到入口。",
            "CLOSED",
            "LOW",
            f"[{now - timedelta(days=3):%Y-%m-%d %H:%M} 张三]\n「我的钱包」右上角有导出按钮，可按月份筛选后导出 Excel。",
            "张三",
            now - timedelta(days=3),
        ),
    ]
    created = 0
    for client, title, content, status, priority, reply, handled_by, handled_at in specs:
        session.add(
            PortalTicket(
                client_id=client.id,
                title=title,
                content=content,
                status=status,
                priority=priority,
                reply=reply,
                handled_by=handled_by,
                handled_at=handled_at,
            )
        )
        created += 1
    session.flush()
    return created


def seed_client_leads(session: Session) -> int:
    """门户开户申请（销售线索）演示数据：只补空表，不覆盖真实提交。"""
    if session.execute(select(func.count()).select_from(ClientLead)).scalar_one() > 0:
        return 0

    now = utcnow()
    specs = [
        (
            "某某公司",
            "林晓",
            "13800001234",
            "linxiao_tk",
            "linxiao@example.com",
            "TIKTOK",
            "30000",
            "主投东南亚，已有营业执照，希望本周内完成开户。",
            str(ClientLeadStatus.NEW),
            None,
            None,
            None,
            now - timedelta(hours=2),
        ),
        (
            "某某公司",
            "周宁",
            "13900005678",
            "zhouning_ads",
            "zhouning@example.com",
            "TIKTOK",
            "80000",
            "品牌出海，需要开 2 个广告账户，已有投放素材。",
            str(ClientLeadStatus.CONTACTED),
            "李四",
            "已电话联系，客户在准备营业执照，预计明天给资料。",
            now - timedelta(days=1),
            now - timedelta(days=2),
        ),
        (
            "某某公司",
            "何雨",
            "13700009999",
            "heyun_cloud",
            "heyu@example.com",
            "TIKTOK",
            "15000",
            "游戏出海，先小预算测试。",
            str(ClientLeadStatus.CONVERTED),
            "王五",
            "已在客户管理建档，等待初审。",
            now - timedelta(days=4),
            now - timedelta(days=5),
        ),
    ]
    created = 0
    for (
        company,
        contact,
        phone,
        wechat,
        email,
        platform,
        budget,
        remark,
        status,
        owner,
        note,
        contacted_at,
        created_at,
    ) in specs:
        lead = ClientLead(
            company_name=company,
            contact_name=contact,
            phone=phone,
            wechat=wechat,
            email=email,
            platform=platform,
            monthly_budget_expect=money(budget),
            remark=remark,
            source="PORTAL_SELF",
            status=status,
            owner=owner,
            note=note,
            contacted_at=contacted_at,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(lead)
        session.flush()
        lead.biz_no = f"LD{utcnow():%Y%m%d}{lead.id:05d}"
        created += 1
    session.flush()
    return created
