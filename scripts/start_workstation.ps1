# start_workstation.ps1 - 一键启动工作站
# 用法: .\scripts\start_workstation.ps1 [-Port 45470] [-Name "控制中心"] [-WithWorker]
# 功能: 检查环境 → 安装依赖 → 复制配置 → 启动 Station Director (+可选 Worker)

param(
    [int]$Port = 45470,
    [string]$Name = "",
    [string]$Config = "",
    [string]$Shared = "",
    [switch]$WithWorker,
    [int]$WorkerPort = 45460
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

function Write-Step([string]$msg) { Write-Host "[step] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  -> $msg" -ForegroundColor Green }
function Write-Skip([string]$msg) { Write-Host "  -> $msg (skip)" -ForegroundColor DarkGray }
function Write-Warn([string]$msg) { Write-Host "  -> $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " LAN Mesh Work Station - One-Click Start" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: 检查 Python ──
Write-Step "1/5 检查 Python..."
$sysPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $sysPython) {
    Write-Host "[ERROR] 未找到 python, 请安装 Python 3.11+ 并加入 PATH" -ForegroundColor Red
    Write-Host "  下载: https://www.python.org/downloads/" -ForegroundColor DarkGray
    exit 1
}
$pyVer = (& python --version 2>&1)
Write-Ok "Python: $pyVer"

# ── Step 2: 虚拟环境 ──
Write-Step "2/5 检查虚拟环境..."
$venv = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPip = Join-Path $venv "Scripts\pip.exe"

if (-not (Test-Path $venvPython)) {
    if (Test-Path $venv) {
        Write-Warn ".venv 存在但不是 Windows 格式, 重建..."
        Remove-Item $venv -Recurse -Force
    }
    Write-Host "  创建虚拟环境..." -ForegroundColor Yellow
    & python -m venv .venv
    if (-not (Test-Path $venvPython)) {
        Write-Host "[ERROR] 虚拟环境创建失败" -ForegroundColor Red
        exit 1
    }
    Write-Ok "已创建 .venv/"
} else {
    Write-Skip "虚拟环境已存在"
}
$python = $venvPython

# ── Step 3: 依赖安装 ──
Write-Step "3/5 检查依赖..."
$needInstall = $false
$reqFile = Join-Path $ProjectRoot "requirements.txt"
if (-not (Test-Path $reqFile)) {
    Write-Skip "requirements.txt 不存在, 跳过"
} else {
    # 快速检查: fastapi 是否已安装
    $check = & $venvPip show fastapi 2>&1
    if ($LASTEXITCODE -ne 0) {
        $needInstall = $true
    }
    if ($needInstall) {
        Write-Host "  安装依赖 (首次启动可能需要 1-2 分钟)..." -ForegroundColor Yellow
        & $venvPip install -r $reqFile -q 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] 依赖安装失败, 请手动运行: .venv\Scripts\pip install -r requirements.txt" -ForegroundColor Red
            exit 1
        }
        Write-Ok "依赖安装完成"
    } else {
        Write-Skip "依赖已安装"
    }
}

# ── Step 4: 配置文件 ──
Write-Step "4/5 检查配置..."
$poolSrc = Join-Path $ProjectRoot "lan_mesh\model_pool.example.yaml"
$poolDst = Join-Path $ProjectRoot "lan_mesh\model_pool.yaml"
if ((Test-Path $poolSrc) -and -not (Test-Path $poolDst)) {
    Copy-Item $poolSrc $poolDst
    Write-Ok "已创建 lan_mesh/model_pool.yaml (请编辑填入 API Key)"
} elseif (Test-Path $poolDst) {
    Write-Skip "model_pool.yaml 已存在"
} else {
    Write-Skip "model_pool.example.yaml 不存在"
}

# 检查环境变量
$hasKey = $false
foreach ($keyName in @("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ALIYUN_TOKENPLAN_API_KEY", "QWEN_API_KEY")) {
    $val = [Environment]::GetEnvironmentVariable($keyName, "Process")
    if ($val) { $hasKey = $true; break }
}
if (-not $hasKey) {
    Write-Warn "未检测到 LLM API Key 环境变量, 秘书对话将无法使用 LLM"
    Write-Host "  设置方法: `$env:DEEPSEEK_API_KEY = 'sk-xxx'" -ForegroundColor DarkGray
}

# ── Step 5: 启动 ──
Write-Step "5/5 启动 Station Director..."
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " LAN Mesh Station Director" -ForegroundColor Green
Write-Host " Port: $Port" -ForegroundColor Green
Write-Host " Web UI: http://localhost:$Port" -ForegroundColor Green
if ($Name) { Write-Host " Name: $Name" -ForegroundColor Green }
Write-Host " Secretary: 未激活 (启动后在 Web UI 中点击「设为主节点」)" -ForegroundColor Yellow
if ($WithWorker) {
    Write-Host " Local Worker: http://localhost:$WorkerPort (后台启动)" -ForegroundColor Green
}
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

# 构建 Station 启动参数
$stationArgs = @("main.py", "station", "--port", $Port)
if ($Name)   { $stationArgs += @("--name", $Name) }
if ($Config) { $stationArgs += @("--config", $Config) }
if ($Shared) { $stationArgs += @("--shared", $Shared) }

# 可选: 后台启动本地 Worker
if ($WithWorker) {
    $workerArgs = @("main.py", "worker", "--port", $WorkerPort)
    if ($Name) { $workerArgs += @("--name", "$Name-Worker") }
    Write-Host "[INFO] 后台启动本地 Worker (port $WorkerPort)..." -ForegroundColor DarkGray
    Start-Process -FilePath $python -ArgumentList $workerArgs -WorkingDirectory $ProjectRoot -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

# 前台启动 Station Director
& $python @stationArgs
