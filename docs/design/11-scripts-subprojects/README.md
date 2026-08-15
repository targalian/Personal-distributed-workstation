# 11 脚本与子项目

运维脚本、技能库资产、独立 Tauri 子项目。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| quicklan-main/ | 独立子项目: Tauri + React 桌面文件共享应用 |
| scripts/start_workstation.bat | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/start_workstation.ps1 | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/start_workstation.sh | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/sync_docs.py | docs/design 模块清单一致性校验/生成器 (D2-docs-sync)。 |
| scripts/sync_push.ps1 | ★ 双库同步推送脚本 (上库唯一入口) |
| skills/ | 技能库资产 (SKILL.md 格式, 中央分发) |
<!-- /AUTO:module-list -->
---

## scripts/sync_push.ps1 — 双库推送（上库唯一入口）

**规范**: 禁止 git push 直推；所有上库必须经此脚本。

**流程**: master → gitee/master；master 合并到 en → origin/CN + origin/EN。

**已知坑**（历次迭代沉淀）:
- origin 固定 push refspec（master:CN、en:EN）导致显式推送报
  "Everything up-to-date" 实际未推 → 绕过: `git push origin HEAD:CN en:EN`
- master→en 合并偶发假报 "Already up to date" 漏合并 → 需手动
  `git checkout en; git merge master` 补做
- gitee 推送偶发网络中断 → 原样重试即可

## scripts/start_workstation.* — 一键启动

bat / ps1 / sh 三平台版本，激活 .venv 后 `python main.py station`。

## skills/ — 技能库资产

当前三个技能:
- `cloud-storage-sync`: 云存储同步操作指引
- `multi-agent-architect`: PM 规划器加载的多 Agent 架构技能（含 reference.md）
- `shared-folder-access`: 共享文件夹访问指引

**格式**: `{skill_id}/SKILL.md`（YAML front matter）；经 skill_registry
中央注册后 HTTP 分发到 Worker。新增技能同步更新 04-execution-engine 文档。

## quicklan-main/ — 独立子项目（Tauri 文件共享）

React + TypeScript 前端、Rust/Tauri 后端的桌面文件共享应用。
**与 lan_mesh 主项目相互独立**（不共享代码），本项目部分设计
（discovery/shared_folder 的 SQLite 用法）参考了它。有独立构建体系
（npm + cargo），不纳入本仓库 Python 测试范围。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建 |
