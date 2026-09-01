@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0.."
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ========================================
REM  LAN Mesh Work Station - One-Click Start
REM  (bat 端与 ps1/sh 功能对齐)
REM ========================================

echo.
echo ========================================
echo  LAN Mesh Work Station - One-Click Start
echo ========================================
echo.

REM -- Step 1: Check Python (>=3.10) --
echo [step] 1/6 Check Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found. Please install Python 3.11+ and add to PATH
    echo   Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   -^> Python %PYVER%

REM -- 版本检查: 提取 major.minor 与 3.10 比较 --
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)
if %PYMAJOR% LSS 3 (
    echo   [ERROR] Python %PYVER% too old, need 3.10+
    pause
    exit /b 1
)
if %PYMAJOR% EQU 3 if %PYMINOR% LSS 10 (
    echo   [ERROR] Python %PYVER% too old, need 3.10+
    pause
    exit /b 1
)
echo   -^> Version OK

REM -- Step 2: Virtual Environment --
echo [step] 2/6 Check venv...
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

REM -- 升级 pip (新 venv 的 pip 通常很旧, 可能不支持现代 wheel) --
%PIP% --version 2>nul | findstr /i "2[1-9]\." >nul 2>&1
if errorlevel 1 (
    echo   Upgrading pip...
    %PIP% install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn 2>nul
    if errorlevel 1 (
        %PIP% install --upgrade pip -q 2>nul
    )
)

REM -- Step 3: Install Dependencies --
echo [step] 3/6 Check dependencies...
%PIP% show fastapi >nul 2>&1
if errorlevel 1 (
    echo   Installing dependencies ^(--requires 1-3 min on first run--^)...
    echo   [INFO] Trying Tsinghua mirror for faster download in China...
    echo.
    %PIP% install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
    if errorlevel 1 (
        echo.
        echo   [WARN] Tsinghua mirror failed, retrying with default PyPI...
        echo.
        %PIP% install -r requirements.txt
        if errorlevel 1 (
            echo   [ERROR] Dependencies install failed
            echo   Please run manually: .venv\Scripts\pip install -r requirements.txt
            pause
            exit /b 1
        )
    )
    echo.
    echo   -^> Dependencies installed
) else (
    echo   -^> Dependencies already installed ^(skip^)
)

REM -- Step 4: Config File --
echo [step] 4/6 Check config...
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

REM -- 加载 .env (如存在, 把 KEY=VALUE 注入环境变量) --
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%k in (".env") do (
        if not "%%k"=="" (
            set _LINE=%%k
            if not "!_LINE:~0,1!"=="#" (
                if not "%%l"=="" (
                    if not defined %%k set "%%k=%%l"
                )
            )
        )
    )
    echo   -^> Loaded .env
)

REM -- 检查 API Key --
set HAS_KEY=0
if defined DEEPSEEK_API_KEY set HAS_KEY=1
if defined OPENAI_API_KEY set HAS_KEY=1
if defined ALIYUN_TOKENPLAN_API_KEY set HAS_KEY=1
if defined ARK_API_KEY set HAS_KEY=1
if defined QWEN_API_KEY set HAS_KEY=1
if defined ANTHROPIC_API_KEY set HAS_KEY=1
if %HAS_KEY%==0 (
    echo   [WARN] No LLM API Key detected in environment or .env
    echo          Secretary chat will not use LLM until a key is configured.
    echo   Set in .env:  DEEPSEEK_API_KEY=sk-xxx
)

REM -- Step 5: Git Hooks --
echo [step] 5/6 Configure Git Hooks...
if exist ".githooks" (
    git config core.hooksPath .githooks 2>nul
    if not errorlevel 1 (
        echo   -^> core.hooksPath -^> .githooks ^(pre-push audit enabled^)
    ) else (
        echo   -^> git not found, hooks skipped
    )
) else (
    echo   -^> .githooks not found ^(skip^)
)

REM -- CLI Agent PATH (npm global + Node.js, 供 shadow_dev 等使用) --
set NPM_GLOBAL=%APPDATA%\npm
if exist "%NPM_GLOBAL%" (
    echo %PATH% | findstr /i "%NPM_GLOBAL%" >nul 2>&1
    if errorlevel 1 set "PATH=%NPM_GLOBAL%;%PATH%"
)
if exist "C:\Program Files\nodejs" (
    echo %PATH% | findstr /i "nodejs" >nul 2>&1
    if errorlevel 1 set "PATH=C:\Program Files\nodejs;%PATH%"
)

REM -- Step 6: Launch --
echo [step] 6/6 Starting Station Director...
echo.
echo ========================================
echo  LAN Mesh Station Director
echo  Web UI: http://localhost:45470
echo  Secretary: not active ^(--click "Set as Master" in Web UI--^)
echo ========================================
echo.
echo  [TIP] If Windows Firewall pops up, click "Allow"
echo        to enable LAN discovery and Web UI access.
echo.

REM Launch Station Director
%PYTHON% main.py station --port 45470
pause
