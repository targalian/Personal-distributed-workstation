# Linux 源码安装与发布检查

本流程验证源码 checkout，当前锁定目标是 **Linux x86_64 / CPython 3.10**。其他 Python、Windows、macOS 以及 wheel/PyPI 安装不在此轮验收范围内。锁定版本来自已验证的运行环境，升级依赖后必须重新生成、安装并执行完整检查。

## 新环境安装

在源码根目录执行，使用独立虚拟环境：

```bash
env -u PYTHONPATH -u PYTHONHOME python3.10 -m venv .venv-release
.venv-release/bin/python -m pip install -r requirements-release-linux-py310.lock
.venv-release/bin/python -m playwright install --with-deps chromium
```

检查需要 bash 和 Node.js（CI 使用 Node 22）。安装 Chromium 的系统依赖可能需要系统包管理权限。`requirements-linux-py310.lock` 固定运行依赖；release 锁另加测试、浏览器及构建工具。只运行工作站时可仅安装运行锁，并执行：

```bash
bash scripts/start_workstation.sh --venv .venv-release
```

基础依赖范围仍以 `requirements.txt` 为来源，`pyproject.toml` 从同一文件读取依赖，避免两份配置漂移。启动器发现依赖不满足时会尝试修复，因此发布验收必须从锁文件安装并先校验精确版本。

## 执行完整检查

```bash
env -u PYTHONPATH -u PYTHONHOME .venv-release/bin/python scripts/release_check.py \
  --output /tmp/workstation-release-check-001
```

输出目录必须不存在，防止覆盖旧证据。检查顺序：

1. 校验平台、虚拟环境、Node 和所有锁定版本；执行 `pip check`。
2. 验证依赖范围及应用导入。
3. 执行本地 loop：Python 语法、全量 pytest、UI 静态检查、设计清单、shell 和 JavaScript 语法。
4. 使用真实 Chrome/Chromium 验证项目描述、DAG、AI 建议审阅、历史、下发及恢复界面；模型响应为模拟数据。
5. 在临时源码和数据目录运行真实启动脚本，验证 Station 健康、首页和 Worker 信息接口；TERM 后退出码为 143，两个 HTTP 端口关闭。
6. 执行固定旧版源码→当前候选→拒绝不支持库→完整归档恢复旧代码/数据的演练，核实需求、验收、身份和回执，见 [升级演练](upgrade-drill.md)。本地 git 必须包含基线历史，缺少时明确失败。

任一门禁失败、超时或被中断，最终报告不能是通过。本地检查项被跳过也不能通过；pytest 自身的历史跳过项会保留在详细输出中，须结合验收范围审阅。子进程超时或退出后清理其进程组；启动冒烟还负责清理自身启动器进程组。

`release.json` 记录提交号、工作区是否有改动、环境、每项状态、耗时、日志路径与最终状态。目录还包含 `local-checks.json`、浏览器截图、启动器及服务日志。工作区有改动的通过结果仅代表该工作区，不代表报告中提交号对应的纯净 checkout。

独立启动检查入口：

```bash
.venv-release/bin/python scripts/startup_smoke.py \
  --venv .venv-release --output /tmp/workstation-startup-check-001
```

## 数据目录隔离

进程启动前可设置 `LAN_MESH_DATA_DIR` 为非空绝对路径。默认的节点身份、认证 token、数据库、回执、日志、缓存、备份和项目工作区使用该目录；默认共享目录变为其下的 `shared/`。首次生成配置也使用该目录。

未设置时保留原来的 `~/.lan_mesh` 和 `~/lan_mesh_shared`。显式配置中的数据库、共享路径、`LAN_MESH_LOG_FILE` 等仍优先；当前目录配置和项目 `.env` 仍可被读取。因此此变量不是文件系统沙箱，也不会自动搬迁现有数据。PM 恢复仍受原数据库路径和主机限制，不能通过切换目录实现恢复迁移。

发布检查使用临时数据目录。启动冒烟额外复制临时源码，排除个人模型池、资源文件和 `.env`，提供独立配置/端口并禁用自动升级。启动器的 Worker 控制台日志位于临时源码的 `.lan_mesh/logs/`，结束前复制到报告。此流程不调用真实模型，不发送 Bot 消息；UDP 发现使用独立端口。

## CI 与验收边界

`.github/workflows/release-check.yml` 在 EN 推送、目标为 EN 的 PR 和手动触发时运行同一入口，获取完整 git 历史并使用 Ubuntu 22.04、Python 3.10、Node 22 和 Playwright Chromium；无论成功失败都尝试保留报告，保留期 14 天。

工作流失败会使该次检查失败。必须另行将其设为受保护分支/发布流程的必需检查，才会阻止对应合并或发布；新增 workflow 本身不证明策略已启用。远程运行结果应以实际 Actions 记录为准。

新虚拟环境验证不等于全新操作系统验收。该流程不能证明真实模型质量、物理多主机故障恢复、付费请求停止、Windows 兼容性、生产数据切换、未覆盖的历史版本/依赖组合升降级或 72 小时稳定性。这些门槛继续见 [1.0 就绪清单](1.0-readiness.md)。
