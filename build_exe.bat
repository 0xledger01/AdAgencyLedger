@echo off
chcp 936 >nul
cd /d "%~dp0"
setlocal

echo ============================================================
echo   CrossAccount.exe 打包（免安装单文件）
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 找不到 .venv\Scripts\python.exe
  echo        请先创建虚拟环境并安装 requirements.txt
  goto :fail
)

echo.
echo [1/3] 构建前端产物 frontend\dist ...
pushd frontend
call npx vite build
if errorlevel 1 (
  popd
  echo [错误] 前端构建失败，打包中止
  goto :fail
)
popd

echo.
echo [2/3] PyInstaller 打包（约 1-3 分钟，产物约 108 MB）...
.venv\Scripts\python.exe -m PyInstaller build.spec --clean --noconfirm
if errorlevel 1 (
  echo [错误] PyInstaller 打包失败，请查看上面的报错
  goto :fail
)

echo.
echo [3/3] 产物检查 ...
if not exist "dist\CrossAccount.exe" (
  echo [错误] 未生成 dist\CrossAccount.exe
  goto :fail
)
for %%F in ("dist\CrossAccount.exe") do (
  echo    路径：%%~fF
  echo    体积：%%~zF 字节
)

echo.
echo ============================================================
echo   打包完成。
echo   CrossAccount.exe 可单独拷到任意目录双击运行；
echo   首次运行会在同级自动创建 data 目录（数据库与上传件）。
echo ============================================================
exit /b 0

:fail
echo.
echo 打包中止。
exit /b 1
