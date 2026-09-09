@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

REM ========================================
REM  LAN Mesh Work Station - Stop
REM ========================================

echo.
echo ========================================
echo  LAN Mesh Work Station - Stop
echo ========================================
echo.

REM ── Step 1: Read lock file ──
echo [stop] 1/3 Read lock file...
set "LOCK_FILE=%USERPROFILE%\.lan_mesh\station.lock"

if not exist "%LOCK_FILE%" (
    echo   -^> Lock file not found, workstation may not be running
    echo   Trying netstat fallback on port 45470...
    goto :fallback_netstat
)

REM Parse JSON with PowerShell
for /f "usebackq delims=" %%j in (`powershell -NoProfile -Command "Get-Content '%LOCK_FILE%' -Raw | ConvertFrom-Json | Select-Object -ExpandProperty pid"`) do set "STATION_PID=%%j"
for /f "usebackq delims=" %%j in (`powershell -NoProfile -Command "Get-Content '%LOCK_FILE%' -Raw | ConvertFrom-Json | Select-Object -ExpandProperty port"`) do set "STATION_PORT=%%j"

if "%STATION_PID%"=="" (
    echo   [ERROR] Failed to parse lock file
    pause
    exit /b 1
)

echo   -^> Station PID: %STATION_PID%, Port: %STATION_PORT%

REM ── Step 2: Check process alive ──
echo [stop] 2/3 Check process...
tasklist /FI "PID eq %STATION_PID%" /NH 2>nul | findstr "%STATION_PID%" >nul
if errorlevel 1 (
    echo   -^> PID %STATION_PID% not running ^(stale lock^)
    del /f "%LOCK_FILE%" >nul 2>&1
    echo   -^> Stale lock cleaned
    REM 锁虽僵尸, 端口仍可能被占 (旧进程换了锁/跨工作区启动) — 继续兜底清理
    netstat -ano 2>nul | findstr "LISTENING" | findstr ":45470" >nul
    if not errorlevel 1 (
        echo   -^> Port 45470 still occupied, falling back to netstat cleanup
        goto :fallback_netstat
    )
    if not "%LANMESH_NO_PAUSE%"=="1" pause
    exit /b 0
)

echo   -^> Process alive ^(PID %STATION_PID%^)

REM ── Step 3: Kill process tree ──
echo [stop] 3/3 Kill process tree...
taskkill /PID %STATION_PID% /F /T >nul 2>&1
echo   -^> taskkill sent ^(PID %STATION_PID% + children^)

REM Wait for port release
set /a WAIT_COUNT=0
:wait_loop
timeout /t 1 /nobreak >nul
set /a WAIT_COUNT+=1
if %WAIT_COUNT% GEQ 15 goto :wait_done
netstat -ano 2>nul | findstr "LISTENING" | findstr ":%STATION_PORT% " >nul
if not errorlevel 1 goto :wait_loop
:wait_done

REM wmic fallback if taskkill failed and port still occupied
netstat -ano 2>nul | findstr "LISTENING" | findstr ":%STATION_PORT% " >nul
if not errorlevel 1 (
    echo   -^> taskkill may have failed, trying wmic...
    wmic process where "ProcessId=%STATION_PID%" delete >nul 2>&1
    timeout /t 3 /nobreak >nul
)

if %WAIT_COUNT% LSS 15 (
    echo   -^> Port %STATION_PORT% released
) else (
    REM final check after wmic
    netstat -ano 2>nul | findstr "LISTENING" | findstr ":%STATION_PORT% " >nul
    if errorlevel 1 (
        echo   -^> Port %STATION_PORT% released ^(via wmic^)
    ) else (
        echo   -^> Port %STATION_PORT% may still be in use ^(timeout^)
    )
)

REM Cleanup lock file
del /f "%LOCK_FILE%" >nul 2>&1
echo   -^> Lock file cleaned

echo.
echo ========================================
echo  Workstation stopped
echo ========================================
echo.
if not "%LANMESH_NO_PAUSE%"=="1" pause
exit /b 0

:fallback_netstat
REM Fallback: find and kill process listening on default port 45470
REM 注意: 锁文件缺失时走此分支 (进程被强杀过 / 跨工作区启动 / 锁被清理)
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":45470"') do set "FOUND_PID=%%p"

if "%FOUND_PID%"=="" (
    echo   -^> No workstation process found on port 45470
    echo   Workstation is not running
    goto :fallback_done
)

echo   -^> Found process PID %FOUND_PID% on port 45470
taskkill /PID %FOUND_PID% /F /T >nul 2>&1

REM wmic fallback if taskkill failed
netstat -ano 2>nul | findstr "LISTENING" | findstr ":45470" >nul
if not errorlevel 1 (
    echo   -^> taskkill may have failed, trying wmic...
    wmic process where "ProcessId=%FOUND_PID%" delete >nul 2>&1
)

REM 等待端口真正释放, 否则后续 start 会因端口占用启动失败
set /a FB_WAIT=0
:fb_wait_loop
timeout /t 1 /nobreak >nul
set /a FB_WAIT+=1
if %FB_WAIT% GEQ 15 goto :fb_wait_done
netstat -ano 2>nul | findstr "LISTENING" | findstr ":45470" >nul
if not errorlevel 1 goto :fb_wait_loop
:fb_wait_done

netstat -ano 2>nul | findstr "LISTENING" | findstr ":45470" >nul
if not errorlevel 1 (
    echo   [ERROR] Port 45470 still occupied by PID %FOUND_PID% after 15s
    echo           taskkill may have failed ^(insufficient privileges?^)
    echo           Run this script as Administrator, or stop it manually:
    echo             taskkill /PID %FOUND_PID% /F /T
    if not "%LANMESH_NO_PAUSE%"=="1" pause
    exit /b 1
)
echo   -^> Port 45470 released ^(PID %FOUND_PID% terminated^)

:fallback_done
del /f "%LOCK_FILE%" >nul 2>&1
if not "%LANMESH_NO_PAUSE%"=="1" pause
exit /b 0
