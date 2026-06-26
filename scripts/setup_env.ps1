# setup_env.ps1 - 初始化项目环境
# 用法: .\scripts\setup_env.ps1
# 功能: 创建虚拟环境、安装依赖、复制模型池配置模板

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " LAN Mesh 环境初始化" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. 创建虚拟环境
$venv = Join-Path $ProjectRoot ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "[1/3] 创建虚拟环境..." -ForegroundColor Yellow
    python -m venv .venv
    Write-Host "      已创建 .venv/" -ForegroundColor Green
} else {
    Write-Host "[1/3] 虚拟环境已存在, 跳过" -ForegroundColor DarkGray
}

# 激活
& (Join-Path $venv "Scripts\Activate.ps1")

# 2. 安装依赖
Write-Host "[2/3] 安装 Python 依赖..." -ForegroundColor Yellow
pip install -r requirements.txt -q
Write-Host "      依赖安装完成" -ForegroundColor Green

# 3. 复制模型池模板
$pool_src = Join-Path $ProjectRoot "lan_mesh\model_pool.example.yaml"
$pool_dst = Join-Path $ProjectRoot "lan_mesh\model_pool.yaml"
if ((Test-Path $pool_src) -and -not (Test-Path $pool_dst)) {
    Write-Host "[3/3] 复制模型池配置模板..." -ForegroundColor Yellow
    Copy-Item $pool_src $pool_dst
    Write-Host "      已创建 lan_mesh/model_pool.yaml (请编辑填入 API Key)" -ForegroundColor Green
} elseif (Test-Path $pool_dst) {
    Write-Host "[3/3] model_pool.yaml 已存在, 跳过" -ForegroundColor DarkGray
} else {
    Write-Host "[3/3] model_pool.example.yaml 不存在, 跳过" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " 环境初始化完成!" -ForegroundColor Green
Write-Host ""
Write-Host " 下一步:" -ForegroundColor Green
Write-Host "   1. 编辑 lan_mesh/model_pool.yaml 配置 API Key" -ForegroundColor White
Write-Host "   2. 设置环境变量 (可选):" -ForegroundColor White
Write-Host '      $env:DEEPSEEK_API_KEY = "sk-xxx"' -ForegroundColor DarkGray
Write-Host '      $env:OPENAI_API_KEY  = "sk-xxx"' -ForegroundColor DarkGray
Write-Host "   3. 启动 Secretary: .\scripts\start_secretary.ps1" -ForegroundColor White
Write-Host "   4. 启动 Worker: .\scripts\start_worker.ps1" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Green
