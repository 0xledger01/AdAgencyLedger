"""单文件 exe 的启动入口。

职责只有四件事，业务逻辑一律不碰（全在 backend.app 里）：

1. 选一个可用端口（8012 起，被占用就顺延，最多试 10 个）；
2. 用 uvicorn 起 FastAPI（**必须 console 模式**，出问题要能看到堆栈）；
3. 打印中文启动横幅：访问地址、内置账号、数据目录；
4. 端口就绪后自动打开浏览器（NO_BROWSER=1 可关闭）。

打包：`pyinstaller build.spec --clean --noconfirm`
开发：`python backend/launcher.py` 或 `python -m backend.launcher`
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# 导入引导：本文件既可能被 PyInstaller 当脚本执行（sys.path[0] = backend/），
# 也可能被 `python -m backend.launcher` 执行。统一把项目根塞进 sys.path，
# 这样 `from backend.app.main import app` 两种情况都能解析。
# ---------------------------------------------------------------------------
if not getattr(sys, "frozen", False):
    _PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

DEFAULT_PORT = 8012
PORT_ATTEMPTS = 10          # 8012..8021
HOST = "127.0.0.1"


def _force_utf8_console() -> None:
    """把控制台和 stdout 统一成 UTF-8。

    为什么必须做：Windows 控制台默认代码页是 GBK(936)，而 PyInstaller 打包后
    stdout 的编码随 locale 走。中文横幅按 UTF-8 写出去会被当成 GBK 解释，
    在窗口里显示成 `���������ҵ��ϵͳ` 这种乱码 —— 用户看不懂，报错时更慌。
    两个方向都要改：SetConsoleOutputCP 改控制台解码，reconfigure 改 Python 编码。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重定向到文件/管道时可能不支持，不阻断启动
            pass
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
        ctypes.windll.kernel32.SetConsoleCP(65001)        # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _port_available(port: int) -> bool:
    """探测 127.0.0.1:port 能否 bind。

    注意：这里**不设** SO_REUSEADDR。Windows 下设了它会允许绑定已占用的端口，
    探测就永远返回「可用」，端口冲突检测形同虚设。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((HOST, port))
        return True
    except OSError:
        return False


def find_free_port(start: int = DEFAULT_PORT, attempts: int = PORT_ATTEMPTS) -> int:
    """从 start 起找第一个能 bind 的端口；全被占用则回落到 start。"""
    for offset in range(attempts):
        port = start + offset
        if _port_available(port):
            return port
    return start


def _wait_until_listening(port: int, timeout: float = 60.0) -> bool:
    """轮询到端口真的能 accept 为止（onefile 冷启动解压可能拖到十几秒）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _open_browser_when_ready(port: int) -> None:
    """后台线程：服务就绪后再开浏览器，避免用户先看到「无法访问」。"""
    if os.environ.get("NO_BROWSER") == "1":
        print("[提示] NO_BROWSER=1，已跳过自动打开浏览器。", flush=True)
        return
    if not _wait_until_listening(port):
        print(f"[警告] 等待 http://{HOST}:{port} 就绪超时，请手动访问。", flush=True)
        return
    time.sleep(1.5)  # 留出 seed / 建表的时间，别让首页撞在启动中
    try:
        webbrowser.open(f"http://{HOST}:{port}/")
    except Exception:  # noqa: BLE001 - 打不开浏览器不该导致服务退出
        pass


def print_banner(port: int, data_dir: Path, actual_port_shifted: bool) -> None:
    """启动横幅：用户双击 exe 后唯一能看懂的说明书。"""
    line = "=" * 62
    print(line, flush=True)
    print("  AdAgencyLedger · 海外广告代理业务系统", flush=True)
    print("  已启动（关闭本窗口即停止服务）", flush=True)
    print(line, flush=True)
    print(f"  访问地址 : http://{HOST}:{port}/", flush=True)
    print(f"  客户门户 : http://{HOST}:{port}/portal", flush=True)
    print(f"  接口文档 : http://{HOST}:{port}/docs", flush=True)
    if actual_port_shifted:
        print(
            f"  端口提示 : {DEFAULT_PORT} 已被占用，自动顺延到 {port}",
            flush=True,
        )
    print(line, flush=True)
    print("  内部账号 : admin / admin123", flush=True)
    print("  客户账号 : client1 / 123456", flush=True)
    print(f"  数据目录 : {data_dir}", flush=True)
    print("             （数据库 app.db、上传件都存这里，备份请连同此目录）", flush=True)
    print(line, flush=True)


def main() -> int:
    """启动本地服务。返回进程退出码。"""
    _force_utf8_console()  # 必须在任何中文输出之前

    from backend.app.config import DATA_DIR, settings  # 延迟导入：--help 场景不建库

    try:
        start_port = int(os.environ.get("PORT_START", DEFAULT_PORT))
    except ValueError:
        start_port = DEFAULT_PORT
    port = find_free_port(start=start_port)
    shifted = port != start_port

    print_banner(port, DATA_DIR, shifted)

    # 端口就绪后开浏览器，放在后台线程里，不阻塞 uvicorn
    threading.Thread(
        target=_open_browser_when_ready, args=(port,), name="open-browser", daemon=True
    ).start()

    import uvicorn

    from backend.app.main import app

    # console 模式 + log_level=info：出问题用户能看到是哪一步炸的
    uvicorn.run(
        app,
        host=HOST,
        port=port,
        log_level="info" if settings.debug else "warning",
    )
    return 0


def _pause_before_exit() -> None:
    """异常时停在窗口上，防止双击运行一闪而过看不到原因。"""
    try:
        input("按回车键退出...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\n[提示] 已手动停止服务。", flush=True)
        exit_code = 0
    except Exception:  # noqa: BLE001 - 兜底，任何异常都要让用户看见
        print("\n" + "=" * 62, flush=True)
        print("  启动失败，错误详情如下：", flush=True)
        print("=" * 62, flush=True)
        traceback.print_exc()
        print("=" * 62, flush=True)
        print(
            "  排查建议：\n"
            "  1. 确认当前目录可写（数据库与上传件要写 exe 同级的 data/）；\n"
            "  2. 确认杀毒软件没有拦截 exe 的临时解压；\n"
            "  3. 端口被占用时可先关掉占用 8012 的程序，或设置环境变量 PORT_START 换起始端口。",
            flush=True,
        )
        _pause_before_exit()
        exit_code = 1
    sys.exit(exit_code)
