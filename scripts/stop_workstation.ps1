# stop_workstation.ps1 - 关闭当前工作站 (Station Director + 附属 Worker)
# 用法: .\scripts\stop_workstation.ps1 [-Force] [-Timeout 15]
# 功能: 读取锁文件 → 确认进程 → 终止进程树 → 等待端口释放 → 清理锁文件

param(
    [switch]$Force,
    [int]$Timeout = 15
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# ── 设置 UTF-8 环境 ──
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Step([string]$msg) { Write-Host "[stop] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  -> $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  -> $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " LAN Mesh Work Station - Stop" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: 读取锁文件 ──
Write-Step "1/4 读取锁文件..."
$lockFile = Join-Path $env:USERPROFILE ".lan_mesh\station.lock"

if (-not (Test-Path $lockFile)) {
    Write-Warn "未找到锁文件: $lockFile"
    Write-Host "  工作站可能未运行, 尝试清理残留进程..." -ForegroundColor DarkGray

    # 兜底: 通过端口探测进程
    $defaultPort = 45470
    $holders = @()
    try {
        $netstatOut = netstat -ano 2>$null
        foreach ($line in $netstatOut) {
            if ($line -match "LISTENING" -and $line -match ":$defaultPort\s") {
                $parts = $line.Trim() -split "\s+"
                if ($parts.Count -ge 5) {
                    $pid = [int]$parts[-1]
                    if ($pid -gt 0) { $holders += $pid }
                }
            }
        }
    } catch {}

    if ($holders.Count -eq 0) {
        Write-Ok "无运行中的工作站进程"
        exit 0
    }

    Write-Warn "发现端口 $defaultPort 占用进程: PID $($holders -join ', ')"
    foreach ($pid in $holders) {
        try {
            taskkill /PID $pid /F /T 2>$null | Out-Null
            Write-Ok "已终止进程 PID $pid"
        } catch {
            Write-Warn "终止 PID $pid 失败"
        }
    }
    exit 0
}

try {
    $lockJson = Get-Content $lockFile -Encoding UTF8 -Raw | ConvertFrom-Json
    $stationPid = [int]$lockJson.pid
    $stationPort = [int]$lockJson.port
} catch {
    Write-Err "锁文件解析失败: $_"
    exit 1
}

if (-not $stationPid -or $stationPid -le 0) {
    Write-Err "锁文件中 PID 无效: $stationPid"
    exit 1
}

Write-Ok "Station PID: $stationPid, Port: $stationPort"

# ── Step 2: 确认进程存活 ──
Write-Step "2/4 确认进程状态..."
$alive = $false
try {
    $proc = Get-Process -Id $stationPid -ErrorAction SilentlyContinue
    if ($proc) { $alive = $true }
} catch {}

if (-not $alive) {
    Write-Warn "进程 PID $stationPid 已不存在 (僵尸锁)"
    # 清理锁文件
    try { Remove-Item $lockFile -Force -ErrorAction SilentlyContinue } catch {}
    Write-Ok "已清理僵尸锁文件"
    exit 0
}

$procName = $proc.ProcessName
$procCmd  = ""
try { $procCmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$stationPid" -ErrorAction SilentlyContinue).CommandLine } catch {}

Write-Ok "进程存活: $procName (PID $stationPid)"
if ($procCmd) { Write-Host "  cmd: $procCmd" -ForegroundColor DarkGray }

# ── Step 3: 确认关闭 (非 Force 模式) ──
if (-not $Force) {
    Write-Host ""
    $confirm = Read-Host "  确认关闭工作站 (PID $stationPid, Port $stationPort)? [y/N]"
    if ($confirm -notmatch "^[yY]") {
        Write-Host "  已取消" -ForegroundColor Yellow
        exit 0
    }
}

# ── Step 4: 终止进程树 ──
Write-Step "3/4 终止进程树..."
try {
    # taskkill /T 终止整个进程树 (含 Worker 子进程)
    $result = taskkill /PID $stationPid /F /T 2>&1
    Write-Ok "已发送终止信号 (PID $stationPid + 子进程)"
} catch {
    Write-Warn "taskkill 失败: $_, 尝试 Stop-Process..."
    try { Stop-Process -Id $stationPid -Force -ErrorAction SilentlyContinue } catch {}
}

# 等待端口释放
Write-Step "4/4 等待端口释放..."
$deadline = (Get-Date).AddSeconds($Timeout)
$portFree = $false
while ((Get-Date) -lt $deadline) {
    $stillListening = $false
    try {
        $netstatOut = netstat -ano 2>$null
        foreach ($line in $netstatOut) {
            if ($line -match "LISTENING" -and $line -match ":$stationPort\s") {
                $stillListening = $true
                break
            }
        }
    } catch {}
    if (-not $stillListening) {
        $portFree = $true
        break
    }
    Start-Sleep -Milliseconds 300
}

if ($portFree) {
    Write-Ok "端口 $stationPort 已释放"
} else {
    Write-Warn "端口 $stationPort 在 ${Timeout}s 内未释放 (进程可能仍在退出)"
}

# 清理锁文件 (进程正常退出时 atexit 会清理, 这里兜底)
try {
    if (Test-Path $lockFile) {
        Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
        Write-Ok "已清理锁文件"
    }
} catch {}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " 工作站已关闭" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
