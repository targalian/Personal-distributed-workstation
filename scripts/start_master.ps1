# start_master.ps1 - 启动 LAN Mesh Master 节点
# 用法: .\scripts\start_master.ps1 [-Port 8080] [-Name "控制中心"]

param(
    [int]$Port = 45470,
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
$args_list = @("main.py", "master", "--port", $Port)
if ($Name)     { $args_list += @("--name", $Name) }
if ($Config)   { $args_list += @("--config", $Config) }
if ($Shared)   { $args_list += @("--shared", $Shared) }

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " LAN Mesh Master Node" -ForegroundColor Green
Write-Host " Port: $Port" -ForegroundColor Green
if ($Name) { Write-Host " Name: $Name" -ForegroundColor Green }
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

# 启动
python @args_list
