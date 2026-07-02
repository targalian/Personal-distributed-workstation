@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0.."

set PORT=45470
set NAME=
set CONFIG=
set SHARED=

REM 解析参数
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--port" (set PORT=%~2) & shift & shift & goto :parse_args
if /i "%~1"=="-p" (set PORT=%~2) & shift & shift & goto :parse_args
if /i "%~1"=="--name" (set NAME=%~2) & shift & shift & goto :parse_args
if /i "%~1"=="-n" (set NAME=%~2) & shift & shift & goto :parse_args
if /i "%~1"=="--config" (set CONFIG=%~2) & shift & shift & goto :parse_args
if /i "%~1"=="-c" (set CONFIG=%~2) & shift & shift & goto :parse_args
if /i "%~1"=="--shared" (set SHARED=%~2) & shift & shift & goto :parse_args
shift
goto :parse_args
:args_done

REM 选择 Python
set PYTHON=python
if exist ".venv\Scripts\python.exe" (
    echo [INFO] 使用虚拟环境 Python...
    set PYTHON=.venv\Scripts\python.exe
)

echo.
echo ========================================
echo  LAN Mesh Secretary Node
echo  Port: %PORT%
if defined NAME echo  Name: %NAME%
echo ========================================
echo.

REM 构建命令
set CMD=%PYTHON% main.py secretary --port %PORT%
if defined NAME set CMD=%CMD% --name "%NAME%"
if defined CONFIG set CMD=%CMD% --config "%CONFIG%"
if defined SHARED set CMD=%CMD% --shared "%SHARED%"

%CMD%
pause
