@echo off
chcp 65001 >nul 2>&1
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

REM ── Phase 2: Start ──
echo.
echo [restart] Phase 2/2: Start workstation...
echo.

if not exist "scripts\start_workstation.bat" (
    echo   [ERROR] scripts\start_workstation.bat not found
    pause
    exit /b 1
)

call scripts\start_workstation.bat
