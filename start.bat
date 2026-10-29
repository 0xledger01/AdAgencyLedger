@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================
echo   海外广告代理业务系统 - 本地启动
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [准备] 首次运行，创建虚拟环境...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)

if not exist "frontend\node_modules" (
    echo [准备] 首次运行，安装前端依赖...
    pushd frontend
    call npm install --silent
    popd
)

echo [1/2] 启动后端  http://127.0.0.1:8012
start "adsys-backend" cmd /k ".venv\Scripts\python.exe -m uvicorn backend.app.main:app --port 8012 --host 127.0.0.1"

echo [2/2] 启动前端  http://127.0.0.1:5173
start "adsys-frontend" cmd /k "cd /d frontend && npx vite --port 5173 --strictPort --host 127.0.0.1"

echo.
echo 等待服务就绪（约 8 秒）...
timeout /t 8 /nobreak >nul
start "" http://127.0.0.1:5173

echo.
echo 内部后台  http://127.0.0.1:5173
echo 客户门户  http://127.0.0.1:5173/portal
echo 接口文档  http://127.0.0.1:8012/docs
echo.
echo 内部账号：admin / admin123 ；sales01 / risk01 / finance01 / ops01 密码 123456
echo 客户账号：client1 / client2 / client3 密码 123456
echo.
echo 两个服务窗口会保留不闪退，出错时窗口里的报错可以直接看到。
echo.
pause
