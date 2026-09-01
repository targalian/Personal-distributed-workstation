# restart_workstation.ps1 - 重启当前工作站 (关闭 → 等待 → 重新启动)
# 用法: .\scripts\restart_workstation.ps1 [-Port 45470] [-Name "控制中心"] [-WithWorker] [-Force]
# 功能: 复用 stop_workstation.ps1 关闭 → 复用 start_workstation.ps1 启动

param(
    [int]$Port = 0,
    [string]$Name = "",
    [string]$Config = "",
    [string]$Shared = "",
    [switch]$WithWorker,
    [int]$WorkerPort = 45460,
    [switch]$Force,
    [int]$WaitSeconds = 3
)

$ErrorActionPreference = "Stop"
$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptsDir
Set-Location $ProjectRoot

# ── 设置 UTF-8 环境 ──
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host ""
Write-Host "========================================" -ForegroundColor Magenta
Write-Host " LAN Mesh Work Station - Restart" -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta
Write-Host ""

# ── Phase 0: 在关闭前记录端口 (锁文件关闭后会被清理) ──
if ($Port -eq 0) {
    $lockFile = Join-Path $env:USERPROFILE ".lan_mesh\station.lock"
    if (Test-Path $lockFile) {
        try {
            $lockJson = Get-Content $lockFile -Encoding UTF8 -Raw | ConvertFrom-Json
            $Port = [int]$lockJson.port
        } catch {}
    }
    if ($Port -eq 0) { $Port = 45470 }
}

# ── Phase 1: 关闭现有工作站 ──
Write-Host "[restart] Phase 1/2: Stop current workstation..." -ForegroundColor Cyan
Write-Host ""

$stopScript = Join-Path $ScriptsDir "stop_workstation.ps1"
if (-not (Test-Path $stopScript)) {
    Write-Host "[ERROR] stop_workstation.ps1 未找到: $stopScript" -ForegroundColor Red
    exit 1
}

# 始终 -Force 跳过确认 (restart 本身已确认意图)
$stopArgs = @("-File", $stopScript, "-Force")

& powershell -NoProfile -ExecutionPolicy Bypass @stopArgs
$stopExit = $LASTEXITCODE

if ($stopExit -ne 0) {
    Write-Host "[WARN] stop 退出码: $stopExit (继续尝试启动)" -ForegroundColor Yellow
}

# ── Phase 2: 等待环境稳定 ──
Write-Host ""
Write-Host "[restart] Phase 2/2: Wait ${WaitSeconds}s then restart..." -ForegroundColor Cyan
Start-Sleep -Seconds $WaitSeconds

# ── Phase 3: 启动工作站 ──
Write-Host ""
$startScript = Join-Path $ScriptsDir "start_workstation.ps1"
if (-not (Test-Path $startScript)) {
    Write-Host "[ERROR] start_workstation.ps1 未找到: $startScript" -ForegroundColor Red
    exit 1
}

# 构建启动参数
$startArgs = @("-File", $startScript)
$startArgs += @("-Port", $Port)

if ($Name)        { $startArgs += @("-Name", $Name) }
if ($Config)      { $startArgs += @("-Config", $Config) }
if ($Shared)      { $startArgs += @("-Shared", $Shared) }
if ($WithWorker)  {
    $startArgs += @("-WithWorker", "-WorkerPort", $WorkerPort)
}

Write-Host "[restart] Starting: powershell $startArgs" -ForegroundColor DarkGray
Write-Host ""

& powershell -NoProfile -ExecutionPolicy Bypass @startArgs
