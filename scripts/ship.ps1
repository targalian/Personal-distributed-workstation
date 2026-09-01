<#
.SYNOPSIS
    一键发货：按 Agent 归属分批提交并推送（人在回路外时的唯一人工触点）

.DESCRIPTION
    解决「Codex 能改能验但提交不了」的权限断点。脚本代替人工完成:
      1. 前置门禁: 编译 + 未绑定引用扇扫 + 文档清单一致性 (FAIL 即中止, 不留半成品提交)
      2. 批 1 - Quest/共享: 文档、wiki、协作机制、配置示例
                （期间置 LAN_MESH_WIKI_DRY_RUN=1，避免 post-commit 拉起的
                  Quest 与批 2 抢同一批 repowiki 脏文件）
      3. 批 2 - Codex: lan_mesh 代码 + tests + docs/design
                （此时工作区已干净，hook 可安全拉起 Quest 同步 wiki）
      4. 调用 scripts/sync_push.ps1 推双仓库 (自动补 VERSION.json 同步提交)
    每批提交前打印文件清单并询问 y/n；-Yes 全自动，-DryRun 只看不做。

.PARAMETER Yes
    跳过所有确认（供定时任务/完全无人值守使用）

.PARAMETER DryRun
    只打印将要执行的动作，不碰仓库

.PARAMETER NoPush
    只提交不推送（想先本地攒几轮时用）

.PARAMETER DocsSubject
    覆盖批 1（文档/wiki）的 commit subject。缺省时由 loop_status.json
    的 current_phase 自动推导，避免写死过期迭代号。

.PARAMETER CodeSubject
    覆盖批 2（代码）的 commit subject。缺省时同样自动推导。

.EXAMPLE
    powershell -File scripts/ship.ps1            # 逐批询问 y/n
    powershell -File scripts/ship.ps1 -DryRun    # 预演
    powershell -File scripts/ship.ps1 -Yes       # 无人值守
#>
param(
    [switch]$Yes,
    [switch]$DryRun,
    [switch]$NoPush,
    [string]$DocsSubject,
    [string]$CodeSubject
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-Head($text) {
    Write-Host ""
    Write-Host ("=" * 62) -ForegroundColor Cyan
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host ("=" * 62) -ForegroundColor Cyan
}

function Confirm-Step($prompt) {
    if ($Yes) { Write-Host "  [-Yes] 自动确认: $prompt" -ForegroundColor DarkGray; return $true }
    if ($DryRun) { Write-Host "  [DryRun] 跳过确认: $prompt" -ForegroundColor DarkGray; return $false }
    $a = Read-Host "  $prompt  [y/N]"
    return ($a -eq "y" -or $a -eq "Y")
}

# 归属判定：与 AGENT_LOCKS.md 第一节 / dev_status.py 的 OWNERSHIP 保持一致
function Get-Owner($path) {
    $shared = @("AGENTS.md", "AGENT_LOCKS.md", "loop_status.json", "VERSION.json",
                ".gitignore", "lan_mesh/model_pool.example.yaml")
    if ($shared -contains $path) { return "Shared" }
    switch -Wildcard ($path) {
        ".qoder/*"      { return "Quest" }
        "docs/reference/*" { return "Quest" }
        "webui/*"       { return "Quest" }
        "quicklan-main/*" { return "Quest" }
        "lan_mesh/*"    { return "Codex" }
        "tests/*"       { return "Codex" }
        "docs/design/*" { return "Codex" }
        "test_bug/*"    { return "Codex" }
        "scripts/*"     { return "Shared" }
        ".githooks/*"   { return "Shared" }
    }
    return "Unclassified"
}

Write-Head "LAN Mesh 一键发货"

# ── 0. 环境前置检查 ──────────────────────────────────────────
$branch = (& git branch --show-current).Trim()
if ($branch -ne "master") { throw "请在 master 分支运行（当前: $branch）" }

# -z 分隔避免中文路径被转义
$raw = (& git status --porcelain -z) -join ""
$entries = $raw.Split([char]0) | Where-Object { $_.Length -gt 3 }
if (-not $entries) {
    Write-Host "工作区干净。" -ForegroundColor Green
    $ahead = & git log --oneline gitee/master..HEAD
    if ($ahead -and -not $NoPush) {
        Write-Host "存在未推送 commit:" -ForegroundColor Yellow
        $ahead | ForEach-Object { Write-Host "  $_" }
        if (Confirm-Step "直接推送到 Gitee + GitHub CN?") {
            & powershell -ExecutionPolicy Bypass -File "$root\scripts\sync_push.ps1"
        }
    } else {
        Write-Host "无待办动作。" -ForegroundColor Green
    }
    exit 0
}

# ── 1. 门禁前置自检（失败即停，不产生半成品提交）────────────
Write-Head "① 门禁自检"
$pyFiles = "['lan_mesh/pm_agent.py','lan_mesh/station_controller.py','lan_mesh/station_api.py','lan_mesh/chat_handler.py','lan_mesh/bot_gateway.py','lan_mesh/database.py','lan_mesh/worker.py','lan_mesh/api.py','lan_mesh/orchestrator.py']"
& python -c "import py_compile; files=$pyFiles; [py_compile.compile(f, doraise=True) for f in files]; print('  编译检查  : PASS')"
if ($LASTEXITCODE -ne 0) { throw "编译检查 FAIL — 修复后再发货" }

& python "$root\scripts\check_unbound_names.py"
if ($LASTEXITCODE -ne 0) {
    throw "未绑定引用 FAIL — 多为 import 被误删, 会在运行时 NameError"
}

& python "$root\scripts\sync_docs.py" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  文档清单  : FAIL" -ForegroundColor Red
    throw "docs/design 清单漂移 — 先运行: python scripts/sync_docs.py --write"
}
Write-Host "  文档清单  : PASS" -ForegroundColor Green
Write-Host "  提示: 完整回归 python -m pytest -q 约 110s, 未在本脚本内自动运行" -ForegroundColor DarkGray

