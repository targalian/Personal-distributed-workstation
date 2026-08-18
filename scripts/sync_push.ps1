<#
.SYNOPSIS
    双仓库同步推送：master -> Gitee（中文版），master/en -> GitHub CN/EN 分支

.DESCRIPTION
    1. 要求工作区干净且当前在 master 分支
    2. 将 master 的代码变更合并进 en 分支（en 分支 .gitattributes 中
       README.md 配置了 merge=ours，合并时自动保留英文 README）
    3. 推送 master 到 gitee 远程，推送 master/en 到 origin(GitHub) 的 CN/EN 分支

.PARAMETER SkipMerge
    跳过 master -> en 的同步合并，仅执行推送

.EXAMPLE
    .\scripts\sync_push.ps1
    .\scripts\sync_push.ps1 -SkipMerge
#>
param([switch]$SkipMerge)

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
& python "$root\scripts\update_version.py"
$verDiff = & git -C $root status --porcelain -- "VERSION.json"
if ($verDiff) {
    Invoke-Git add "VERSION.json"
    Invoke-Git commit -m "chore(config): VERSION.json 自动同步 (commit/released_at 对齐 HEAD, sync_push 自动生成)"
}

# 3. 同步 master 代码变更到 en 分支（英文 README 由 merge=ours 属性保护）
if (-not $SkipMerge) {
    Write-Host "==> 同步 master -> en（保留英文 README）"
    Invoke-Git checkout en
    try {
        Invoke-Git merge master --no-edit
    } finally {
        Invoke-Git checkout master
    }
}

# 4. 双端推送
Write-Host "==> 推送 master -> Gitee（中文版）"
Invoke-Git push gitee master

Write-Host "==> 推送 master/en -> GitHub CN/EN 分支"
Invoke-Git push origin master:CN en:EN

Write-Host "完成：Gitee(master 中文) 与 GitHub(CN 中文 / EN 英文) 已同步更新" -ForegroundColor Green
