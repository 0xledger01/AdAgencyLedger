# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：产出单文件免安装 CrossAccount.exe。

产物布局（onefile，运行时解压到 sys._MEIPASS）：
    CrossAccount.exe            ← dist/CrossAccount.exe
    ├─ (内部) _MEIPASS/web                ← frontend/dist 全量（config.WEB_DIR）
    ├─ (内部) _MEIPASS/rapidocr_onnxruntime/  ← OCR 模型 + 包内 config.yaml
    └─ (旁边) data/app.db, data/uploads/   ← 可写数据，落 exe 同级，重启不丢

构建：pyinstaller build.spec --clean --noconfirm
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, get_package_paths

PROJECT_ROOT = Path(SPECPATH).resolve()
WEB_DIST = PROJECT_ROOT / "frontend" / "dist"
ENTRY = PROJECT_ROOT / "backend" / "launcher.py"

# ---------------------------------------------------------------------------
# 1. 前端产物：必须放到 web/，与 config.WEB_DIR = _MEIPASS / "web" 对应
# ---------------------------------------------------------------------------
if not (WEB_DIST / "index.html").is_file():
    raise SystemExit(
        f"[build.spec] frontend build not found: {WEB_DIST / 'index.html'}; "
        "run `npx vite build` in frontend/ first (or just run build_exe.bat)"
    )
datas = [(str(WEB_DIST), "web")]

# ---------------------------------------------------------------------------
# 2. rapidocr 资源：包内代码按 __file__ 找 models/ 和 config.yaml，
#    因此必须**保留包内相对路径**（整体落到 rapidocr_onnxruntime/ 下），
#    只 collect_data_files 会把文件摊平到根，引擎会静默降级成 manual。
# ---------------------------------------------------------------------------
try:
    _rapid_base, _rapid_pkg = get_package_paths("rapidocr_onnxruntime")
    datas.append((str(_rapid_pkg), "rapidocr_onnxruntime"))
    print(f"[build.spec] rapidocr package dir bundled as-is: {_rapid_pkg}")
except Exception as exc:  # noqa: BLE001 - 退回 collect_data_files 并告警
    print(f"[build.spec][WARN] get_package_paths failed ({exc}), fallback to collect_data_files")
    datas += collect_data_files("rapidocr_onnxruntime", includes=["**/*"])

# ---------------------------------------------------------------------------
# 2.1 OCR 的「隐式依赖」：rapidocr 用 importlib.import_module(config 里的名字)
#     动态加载 ch_ppocr_v3_det / ch_ppocr_v3_rec / ch_ppocr_v2_cls，
#     打包器静态分析看不到，靠上面的整包 datas 兜住 .py 源码；
#     而它们的第三方依赖（pyclipper / shapely / six / yaml）必须显式声明，
#     否则 exe 里会报 ModuleNotFoundError: No module named 'pyclipper'，
#     并被 license_ocr 静默降级成人工填写。
#     shapely 2.x 的 geos dll 放在 shapely.libs/（包目录之外），
#     只有 collect_all 才带得全，所以要收 binaries 而不只是 datas。
# ---------------------------------------------------------------------------
extra_binaries = []
ocr_hiddenimports = [
    "pyclipper",
    "six",
    "shapely",
    "shapely.geometry",
    "yaml",
    "PIL",
    "PIL.Image",
    # 动态加载的三个子模块（顶格名字，因为 rapidocr 把包目录 append 进 sys.path）
    "ch_ppocr_v3_det",
    "ch_ppocr_v3_rec",
    "ch_ppocr_v2_cls",
]
for _pkg in ("pyclipper", "shapely"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        extra_binaries += _b
        ocr_hiddenimports += _h
        print(f"[build.spec] collect_all({_pkg}): datas={len(_d)} binaries={len(_b)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[build.spec][WARN] collect_all({_pkg}) failed: {exc}")
        ocr_hiddenimports.append(_pkg)

# ---------------------------------------------------------------------------
# 3. 隐式导入：uvicorn 的实现模块全靠字符串动态导入，不写这里 exe 起不来
# ---------------------------------------------------------------------------
hiddenimports = [
    # uvicorn
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # SQLAlchemy / sqlite
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.sql.default_comparator",
    # OCR 引擎（导入失败会自动降级，但仍尽量打进去）
    "rapidocr_onnxruntime",
    "rapidocr_onnxruntime.rapid_ocr_api",
    "onnxruntime",
    "onnxruntime.capi",
    "onnxruntime.capi._pybind_state",
    "cv2",
    # 业务侧的运行时导入（避免被打包器静态分析漏掉）
    "backend.app.main",
    "backend.app.seed",
    "pydantic_settings",
    "email.mime.text",
    "multipart",
] + ocr_hiddenimports

# ---------------------------------------------------------------------------
# 4. 排除项：压体积。若 OCR / cv2 在 exe 里起不来，先撤回 setuptools 与 scipy
# ---------------------------------------------------------------------------
excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "pytest",
    "_pytest",
    "PyInstaller",
    "setuptools",
    "pip",
    "wheel",
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(PROJECT_ROOT)],
    binaries=extra_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# onefile：binaries/datas 全部塞进 EXE，不产出 COLLECT 目录
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CrossAccount",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 会压坏 cv2 / onnxruntime 的 dll，必须关
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 必须 console：启动异常要能被看到
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