# ── 2. 按归属分组 ────────────────────────────────────────────
$byOwner = @{}
foreach ($e in $entries) {
    $p = $e.Substring(3)
    $o = Get-Owner $p
    if (-not $byOwner.ContainsKey($o)) { $byOwner[$o] = @() }
    $byOwner[$o] += $p
}

$questSet = @()
foreach ($o in @("Quest", "Shared", "Unclassified")) {
    if ($byOwner.ContainsKey($o)) { $questSet += $byOwner[$o] }
}
$codexSet = @()
if ($byOwner.ContainsKey("Codex")) { $codexSet = $byOwner["Codex"] }

function Invoke-Batch($title, $files, $message, $muteHook) {
    if (-not $files -or $files.Count -eq 0) {
        Write-Host "  (本批无改动，跳过)" -ForegroundColor DarkGray
        return $false
    }
    Write-Head $title
    Write-Host "  文件 ($($files.Count) 个):"
    $files | Select-Object -First 12 | ForEach-Object { Write-Host "      $_" }
    if ($files.Count -gt 12) { Write-Host "      ... 另有 $($files.Count - 12) 个" }
    Write-Host ""
    Write-Host "  commit: $message" -ForegroundColor Yellow

    if (-not (Confirm-Step "提交这一批?")) {
        Write-Host "  已跳过。" -ForegroundColor DarkGray
        return $false
    }
    if ($muteHook) { $env:LAN_MESH_WIKI_DRY_RUN = "1" }
    try {
        # --literal-pathspecs: 中文/含特殊字符路径按字面处理
        & git --literal-pathspecs add -- $files
        if ($LASTEXITCODE -ne 0) { throw "git add 失败" }
        & git commit -m $message
        if ($LASTEXITCODE -ne 0) { throw "git commit 失败（commit-msg 钩子可能拒绝了消息格式）" }
        Write-Host "  提交完成。" -ForegroundColor Green
    } finally {
        if ($muteHook) { Remove-Item Env:\LAN_MESH_WIKI_DRY_RUN -ErrorAction SilentlyContinue }
    }
    return $true
}

# 迭代号取自 loop_status.json，避免硬编码写死过期的 iter-NN
$iter = "unknown"
try {
    $ls = Get-Content "$root\loop_status.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($ls.current_phase -match '^(iter-\d+)') {
        $iter = $Matches[1]
    } elseif ($ls.iteration_count) {
        $iter = "iter-$($ls.iteration_count)"
    }
} catch {
    Write-Host "  警告: 无法解析 loop_status.json，迭代号回退为 unknown" -ForegroundColor Yellow
}

if ($DocsSubject) { $msg1 = $DocsSubject } else { $msg1 = "chore(config): $iter 迭代状态与文档清单同步" }
if ($CodeSubject) { $msg2 = $CodeSubject } else { $msg2 = "chore(station): $iter 代码变更同步" }

Write-Host ""
Write-Host "  迭代号: $iter (来自 loop_status.json current_phase)" -ForegroundColor DarkGray
Write-Host "  批 1 subject: $msg1" -ForegroundColor DarkGray
Write-Host "  批 2 subject: $msg2" -ForegroundColor DarkGray
Write-Host "  如需更贴切的描述: -DocsSubject / -CodeSubject 覆盖" -ForegroundColor DarkGray

if ($DryRun) {
    Write-Head "DryRun 预演"
    Write-Host "  批 1 (Quest/共享): $($questSet.Count) 个文件"
    $questSet | ForEach-Object { Write-Host "      $_" }
    Write-Host "  批 2 (Codex 代码): $($codexSet.Count) 个文件"
    $codexSet | ForEach-Object { Write-Host "      $_" }
    Write-Host ""
    Write-Host "  批 1 subject: $msg1"
    Write-Host "  批 2 subject: $msg2"
    Write-Host ""
    Write-Host "  未做任何改动 (DryRun)。" -ForegroundColor Green
    exit 0
}

# 批 1 先行 + 静音 hook：避免 Quest 后台任务与批 2 抢 repowiki 脏文件
Invoke-Batch "② 批 1 — Quest 文档 / wiki / 协作机制" $questSet $msg1 $true | Out-Null

Invoke-Batch "③ 批 2 — Codex 代码修复" $codexSet $msg2 $false | Out-Null

# ── 3. 推送 ──────────────────────────────────────────────────
Write-Head "④ 推送"
$left = (& git status --porcelain -z) -join ""
if ($left.Split([char]0) | Where-Object { $_.Length -gt 3 }) {
    Write-Host "  仍有未提交改动，sync_push.ps1 要求工作区干净 — 跳过推送。" -ForegroundColor Yellow
    Write-Host "  查看剩余: python scripts/dev_status.py" -ForegroundColor DarkGray
    exit 0
}
if ($NoPush) {
    Write-Host "  -NoPush 指定，仅提交不推送。" -ForegroundColor DarkGray
    exit 0
}
if (Confirm-Step "推送到 Gitee + GitHub CN?") {
    & powershell -ExecutionPolicy Bypass -File "$root\scripts\sync_push.ps1"
    Write-Host ""
    Write-Host "发货完成。" -ForegroundColor Green
} else {
    Write-Host "  已提交但未推送，稍后可运行: powershell -File scripts/ship.ps1" -ForegroundColor DarkGray
}
