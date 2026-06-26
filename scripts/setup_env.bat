@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo  LAN Mesh 环境初始化
echo ========================================
echo.

cd /d "%~dp0.."

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 python, 请确认已安装 Python 3.11+
    pause
    exit /b 1
)

REM 创建虚拟环境
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境...
    python -m venv .venv
    echo       已创建 .venv/
) else (
    echo [1/3] 虚拟环境已存在, 跳过
)

REM 安装依赖
echo [2/3] 安装 Python 依赖...
.venv\Scripts\pip.exe install -r requirements.txt -q
echo       依赖安装完成

REM 复制模型池模板
if exist "lan_mesh\model_pool.example.yaml" (
    if not exist "lan_mesh\model_pool.yaml" (
        echo [3/3] 复制模型池配置模板...
        copy "lan_mesh\model_pool.example.yaml" "lan_mesh\model_pool.yaml" >nul
        echo       已创建 lan_mesh/model_pool.yaml (请编辑填入 API Key^)
    ) else (
        echo [3/3] model_pool.yaml 已存在, 跳过
    )
) else (
    echo [3/3] model_pool.example.yaml 不存在, 跳过
)

echo.
echo ========================================
echo  环境初始化完成!
echo.
echo  下一步:
echo    1. 编辑 lan_mesh\model_pool.yaml 配置 API Key
echo    2. 设置环境变量 (可选^):
echo       set DEEPSEEK_API_KEY=sk-xxx
echo       set OPENAI_API_KEY=sk-xxx
echo       set ALIYUN_TOKENPLAN_API_KEY=你的TokenPlan专属Key
echo    3. 运行 scripts\start_secretary.bat 启动 Secretary
echo    4. 运行 scripts\start_worker.bat 启动 Worker
echo ========================================
pause
