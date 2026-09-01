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

call scripts\stop_workstation.bat

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
