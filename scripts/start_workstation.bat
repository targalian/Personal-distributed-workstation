@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM Keep a parent shell alive even if a child batch command or syntax error exits.
if /i "%~1"=="--run" goto :run
cmd.exe /d /c call "%~f0" --run "%~1"
set "START_RC=%ERRORLEVEL%"
if not "%START_RC%"=="0" echo [ERROR] Workstation startup failed ^(exit %START_RC%^). See the error above.
if not "%LANMESH_NO_PAUSE%"=="1" pause
exit /b %START_RC%

:run
cd /d "%~dp0.."
if errorlevel 1 exit /b 1
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHON=.venv\Scripts\python.exe"

REM -- Read-only diagnostic mode: no installs, config writes, CLI probes or server --
if /i "%~2"=="--check" goto :check_only

echo.
echo ========================================
echo  LAN Mesh Work Station - One-Click Start
echo ========================================
echo.

REM -- Step 1: Check Python 3.11+ --
echo [step] 1/6 Check Python...
python -c "import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11+ is required. Install it and add it to PATH.
    exit /b 1
)

REM -- Step 2: Create or repair the project virtual environment in place --
echo [step] 2/6 Check venv...
if not exist "%PYTHON%" (
    echo Creating or repairing .venv without deleting existing files...
    python -m venv .venv
    if errorlevel 1 exit /b 1
)
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] The project venv needs Python 3.11+. Recreate it with a supported interpreter.
    exit /b 1
)

REM -- Step 3: Verify all requirements, including newly added framework dependencies --
echo [step] 3/6 Check dependencies...
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m ensurepip --upgrade
    if errorlevel 1 exit /b 1
)
"%PYTHON%" scripts\check_runtime_dependencies.py
if errorlevel 1 (
    echo Installing or updating runtime dependencies...
    "%PYTHON%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [WARN] Mirror install failed. Retrying default package index...
        "%PYTHON%" -m pip install -r requirements.txt
        if errorlevel 1 exit /b 1
    )
    "%PYTHON%" scripts\check_runtime_dependencies.py
    if errorlevel 1 exit /b 1
)

REM -- Step 4: Config; main.py already loads .env via python-dotenv --
echo [step] 4/6 Check config...
if exist "lan_mesh\model_pool.example.yaml" (
    if not exist "lan_mesh\model_pool.yaml" (
        copy "lan_mesh\model_pool.example.yaml" "lan_mesh\model_pool.yaml" >nul
        if errorlevel 1 exit /b 1
        echo Created lan_mesh\model_pool.yaml. Configure your model credentials there or in .env.
    )
)
if exist ".env" echo .env will be loaded by Python at startup.

REM -- Step 5: Read Git Hook configuration only; startup must not change git config --
echo [step] 5/6 Check Git Hooks...
git config --get core.hooksPath 2>nul
if errorlevel 1 echo [INFO] No custom Git Hooks path configured.

REM -- CLI Agent probes: npm commands may be .cmd files and must use CALL --
set "NPM_GLOBAL=%APPDATA%\npm"
if exist "%NPM_GLOBAL%" set "PATH=%NPM_GLOBAL%;%PATH%"
if exist "C:\Program Files\nodejs" set "PATH=C:\Program Files\nodejs;%PATH%"
echo [step] Check optional CLI Agents...
call claude --version >nul 2>&1
if errorlevel 1 echo [INFO] claude is unavailable; configure a backend when needed.
call codex --version >nul 2>&1
if errorlevel 1 echo [INFO] codex is unavailable; configure a backend when needed.
REM Optional CLI tools are installed separately; installing aider here can overwrite runtime dependencies.

REM -- Step 6: Validate imports, then launch --
echo [step] 6/6 Starting Station Director...
"%PYTHON%" -c "from lan_mesh.station_controller import StationController; print('[Preflight] Station import OK')"
if errorlevel 1 exit /b 1
echo.
echo Web UI: http://localhost:45470
echo Allow Windows Firewall access if you want LAN discovery.
echo.
"%PYTHON%" main.py station --port 45470
exit /b %ERRORLEVEL%

:check_only
if not exist "%PYTHON%" (
    echo [ERROR] Project venv not found. Run this script without --check to prepare it.
    exit /b 1
)
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 exit /b 1
"%PYTHON%" scripts\check_runtime_dependencies.py
if errorlevel 1 exit /b 1
"%PYTHON%" -c "from lan_mesh.station_controller import StationController; print('[Preflight] Station import OK')"
exit /b %ERRORLEVEL%
