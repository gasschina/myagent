@echo off
REM MyAgent Windows 启动脚本

echo ========================================
echo   MyAgent - 本地桌面端执行型 AI 助手
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

REM 检查依赖
pip show pyyaml >nul 2>&1
if errorlevel 1 (
    echo [提示] 正在安装依赖...
    pip install -r requirements.txt
)

REM 检查配置
if not exist config.yaml (
    echo [提示] 未找到 config.yaml，使用默认配置
    echo [提示] 请编辑 config.yaml 填入你的 API Key
)

echo.
echo 选择运行模式:
echo   1. CLI 交互模式
echo   2. 系统托盘模式
echo   3. HTTP API 模式
echo.
set /p mode="请输入 (1/2/3): "

if "%mode%"=="1" python main.py
if "%mode%"=="2" python main.py --tray
if "%mode%"=="3" python main.py --server --port 8080

if not "%mode%"=="1" if not "%mode%"=="2" if not "%mode%"=="3" (
    echo 无效选择
    pause
)
