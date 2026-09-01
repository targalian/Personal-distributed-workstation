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
    pause
    exit /b 0
)

echo   -^> Process alive ^(PID %STATION_PID%^)

REM ── Step 3: Kill process tree ──
echo [stop] 3/3 Kill process tree...
taskkill /PID %STATION_PID% /F /T >nul 2>&1
echo   -^> Termination signal sent ^(PID %STATION_PID% + children^)

REM Wait for port release
set /a WAIT_COUNT=0
:wait_loop
timeout /t 1 /nobreak >nul
set /a WAIT_COUNT+=1
if %WAIT_COUNT% GEQ 15 goto :wait_done
netstat -ano 2>nul | findstr "LISTENING" | findstr ":%STATION_PORT% " >nul
if not errorlevel 1 goto :wait_loop
:wait_done

if %WAIT_COUNT% LSS 15 (
    echo   -^> Port %STATION_PORT% released
) else (
    echo   -^> Port %STATION_PORT% may still be in use ^(timeout^)
)

REM Cleanup lock file
del /f "%LOCK_FILE%" >nul 2>&1
echo   -^> Lock file cleaned

echo.
echo ========================================
echo  Workstation stopped
echo ========================================
echo.
pause
exit /b 0

:fallback_netstat
REM Fallback: find and kill process on default port 45470
set "FOUND_PID="
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":45470 "') do set "FOUND_PID=%%p"

if "%FOUND_PID%"=="" (
    echo   -^> No workstation process found on port 45470
    echo   Workstation is not running
    pause
    exit /b 0
)

echo   -^> Found process PID %FOUND_PID% on port 45470
taskkill /PID %FOUND_PID% /F /T >nul 2>&1
echo   -^> Termination signal sent
pause
exit /b 0
