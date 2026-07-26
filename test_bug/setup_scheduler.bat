@echo off
REM ═══════════════════════════════════════════════════════════
REM  LAN Mesh Loop Engineering - Windows 任务计划注册
REM
REM  功能:
REM    1. 注册每日凌晨 3:00 自动巡检任务
REM    2. 注册开机自启动 Station Director 任务
REM
REM  用法 (需要管理员权限):
REM    右键 → 以管理员身份运行
REM
REM  卸载:
REM    setup_scheduler.bat /uninstall
REM ═══════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

REM ── 配置 ──
set PROJECT_DIR=%~dp0..
set VENV_PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe
set FALLBACK_PYTHON=python
set TASK_NAME_NIGHTLY=LAN_Mesh_Loop_Engineering
set TASK_NAME_AUTOSTART=LAN_Mesh_Station_AutoStart

REM 检查 Python
if exist "%VENV_PYTHON%" (
    set PYTHON=%VENV_PYTHON%
) else (
    set PYTHON=%FALLBACK_PYTHON%
)

REM 转为绝对路径
cd /d "%PROJECT_DIR%"
set PROJECT_DIR=%CD%

echo.
echo  ========================================
echo   LAN Mesh - Task Scheduler Setup
echo   Project: %PROJECT_DIR%
echo   Python:  %PYTHON%
echo  ========================================
echo.

REM ── 卸载模式 ──
if "%1"=="/uninstall" (
    echo  [Uninstall] Removing scheduled tasks...
    schtasks /delete /tn "%TASK_NAME_NIGHTLY%" /f 2>nul
    schtasks /delete /tn "%TASK_NAME_AUTOSTART%" /f 2>nul
    echo  [Done] Tasks removed.
    pause
    exit /b 0
)

REM ── 检查管理员权限 ──
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  [WARNING] Not running as Administrator.
    echo  Some operations may fail. Right-click and
    echo  "Run as administrator" for best results.
    echo.
    pause
)

REM ═══════════════════════════════════════════════════════════
REM  Task 1: 每日凌晨 3:00 自动巡检
REM ═══════════════════════════════════════════════════════════

echo  [1/2] Registering nightly loop task (daily at 03:00)...

REM 先删除旧任务 (如果存在)
schtasks /delete /tn "%TASK_NAME_NIGHTLY%" /f 2>nul

schtasks /create ^
    /tn "%TASK_NAME_NIGHTLY%" ^
    /tr "\"%PYTHON%\" \"%PROJECT_DIR%\test_bug\nightly_loop.py\"" ^
    /sc daily ^
    /st 03:00 ^
    /rl HIGHEST ^
    /f

if %ERRORLEVEL% EQU 0 (
    echo        OK: %TASK_NAME_NIGHTLY% registered.
) else (
    echo        FAILED: Could not register nightly task.
)

REM ═══════════════════════════════════════════════════════════
REM  Task 2: 开机自启动 Station Director
REM ═══════════════════════════════════════════════════════════

echo.
echo  [2/2] Registering auto-start task (at user logon)...

REM 先删除旧任务
schtasks /delete /tn "%TASK_NAME_AUTOSTART%" /f 2>nul

schtasks /create ^
    /tn "%TASK_NAME_AUTOSTART%" ^
    /tr "\"%PYTHON%\" \"%PROJECT_DIR%\main.py\" station" ^
    /sc onlogon ^
    /rl HIGHEST ^
    /f

if %ERRORLEVEL% EQU 0 (
    echo        OK: %TASK_NAME_AUTOSTART% registered.
) else (
    echo        FAILED: Could not register auto-start task.
)

REM ═══════════════════════════════════════════════════════════
REM  完成
REM ═══════════════════════════════════════════════════════════

echo.
echo  ========================================
echo   Setup Complete!
echo  ========================================
echo.
echo   Registered tasks:
echo     1. %TASK_NAME_NIGHTLY%
echo        Schedule: Daily at 03:00
echo        Action: Run full loop test + report
echo.
echo     2. %TASK_NAME_AUTOSTART%
echo        Schedule: At user logon
echo        Action: Start Station Director
echo.
echo   Management:
echo     View:    schtasks /query /tn "%TASK_NAME_NIGHTLY%"
echo     Run now: schtasks /run /tn "%TASK_NAME_NIGHTLY%"
echo     Remove:  setup_scheduler.bat /uninstall
echo.
echo   Logs: %PROJECT_DIR%\test_bug\logs\
echo   Reports: %PROJECT_DIR%\test_bug\reports\
echo.

pause
