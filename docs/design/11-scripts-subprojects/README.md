# 11 脚本与子项目

运维脚本、技能库资产、独立 Tauri 子项目。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| .qoder/skills/ | Qoder 技能库 (docs-sync / code-review / repowiki-update 等) |
| quicklan-main/ | 独立子项目: Tauri + React 桌面文件共享应用 |
| scripts/boss_channel.py | Boss 通道 — Codex 对接秘书 (Secretary) 与 PM Agent 的命令行客户端。 |
| scripts/check_unbound_names.py | 静态扫描 lan_mesh/ 中「被引用但从未绑定」的全局名。 |
| scripts/dev_status.py | Loop Engineering - 全局开发态势看板 (只读, 人在回路外时的唯一观察入口)。 |
| scripts/fix_task_project_binding.py | 一次性修正: 把 BUG-033 修复前对话派发的任务补绑到正式项目。 |
| scripts/ship.ps1 | ★ 一键发货: 按 Agent 归属分批提交 + 调 sync_push 推送 |
| scripts/start_workstation.bat | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/start_workstation.ps1 | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/start_workstation.sh | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/sync_docs.py | docs/design 模块清单一致性校验/生成器 (D2-docs-sync)。 |
| scripts/sync_push.ps1 | ★ 双库同步推送脚本 (上库唯一入口) |
| scripts/update_version.py | VERSION.json 自动同步脚本 (P2 #10: commit/released_at 对齐 HEAD + 可选 bump) |
| skills/ | 技能库资产 (SKILL.md 格式, 中央分发) |
<!-- /AUTO:module-list -->
---

## scripts/sync_push.ps1 — 双库推送（上库唯一入口）

**规范**: 禁止 git push 直推；所有上库必须经此脚本。

**流程**: 干净/分支检查 → VERSION.json 自动同步 (P2 #10, 变更自动提交)
→ master → gitee/master；master 合并到 en → origin/CN + origin/EN。

## scripts/update_version.py — VERSION.json 自动同步 (P2 #10)

commit/released_at 自动对齐 git HEAD (幂等); `--bump patch/minor/major`
递增版本号 (低位归零), `--note` 更新说明。由 sync_push.ps1 推送前自动
调用, 消除 VERSION.json 手工维护遗漏。

**已知坑**（历次迭代沉淀）:
- origin 固定 push refspec（master:CN、en:EN）导致显式推送报
  "Everything up-to-date" 实际未推 → 绕过: `git push origin HEAD:CN en:EN`
- master→en 合并偶发假报 "Already up to date" 漏合并 → 需手动
  `git checkout en; git merge master` 补做
- gitee 推送偶发网络中断 → 原样重试即可

## scripts/boss_channel.py — Codex ↔ 秘书/PM 通道

**动因**: Codex CLI 需以 Boss 身份向秘书交接需求、确认项目蓝图, 并监管 PM
执行与排查 bug。此前只能手写 `Invoke-RestMethod`, token / 编码 / 错误处理
每次重来, 且 401/503 语义要靠猜。本脚本把 Station HTTP API 封成稳定子命令,
供 Codex 与人共用。

**信任根**: `--token` > `LAN_MESH_TOKEN` > `~/.lan_mesh/mesh_token`
(mesh token 持有人 = boss)。

| 子命令 | 端点 | 用途 |
|---|---|---|
| `health` | `GET /health` | 探活与组件状态 (白名单, 免 token) |
| `diag` | health + tasks + pm + stall-alerts + errors | 一次性体检 (监管入口) |
| `projects` | `GET /api/projects` | 项目列表 (折叠冗长字段) |
| `blueprint <id> [--set-file]` | `GET/PUT /api/projects/{id}/blueprint` | 读/整体写蓝图 |
| `chat <msg> [--conv]` | `POST /api/secretary/chat` | 向秘书投递指令 (默认 180s 超时) |
| `tasks [--status]` / `task <id>` | `GET /api/tasks[/{id}]` | 任务列表 / 详情含 `acceptance_review` |
| `graph <id> [--put-file]` | `GET/PUT /api/tasks/{id}/graph` | 读/回写 DAG 图 |
| `pm [<id>]` / `progress <id>` | `GET /api/pm[/{id}]`, `/progress` | PM 状态与进度流水 |
| `reply <pm_id> <content>` | `POST /api/pm/{id}/inject-input` | 向 awaiting_input 的 PM 注入回复 |
| `watch [--interval]` | 轮询 `GET /api/tasks` | 状态变化时打印一行 (长任务监管) |

**设计约束**: 所有网络异常收敛为 `(0, {"error": ...})` 不抛出; 输出统一
UTF-8 JSON 便于 Codex 解析; 401/503/409 附带成因提示 (token 失效 /
Secretary 未激活 / PM 非等待态)。

**首次运行即抓到两个真问题**: BUG-032 (停滞告警 100 条中 99 条幽灵, 已修)
与 `diag` 内错用 `/api/runtime/errors` (实际为 `/api/errors/recent`, 已改)。

## scripts/start_workstation.* — 一键启动

bat / ps1 / sh 三平台版本，激活 .venv 后 `python main.py station`。

**iter-76 对齐增强 (三端功能一致)**:
- Python 版本检查 (>=3.10, bat 新增)
- pip 自动升级 + 清华镜像优先 + 官方 PyPI 回退 + 进度条显示
- .env 文件加载 (bat 新增, main.py 已有兜底)
- API Key 环境变量检测 (6 个 key 名, 缺失时提示)
- Git Hooks 配置 (core.hooksPath → .githooks)
- CLI Agent PATH 注入 (npm global + Node.js, 供 shadow_dev 使用)
- 防火墙弹窗提示 (启动前打印, 避免用户困惑)

## 变更记录

| 日期 | 迭代 | 变更 |
|------|------|------|
| 2026-09-09 | iter-92 | 新增 `scripts/boss_channel.py`: Codex ↔ 秘书/PM 通道 12 子命令 (health/diag/projects/blueprint/chat/tasks/task/graph/pm/progress/reply/watch), mesh token 自动加载 + 错误分类提示; `sync_docs.py` MAPPING 登记该脚本 |
| 2026-09-02 | iter-76 | start_workstation 三端对齐: bat 补齐版本检查/.env/API Key/Git Hooks/防火墙提示; ps1/sh 加镜像+进度条 |

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
| 2026-08-16 | iter-30 补③ | P2 #10: VERSION.json 自动化 (update_version.py 幂等同步 + sync_push 推送前自动调用提交) |
| 2026-08-16 | iter-27 后 | 初建 |
