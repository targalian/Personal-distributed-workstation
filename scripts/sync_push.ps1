<#
.SYNOPSIS
    双仓库同步推送：master -> Gitee + GitHub CN；EN 按需同步

.DESCRIPTION
    1. 要求工作区干净且当前在 master 分支
    2. 推送 master 到 gitee 远程 + origin(GitHub) CN 分支 (默认)
    3. 指定 -WithEN 时额外: 将 master 合并进 en 分支 (.gitattributes
       中 README.md 配置了 merge=ours, 自动保留英文 README) 并推送 EN
    4. 指定 -SkipGitee 时不访问 Gitee，仅同步 GitHub

.PARAMETER WithEN
    额外同步 master -> en 并推送 origin/EN (默认跳过, CN 到阶段性里程碑时手动触发)

.PARAMETER SkipGitee
    暂停 Gitee 推送，仅同步 origin/CN；可与 -WithEN 组合

.EXAMPLE
    .\scripts\sync_push.ps1              # 默认: Gitee + GitHub CN
    .\scripts\sync_push.ps1 -WithEN     # 里程碑: Gitee + GitHub CN + EN
    .\scripts\sync_push.ps1 -SkipGitee  # 仅 GitHub CN
#>
param([switch]$WithEN, [switch]$SkipGitee)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Git {
    # 用 $args 透传 (避免 [string[]] 具名参数绑定在部分场景丢失实参)
    # hook 提示经 stderr 转发: PS 5.1 + ErrorActionPreference=Stop 下
    # 即使 2>&1 合并仍会抛 NativeCommandError 中断, 故函数内局部降级
    $ErrorActionPreference = "Continue"
    & git -C $root @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($args -join ' ') 执行失败 (exit $LASTEXITCODE)"
    }
}

# 1. 工作区必须干净
$status = & git -C $root status --porcelain
if ($status) {
    throw "存在未提交的改动，请先提交或 stash 后再运行：`n$status"
}

# 2. 必须在 master 分支
$branch = (& git -C $root branch --show-current).Trim()
if ($branch -ne "master") {
    throw "请在 master 分支上运行本脚本（当前分支: $branch）"
}

# 2.5 P2 #10: VERSION.json 自动同步 (commit/released_at 对齐 HEAD, 变更自动提交)
# 注: VERSION.json 必须加引号 — 裸词含点会被 PowerShell 解析为成员访问表达式而丢失
Write-Host "==> 同步 VERSION.json"
& python (Join-Path $PSScriptRoot "update_version.py")
if ($LASTEXITCODE -ne 0) {
    throw "VERSION.json 同步失败，已停止推送 (exit $LASTEXITCODE)"
}
$verDiff = & git -C $root status --porcelain -- "VERSION.json"
if ($verDiff) {
    Invoke-Git add "VERSION.json"
    Invoke-Git commit -m "chore(config): VERSION.json 自动同步 (commit/released_at 对齐 HEAD, sync_push 自动生成)"
}

# 3. 默认推送 Gitee + GitHub CN；显式暂停 Gitee 时完全跳过该远端
$pushFailures = @()
$pushedEndpoints = "GitHub(CN)"
if ($SkipGitee) {
    Write-Host "==> 按参数跳过 Gitee"
} else {
    Write-Host "==> 推送 master -> Gitee（中文版）"
    try { Invoke-Git push gitee master }
    catch { $pushFailures += "Gitee(master): $($_.Exception.Message)" }
    $pushedEndpoints = "Gitee(master) + GitHub(CN)"
}

Write-Host "==> 推送 master -> GitHub CN 分支"
try { Invoke-Git push origin master:CN }
catch { $pushFailures += "GitHub(CN): $($_.Exception.Message)" }

if ($pushFailures.Count -gt 0) {
    throw "同步未全部完成，请解决以下错误后重跑：`n$($pushFailures -join '`n')"
}

# 4. EN 分支按需同步 (CN 到阶段性里程碑时手动 -WithEN 触发)
if ($WithEN) {
    Write-Host "==> 同步 master -> en（保留英文 README）"
    Invoke-Git checkout en
    try {
        Invoke-Git merge master --no-edit
    } finally {
        Invoke-Git checkout master
    }
    Write-Host "==> 推送 en -> GitHub EN 分支"
    Invoke-Git push origin en:EN
    $pushedEndpoints += " + GitHub(EN)"
}

Write-Host "完成：${pushedEndpoints} 已同步更新" -ForegroundColor Green
