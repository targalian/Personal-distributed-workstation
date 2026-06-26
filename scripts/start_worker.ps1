# start_worker.ps1 - 启动 LAN Mesh Worker 节点
# 用法: .\scripts\start_worker.ps1 [-Port 45460] [-Name "计算节点-01"]

param(
    [int]$Port = 45460,
    [string]$Name = "",
    [string]$Config = "",
    [string]$Shared = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# 检查 Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[ERROR] 未找到 python, 请确认已安装 Python 3.11+" -ForegroundColor Red
    exit 1
}

# 检查虚拟环境
$venv = Join-Path $ProjectRoot ".venv"
if (Test-Path (Join-Path $venv "Scripts\Activate.ps1")) {
    Write-Host "[INFO] 激活虚拟环境..." -ForegroundColor Cyan
    & (Join-Path $venv "Scripts\Activate.ps1")
}

Set-Location $ProjectRoot

# 构建启动命令
$args_list = @("main.py", "worker", "--port", $Port)
if ($Name)     { $args_list += @("--name", $Name) }
if ($Config)   { $args_list += @("--config", $Config) }
if ($Shared)   { $args_list += @("--shared", $Shared) }

Write-Host ""
Write-Host "========================================" -ForegroundColor Yellow
Write-Host " LAN Mesh Worker Node" -ForegroundColor Yellow
Write-Host " Port: $Port" -ForegroundColor Yellow
if ($Name) { Write-Host " Name: $Name" -ForegroundColor Yellow }
Write-Host "========================================" -ForegroundColor Yellow
Write-Host ""

# 启动
python @args_list
