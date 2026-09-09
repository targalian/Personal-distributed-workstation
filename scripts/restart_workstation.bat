@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0.."

REM ========================================
REM  LAN Mesh Work Station - Restart
REM ========================================

echo.
echo ========================================
echo  LAN Mesh Work Station - Restart
echo ========================================
echo.

REM ── Phase 1: Stop ──
echo [restart] Phase 1/2: Stop current workstation...
echo.

if not exist "scripts\stop_workstation.bat" (
    echo   [ERROR] scripts\stop_workstation.bat not found
    pause
    exit /b 1
)

REM 让 stop 阶段不要停在 pause 上 (restart 是无人值守串联)
set "LANMESH_NO_PAUSE=1"
call scripts\stop_workstation.bat
set "STOP_RC=%ERRORLEVEL%"
set "LANMESH_NO_PAUSE="

REM 停止失败 (端口仍被占) 时必须中断: 否则 start 会因端口冲突失败,
REM 表现为「重启脚本跑完了但服务还是旧进程」这种静默失败
if not "%STOP_RC%"=="0" (
    echo.
    echo   [ERROR] Stop phase failed ^(exit %STOP_RC%^), aborting restart
    echo           Old process is still running -- resolve it before retrying.
    pause
    exit /b 1
)

echo.
echo [restart] Waiting 3 seconds for port release...
timeout /t 3 /nobreak >nul

REM ── Phase 2: Start (直接启动, 不依赖 start_workstation.bat) ──
REM start_workstation.bat 有 setlocal + 复杂环境检查, 通过 call 串联时
REM 变量作用域和 pause 控制不可靠. 重启时 venv/依赖已就绪, 直接拉起即可.
echo.
echo [restart] Phase 2/2: Start workstation...
echo.

REM -- 检查 venv python 存在 --
if not exist ".venv\Scripts\python.exe" (
    echo   [ERROR] .venv\Scripts\python.exe not found
    echo           Run scripts\start_workstation.bat first to set up environment.
    pause
    exit /b 1
)
set "PYTHON=.venv\Scripts\python.exe"

REM -- 加载 .env (如存在, 注入 API Key 等环境变量) --
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%k in (".env") do (
        if not "%%k"=="" (
            set "_LINE=%%k"
            if not "!_LINE:~0,1!"=="#" (
                if not "%%l"=="" (
                    if not defined %%k set "%%k=%%l"
                )
            )
        )
    )
)

REM -- CLI Agent PATH (npm global + Node.js) --
REM 注意: 不能用 echo %%PATH%% | findstr, PATH 含括号会炸 if 块.
REM 直接无条件追加 (重复无害, 重启时 PATH 已在初始启动中设好).
set "NPM_GLOBAL=%APPDATA%\npm"
if exist "%NPM_GLOBAL%" set "PATH=%NPM_GLOBAL%;%PATH%"
if exist "C:\Program Files\nodejs" set "PATH=C:\Program Files\nodejs;%PATH%"

echo.
echo ========================================
echo  LAN Mesh Station Director ^(Restart^)
echo  Web UI: http://localhost:45470
echo ========================================
echo.

REM -- 直接前台启动, 不经过 pause --
%PYTHON% main.py station --port 45470
