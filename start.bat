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
start "adsys-backend" cmd /c ".venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --port 8012"

echo [2/2] 启动前端  http://127.0.0.1:5173
start "adsys-frontend" cmd /c "cd frontend && npx vite"

timeout /t 6 /nobreak >nul
start "" http://127.0.0.1:5173

echo.
echo 内部后台  http://127.0.0.1:5173
echo 客户门户  http://127.0.0.1:5173/portal
echo 接口文档  http://127.0.0.1:8012/docs
echo.
echo 内部账号：admin / admin123 ；sales01 / risk01 / finance01 / ops01 密码 123456
echo 客户账号：client1 / client2 / client3 密码 123456
echo.
echo 步骤：先登录内部后台看「客户管理」，审核一家公司走完三级流程，再去「资金流水」看三视图。
echo.
pause
