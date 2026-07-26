@echo off
REM ═══════════════════════════════════════════════════════════
REM  Loop Engineering - 每日验证循环 (Windows)
REM
REM  用法:
REM    daily_loop.bat              每日增量 (复测已修复项)
REM    daily_loop.bat --full       全量扫描
REM    daily_loop.bat --retest BUG-001 BUG-004
REM ═══════════════════════════════════════════════════════════

cd /d "%~dp0.."

REM 检查虚拟环境
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

echo.
echo  ========================================
echo   LAN Mesh Loop Engineering
echo   %date% %time%
echo  ========================================
echo.

%PYTHON% test_bug/run_loop.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [!] 存在失败项或回归, 请检查报告。
    echo.
) else (
    echo.
    echo  [OK] 全部通过, 无回归。
    echo.
)

pause
