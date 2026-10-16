# 11 脚本与子项目

运维脚本、技能库资产、独立 Tauri 子项目。

## Linux 发布检查（iter-58）

`release_check.py` 校验 Linux x86_64 / Python 3.10 虚拟环境精确依赖，顺序执行依赖、导入、本地 loop、Chrome/Chromium 和启动检查。新输出目录保留提交/dirty 标记、状态、日志和截图；失败即停止，中断写入失败证据。`startup_smoke.py` 使用临时源码、数据和随机端口，验证真实 Station/Worker HTTP 以及 TERM 清理；不会启动模型任务。GitHub Actions 使用同一命令，远程状态和分支保护须单独核实。锁文件范围为源码安装，参见 [完整说明](../../release-checks.md)。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| quicklan-main/ | 独立子项目: Tauri + React 桌面文件共享应用 |
| scripts/backup_workstation.py | Create, verify or restore an offline workstation backup without overwriting live data. |
| scripts/check_environment.py | Validate startup dependencies and imports without contacting a package index. |
| scripts/release_check.py | Run the Linux release gates and preserve evidence without invoking real models. |
| scripts/start_workstation.bat | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/start_workstation.ps1 | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/start_workstation.sh | 跨平台一键启动 Station (bat/ps1/sh) |
| scripts/startup_smoke.py | Verify real launcher HTTP readiness and shutdown using disposable source and data. |
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

## Unix 启动与环境检查（iter-35）

`scripts/start_workstation.sh` 支持 Linux/macOS、Python 3.10+。启动顺序为：校验参数 → 创建或检查虚拟环境 → 验证依赖版本及导入 → 按需安装 → 验证应用导入 → 启动 Station。默认环境为项目 `.venv`；损坏环境或 Windows 环境只报错保留，不自动删除，用户可通过 `--venv .venv-linux --python python3` 创建独立环境。

`scripts/check_environment.py` 读取 requirements 的版本要求，使用当前环境的包元数据与实际导入双重验证。存在 FastAPI 不代表依赖齐全；Pydantic 版本不符、缺 cryptography 等均会触发修复。pip 安装失败返回非零退出码并显示手动修复命令。已满足依赖时不访问包索引。所有解释器调用都清除 PYTHONPATH/PYTHONHOME，禁用用户 site-packages，避免 ROS 等系统环境污染。

- `--check`：准备环境并检查，不启动服务；首次可能安装依赖、复制模型池模板。
- `--install-dev`：使用 `requirements-dev.txt`，包含运行依赖、pytest、pytest-cov、httpx、ruff。
- `--config / --shared / --venv`：相对于调用者当前目录解析，支持空格和中文路径。
- `--with-worker`：Station/Worker 共享传入的 config/shared 配置；日志保存在 `.lan_mesh/logs/`；Station 退出或启动器收到 INT/TERM 时清理本次创建的两个进程。Worker 提前退出时停止 Station 并报错。
- `--dev`：单独启动 Station 的文件监控模式，不与 `--with-worker` 组合，避免应用自重启脱离进程监督。

Station 已内嵌本机 Worker，并在发现窗口结束后自动选举 Secretary。默认启动不要求预先填写模型密钥；实际模型调用前需在 Web UI 或 `.env` 中配置。Windows 启动脚本本轮未改动。

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
| 2026-10-15 | iter-47 | 新增 backup_workstation.py create/verify/restore 与路径预览，提供配置覆盖和恢复映射说明 |
| 2026-10-13 | iter-35 | Unix 启动器保留异常环境、检查完整依赖、隔离 ROS 路径、监督 Worker 生命周期；统一 Python 3.10+ |
| 2026-08-16 | iter-30 补③ | P2 #10: VERSION.json 自动化 (update_version.py 幂等同步 + sync_push 推送前自动调用提交) |
| 2026-08-16 | iter-27 后 | 初建 |


## scripts/backup_workstation.py — 离线备份与恢复

create --plan 列出当前配置推导的根目录与文件；create --offline-confirmed 在停止写入后打包，verify 完整校验，restore 只恢复到新目录。支持 --config / --db / --shared / --worker-shared / --state-dir / --extra-file。运行时依赖项目虚拟环境，错误返回非零，不输出凭据内容。恢复目录提供原路径映射；原位切换与回退按 [备份恢复说明](../../backup-restore.md) 操作。
