from __future__ import annotations

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 路径三分法（打包成单文件 exe 的关键）
#
#   RESOURCE_DIR  只读资源：前端 dist(web/)、OCR 模型等，onefile 模式下在
#                 sys._MEIPASS 临时解压目录里，进程退出即销毁。
#   DATA_DIR      可写运行数据：SQLite、上传件、CRM 缓存。**必须**落在 exe
#                 同级目录，否则重启一次数据就没了。
#   WEB_DIR       前端产物目录，dev 下是 frontend/dist，打包后在 _MEIPASS/web。
#
# dev 模式下 FROZEN 为 False，三者与改造前完全一致（BASE_DIR = 项目根）。
# ---------------------------------------------------------------------------
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    APP_DIR = Path(sys.executable).resolve().parent
    DATA_DIR = APP_DIR / "data"
    WEB_DIR = RESOURCE_DIR / "web"
else:
    RESOURCE_DIR = Path(__file__).resolve().parents[2]
    APP_DIR = RESOURCE_DIR
    DATA_DIR = APP_DIR / "data"
    WEB_DIR = APP_DIR / "frontend" / "dist"

# 兼容旧引用（.env 查找位置、日志路径等仍按 BASE_DIR 理解）
BASE_DIR = APP_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # 是否由后端托管前端产物。main.py 里还会再判断 WEB_DIR/index.html 是否存在，
    # 两者同时满足才挂载静态路由（否则纯 API 模式照样能跑）。
    serve_frontend: bool = True

    app_name: str = "AdAgencyLedger · 海外广告代理业务系统"
    debug: bool = True

    # 本地默认 SQLite 单文件；换 Postgres 只改这一行
    database_url: str = f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"

    upload_dir: Path = DATA_DIR / "uploads"

    # 业务默认阈值（PRD 第 11 节待确认项的默认值，可被系统设置覆盖）
    large_amount_threshold_usd: float = 5000.0
    low_balance_alert_usd: float = 1000.0

    seed_on_startup: bool = True

    # 认证：本地开发用固定密钥，生产必须从环境变量注入
    secret_key: str = "dev-only-secret-change-in-production-9f3a2c1e"
    jwt_ttl_hours: int = 12

    # 客户门户账号的默认初始密码：添加客户时「初始密码」留空即用它，
    # 客户拿到「登录邮箱 + 默认密码」就能登录 /portal。
    # 生产用环境变量 CLIENT_DEFAULT_PASSWORD 覆盖，至少 6 位。
    client_default_password: str = "123456"

    # 门户充值：美元到账、人民币付款。汇率只影响「应付人民币」，
    # 美元钱包仍按 1:1 入账，财务按单据里的汇率快照核对。
    # 首选央行授权公布的中间价（中国外汇交易中心），失败再退商业行情接口。
    fx_pboc_url: str = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
    fx_provider_url: str = "https://open.er-api.com/v6/latest/USD"
    fx_backup_provider_url: str = (
        "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY"
    )
    fx_cache_ttl_seconds: int = 3600          # 1 小时内复用缓存，避免打爆免费接口
    fx_max_stale_hours: int = 24              # 超过 24 小时未更新则拒绝报价
    fx_drift_tolerance: float = 0.002         # 提交时汇率漂移超过 0.2% 要求重新确认
    fx_usd_cny_min: float = 4.0               # 汇率合理区间，防供应商返回脏数据
    fx_usd_cny_max: float = 12.0
    fx_topup_max_usd: float = 1_000_000.0     # 单笔充值上限

    # 证照 OCR（本地 rapidocr-onnxruntime，离线跑）
    # 关掉后 /parse-license 只做「客户档案 + 文件名」提示，不启动推理。
    ocr_enabled: bool = True
    ocr_max_side: int = 1400         # 长边先缩到这个像素：单张 6–10 秒，字段够用
    ocr_min_score: float = 0.45      # 低于这个置信度的文本框直接丢


settings = Settings()

DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.upload_dir.mkdir(parents=True, exist_ok=True)
