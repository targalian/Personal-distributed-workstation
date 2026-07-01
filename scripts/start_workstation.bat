@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."

REM ========================================
REM  LAN Mesh Work Station - One-Click Start
REM  双击此文件即可启动工作站
REM ========================================

echo.
echo ========================================
echo  LAN Mesh Work Station - One-Click Start
echo ========================================
echo.

REM ── Step 1: 检查 Python ──
echo [step] 1/5 检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] 未找到 python, 请安装 Python 3.11+ 并加入 PATH
    echo   下载: https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   -^> %PYVER%

REM ── Step 2: 虚拟环境 ──
echo [step] 2/5 检查虚拟环境...
if not exist ".venv\Scripts\python.exe" (
    if exist ".venv" (
        echo   -^> .venv 存在但不是 Windows 格式, 重建...
        rmdir /s /q ".venv"
    )
    echo   创建虚拟环境...
    python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo   [ERROR] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo   -^> 已创建 .venv\
) else (
    echo   -^> 虚拟环境已存在 (skip)
)
set PYTHON=.venv\Scripts\python.exe
set PIP=.venv\Scripts\pip.exe

REM ── Step 3: 依赖安装 ──
echo [step] 3/5 检查依赖...
%PIP% show fastapi >nul 2>&1
if errorlevel 1 (
    echo   安装依赖 (首次启动可能需要 1-2 分钟)...
    %PIP% install -r requirements.txt -q
    if errorlevel 1 (
        echo   [ERROR] 依赖安装失败, 请手动运行: .venv\Scripts\pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo   -^> 依赖安装完成
) else (
    echo   -^> 依赖已安装 (skip)
)

REM ── Step 4: 配置文件 ──
echo [step] 4/5 检查配置...
if exist "lan_mesh\model_pool.example.yaml" (
    if not exist "lan_mesh\model_pool.yaml" (
        copy "lan_mesh\model_pool.example.yaml" "lan_mesh\model_pool.yaml" >nul
        echo   -^> 已创建 lan_mesh\model_pool.yaml (请编辑填入 API Key)
    ) else (
        echo   -^> model_pool.yaml 已存在 (skip)
    )
) else (
    echo   -^> model_pool.example.yaml 不存在 (skip)
)

REM ── Step 5: 启动 ──
echo [step] 5/5 启动 Station Director...
echo.
echo ========================================
echo  LAN Mesh Station Director
echo  Web UI: http://localhost:45470
echo  Secretary: 未激活 (启动后在 Web UI 中点击「设为主节点」)
echo ========================================
echo.

REM 启动 Station Director
%PYTHON% main.py station --port 45470
pause
