@echo off
cd /d "%~dp0.."
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ========================================
REM  LAN Mesh Work Station - One-Click Start
REM ========================================

echo.
echo ========================================
echo  LAN Mesh Work Station - One-Click Start
echo ========================================
echo.

REM -- Step 1: Check Python --
echo [step] 1/5 Check Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found. Please install Python 3.11+ and add to PATH
    echo   Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   -^> %PYVER%

REM -- Step 2: Virtual Environment --
echo [step] 2/5 Check venv...
if not exist ".venv\Scripts\python.exe" (
    if exist ".venv" (
        echo   -^> .venv exists but not Windows format, rebuilding...
        rmdir /s /q ".venv"
    )
    echo   Creating virtual environment...
    python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo   [ERROR] venv creation failed
        pause
        exit /b 1
    )
    echo   -^> Created .venv\
) else (
    echo   -^> venv already exists ^(skip^)
)
set PYTHON=.venv\Scripts\python.exe
set PIP=.venv\Scripts\pip.exe

REM -- Step 3: Install Dependencies --
echo [step] 3/5 Check dependencies...
%PIP% show fastapi >nul 2>&1
if errorlevel 1 (
    echo   Installing dependencies ^(--requires 1-2 min on first run--^)...
    echo   Using Tsinghua mirror for faster download in China
    %PIP% install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
    if errorlevel 1 (
        echo.
        echo   [WARN] Mirror install failed, retrying with default PyPI...
        %PIP% install -r requirements.txt
        if errorlevel 1 (
            echo   [ERROR] Dependencies install failed
            echo   Please run manually: .venv\Scripts\pip install -r requirements.txt
            pause
            exit /b 1
        )
    )
    echo   -^> Dependencies installed
) else (
    echo   -^> Dependencies already installed ^(skip^)
)

REM -- Step 4: Config File --
echo [step] 4/5 Check config...
if exist "lan_mesh\model_pool.example.yaml" (
    if not exist "lan_mesh\model_pool.yaml" (
        copy "lan_mesh\model_pool.example.yaml" "lan_mesh\model_pool.yaml" >nul
        echo   -^> Created lan_mesh\model_pool.yaml ^(--edit to add API Key--^)
    ) else (
        echo   -^> model_pool.yaml already exists ^(skip^)
    )
) else (
    echo   -^> model_pool.example.yaml not found ^(skip^)
)

REM -- Step 5: Launch --
echo [step] 5/5 Starting Station Director...
echo.
echo ========================================
echo  LAN Mesh Station Director
echo  Web UI: http://localhost:45470
echo  Secretary: not active ^(--click "Set as Master" in Web UI--^)
echo ========================================
echo.

REM Launch Station Director
%PYTHON% main.py station --port 45470
pause
