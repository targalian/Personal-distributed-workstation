# start_station.ps1 - 启动 LAN Mesh Station Director
# 用法: .\scripts\start_station.ps1 [-Port 8080] [-Name "控制中心"]

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
$venvPython = Join-Path $venv "Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Host "[INFO] 使用虚拟环境 Python..." -ForegroundColor Cyan
    $python = $venvPython
} else {
    $python = "python"
}

Set-Location $ProjectRoot

# 构建启动命令
$args_list = @("main.py", "station", "--port", $Port)
if ($Name)     { $args_list += @("--name", $Name) }
if ($Config)   { $args_list += @("--config", $Config) }
if ($Shared)   { $args_list += @("--shared", $Shared) }

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " LAN Mesh Station Director" -ForegroundColor Green
Write-Host " Port: $Port" -ForegroundColor Green
if ($Name) { Write-Host " Name: $Name" -ForegroundColor Green }
Write-Host " Secretary: 未激活 (请在 Web UI 中激活)" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

# 启动
& $python @args_list
