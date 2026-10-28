"""轻量迁移：对已有库做增量 ALTER，不删库、不丢数据。

生产环境应换成 Alembic；本地调试阶段保持零依赖、可重复执行。
"""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text


def _existing_columns(engine: Engine, table: str) -> set[str]:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


# (表, 列, DDL 片段)
_ADD_COLUMNS: list[tuple[str, str, str]] = [
    # P2：入驻申请与档案字段
    ("client", "target_regions", "VARCHAR(120)"),
    ("client", "monthly_spend_expect", "BIGINT NOT NULL DEFAULT 0"),
    ("client", "registered_place", "VARCHAR(120)"),
    ("client", "founded_at", "VARCHAR(20)"),
    ("client", "license_url", "VARCHAR(300)"),
    ("client", "idcard_url", "VARCHAR(300)"),
    ("client", "website", "VARCHAR(300)"),
    ("client", "qualification_url", "VARCHAR(300)"),
    ("client", "settle_intent", "VARCHAR(20)"),
    ("client", "version", "INTEGER NOT NULL DEFAULT 1"),
    ("client", "last_active_at", "DATETIME"),
    # P6：客户管理列表（CRM 对照 / 负责 AE / 创建人）
    ("client", "crm_name", "VARCHAR(200)"),
    ("client", "crm_code", "VARCHAR(60)"),
    ("client", "ae_owner", "VARCHAR(60)"),
    ("client", "created_by", "VARCHAR(60)"),
    # P7：客户自助开户申请
    ("open_request", "source", "VARCHAR(16) NOT NULL DEFAULT 'DESK'"),
    ("open_request", "subject", "VARCHAR(200)"),
    ("open_request", "apply_count", "INTEGER NOT NULL DEFAULT 1"),
    ("open_request", "completed_count", "INTEGER NOT NULL DEFAULT 0"),
    ("open_request", "open_method", "VARCHAR(16) NOT NULL DEFAULT 'AUTO'"),
    ("open_request", "applicant", "VARCHAR(60)"),
    ("open_request", "remark", "TEXT"),
    ("open_request", "processed_at", "DATETIME"),
    # 客户开户申请按 CRM 原型改版：企业材料 + 投放信息 + BCID + 产品链接
    ("open_request", "license_url", "VARCHAR(300)"),
    ("open_request", "credit_code", "VARCHAR(64)"),
    ("open_request", "registered_place", "VARCHAR(120)"),
    ("open_request", "industry", "VARCHAR(60)"),
    ("open_request", "target_country", "VARCHAR(60)"),
    ("open_request", "bc_ids", "TEXT"),
    ("open_request", "product_link", "VARCHAR(500)"),
    # D3 改版：线下汇款申请单（客户充值单扩展字段）
    ("client_topup", "fund_account_type", "VARCHAR(16) NOT NULL DEFAULT 'CASH'"),
    ("client_topup", "op_type", "VARCHAR(16) NOT NULL DEFAULT 'INCREASE'"),
    ("client_topup", "txn_no", "VARCHAR(64)"),
    ("client_topup", "payable_amount", "BIGINT NOT NULL DEFAULT 0"),
    # 存量单默认 USD；门户新单会写入 CNY 应付（历史单据保留原值，不在这里改写）
    ("client_topup", "payable_currency", "VARCHAR(8) NOT NULL DEFAULT 'USD'"),
    ("client_topup", "received_amount", "BIGINT NOT NULL DEFAULT 0"),
    ("client_topup", "applied_by", "VARCHAR(60)"),
    ("client_topup", "reviewed_by", "VARCHAR(60)"),
    ("client_topup", "reviewed_at", "DATETIME"),
    ("client_topup", "review_note", "TEXT"),
    ("client_topup", "rate_source", "VARCHAR(160)"),
    # 门户充值：客户选择的收款渠道（易宝 / 空中云汇 / 万里汇 / 线下汇款）
    ("client_topup", "payment_channel", "VARCHAR(20)"),
    # 交易记录页：加款单的对外交易编号（ADTR + 日期 + 6 位序号）
    ("recharge_order", "txn_no", "VARCHAR(40)"),
    # 账户管理：广告账户时区（开户单下户时带入）
    ("ad_account", "timezone", "VARCHAR(60)"),
    # D6 改版：消耗报表结构（GMV Max / BC 拆分 + 投放指标）
    ("spend_daily", "gmv_max_spend", "BIGINT NOT NULL DEFAULT 0"),
    ("spend_daily", "gmv_max_net_spend", "BIGINT NOT NULL DEFAULT 0"),
    ("spend_daily", "bc_spend", "BIGINT NOT NULL DEFAULT 0"),
    ("spend_daily", "bc_net_spend", "BIGINT NOT NULL DEFAULT 0"),
    ("spend_daily", "bc_cash_spend", "BIGINT NOT NULL DEFAULT 0"),
    ("spend_daily", "bc_grant_spend", "BIGINT NOT NULL DEFAULT 0"),
    # 计数列不能走上面 "BIGINT → NUMERIC" 的金额替换，用 INTEGER（SQLite 仍是 64 位）
    ("spend_daily", "impressions", "INTEGER NOT NULL DEFAULT 0"),
    ("spend_daily", "clicks", "INTEGER NOT NULL DEFAULT 0"),
    ("spend_daily", "conversions", "INTEGER NOT NULL DEFAULT 0"),
    # 问题工单（管理端受理 / 回复 / 结单）：老库补列，历史工单默认为待处理、普通优先级
    ("portal_ticket", "priority", "VARCHAR(10) NOT NULL DEFAULT 'NORMAL'"),
    ("portal_ticket", "reply", "TEXT"),
    ("portal_ticket", "handled_by", "VARCHAR(60)"),
    # 用 TIMESTAMP 而不是 DATETIME：SQLite 两种都收，Postgres 只认 TIMESTAMP
    ("portal_ticket", "handled_at", "TIMESTAMP"),
    # BC 管理：删除留墓碑，历史归纳不再把已删除的绑定重新补回来
    ("client_bcid", "is_deleted", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("client_bcid", "deleted_at", "TIMESTAMP"),
]

# 老数据回填：已开户的开户单视作完成开户数 = 申请开户数
_BACKFILLS: list[str] = [
    "UPDATE open_request SET completed_count = apply_count WHERE status = 'OPENED' AND completed_count = 0",
    "UPDATE open_request SET processed_at = approved_at WHERE processed_at IS NULL AND approved_at IS NOT NULL",
    # 老开户单没有开户主体，回落到客户公司名（列表/详情都靠这个字段展示）
    "UPDATE open_request SET subject = (SELECT company_name FROM client WHERE client.id = open_request.client_id) "
    "WHERE (subject IS NULL OR subject = '') AND EXISTS (SELECT 1 FROM client WHERE client.id = open_request.client_id)",
    # 老广告账户没有时区：先按媒体平台常用时区补默认值（页面仍显示真实登记值，可后续人工改）
    "UPDATE ad_account SET timezone = CASE platform "
    "WHEN 'FB' THEN 'America/New_York' "
    "WHEN 'GOOGLE' THEN 'America/Los_Angeles' "
    "WHEN 'TIKTOK' THEN 'Asia/Shanghai' END "
    "WHERE timezone IS NULL",
    # 老消耗数据只有总消耗：全部按 BC 口径回填，保证报表口径（GMV Max + BC）与历史一致
    "UPDATE spend_daily SET bc_spend = spend, bc_net_spend = spend, bc_cash_spend = spend "
    "WHERE bc_spend = 0 AND gmv_max_spend = 0 AND spend <> 0",
]

# 老库 ALTER 出来的列不会自动带索引（create_all 会跳过已存在的表），单独补
_INDEXES: list[tuple[str, str, str]] = [
    ("recharge_order", "ix_recharge_order_txn_no", "CREATE INDEX IF NOT EXISTS ix_recharge_order_txn_no ON recharge_order (txn_no)"),
]


def _backfill_recharge_txn_no(conn) -> None:  # noqa: ANN001
    """给没有交易编号的存量加款单补号（幂等，只补空值）。

    用 Python 回填而不是写死在 _BACKFILLS 里：SQLite 的 printf 与 Postgres 的
    lpad 不同，这里只需要一次 SELECT + 逐行 UPDATE，换库不用改 SQL。
    """
    rows = conn.execute(
        text("SELECT id, created_at FROM recharge_order WHERE txn_no IS NULL")
    ).all()
    for order_id, created_at in rows:
        day = str(created_at or "")[:10].replace("-", "")
        if len(day) != 8:
            continue
        conn.execute(
            text("UPDATE recharge_order SET txn_no = :no WHERE id = :id"),
            {"no": f"ADTR{day}{int(order_id):06d}", "id": int(order_id)},
        )


def run_migrations(engine: Engine) -> list[str]:
    """幂等执行：缺什么补什么，已存在就跳过。返回实际执行的 DDL 列表。"""
    dialect = engine.dialect.name
    applied: list[str] = []

    with engine.begin() as conn:
        for table, column, ddl in _ADD_COLUMNS:
            if dialect == "sqlite":
                cols = _existing_columns(engine, table)
            else:
                cols = _existing_columns(engine, table)
            if not cols:
                continue
            if column in cols:
                continue
            if dialect == "postgresql":
                ddl = ddl.replace("BIGINT NOT NULL DEFAULT 0", "NUMERIC(20,4) NOT NULL DEFAULT 0")
            stmt = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
            conn.execute(text(stmt))
            applied.append(stmt)

        for stmt in _BACKFILLS:
            # 表或列不存在时（全新库、尚未建表）直接跳过，交给 create_all
            if "open_request" in inspect(engine).get_table_names():
                conn.execute(text(stmt))

        # 交易编号回填：只在表与列都就绪时执行（全新库没有存量行）
        if "recharge_order" in inspect(engine).get_table_names():
            if "txn_no" in _existing_columns(engine, "recharge_order"):
                _backfill_recharge_txn_no(conn)

        for table, _index, stmt in _INDEXES:
            if table in inspect(engine).get_table_names():
                conn.execute(text(stmt))

    return applied
