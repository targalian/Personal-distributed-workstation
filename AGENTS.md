# LAN Mesh 项目 Agent 工作约定

分布式个人 AI 工作站：局域网异构主机组成统一调度网格，PM Agent 驱动任务拆解、
团队组建、分布式执行与结果聚合。

- 技术栈：Python 3.11+ / FastAPI / Uvicorn / SQLite / Pydantic v2
- 前端：单文件 HTML 仪表盘 + Tauri React SPA (`quicklan-main/`, `webui/`)
- 当前版本见 `VERSION.json`，迭代进度见 `loop_status.json`

> 本文件面向所有编码 Agent。Qoder 用户另见 `.qoder/skills/`（loop-engineering、
> code-review、docs-sync、ui-change-checklist、repowiki-update），内容以本文件为硬约束基线。

## 模块职责

| 文件 | 职责 |
|------|------|
| `lan_mesh/station_controller.py` | Station Director 壳类：8 mixin 组合声明 + StationState + __init__（iter-75 拆分后 246 行） |
| `lan_mesh/station_{lifecycle,selfheal,secretary,local_pm}.py` | 控制器职责域 mixin：生命周期/建 app ・故障自愈 ・ Secretary 选举/激活 ・ 本机 PM 启停与恢复 |
| `lan_mesh/station_{pm_control,scheduler,sync,hosts}.py` | 控制器职责域 mixin：PM 远程控制 ・ 负载选站/自动伸缩/联邦转发 ・ 集群配置与密钥同步 ・ 主机发现与注册 |
| `lan_mesh/station_api.py` | Secretary HTTP API：任务/PM/团队/项目/Bot/Graph 端点 |
| `lan_mesh/chat_handler.py` | 秘书对话：LLM 回复 + 意图检测 + 操作执行 |
| `lan_mesh/pm_agent.py` | PM Agent：任务规划、团队组建、子任务分发、结果聚合、交付闭环 |
| `lan_mesh/orchestrator.py` | Graph Engine：DAG 状态机、Checkpoint、断点恢复 |
| `lan_mesh/worker.py` | Worker 节点：接收 PM 指令、运行子 Agent |
| `lan_mesh/database.py` | SQLite 持久化：主机/任务/PM/团队/进度/记忆/Checkpoint |
| `lan_mesh/model_router.py` | 模型路由器：多模型池、技能路由、fallback 链 |
| `lan_mesh/agent_runtime.py` | Agent 运行时：LLM 调用、工具执行、Prompt 管理 |
| `lan_mesh/bot_gateway.py` | Bot 网关：Telegram/企微推送 + 自然语言入口 |
| `lan_mesh/discovery.py` | UDP 广播发现：局域网主机自动注册 |
| `lan_mesh/skill_registry.py` | 技能库：SKILL.md 扫描、注册、分配 |

## 编码规范

1. **向后兼容**：新增列用 `ALTER TABLE` + `try/except`；新增端点不修改已有路径。
2. **线程安全**：DB 操作通过 `_get_conn()` 获取线程本地连接。
3. **错误隔离**：所有 HTTP 调用包裹 `try/except`，超时设为 10s。
4. **日志必须**：关键路径（任务创建/完成/失败）必须有 `print` 输出，且带模块前缀
   `print(f"[Station] 任务已创建: {task_id}")`。合法前缀：`AgentRuntime` `PM`
   `ModelRouter` `Secretary` `Worker` `Discovery` `DB` `WS` `Preflight` `Station`
   `Chat` `Bot` `Skill` `Task` `Project` `Host` `Rating`。
5. **类型标注**：公共方法（非 `_` 开头）参数和返回值必须有类型标注。
6. **Pydantic v2**：用 `model_validator`，不用 `validator`。
7. **命名**：模块 `小写_下划线.py`，类 `PascalCase`。
8. **函数长度**：上限 80 行，超出会触发门禁警告。
9. **模块 docstring**：每个模块（除 `__init__.py`）必须有模块级 docstring。

## 禁止事项

- 不得删除已有 API 端点
- 不得修改 DB 表的主键结构
- 不得硬编码 API Key 或密码（门禁强制拦截）
- 不得修改 `.git/hooks` 或 git config
- 不得跳过编译验证直接进入下一个任务
- 不得 `git push` 直推（见下方推送流程）

## 验证流程

改完代码按顺序自查，编译与门禁失败属 Blocker，必须当轮修复：

```powershell
# 1. 编译检查
python -c "import py_compile; files=['lan_mesh/pm_agent.py','lan_mesh/station_controller.py','lan_mesh/station_api.py','lan_mesh/chat_handler.py','lan_mesh/bot_gateway.py','lan_mesh/database.py','lan_mesh/worker.py','lan_mesh/api.py','lan_mesh/orchestrator.py']; [py_compile.compile(f, doraise=True) for f in files]; print('All OK')"

# 2. 导入检查
python -c "from lan_mesh.station_controller import StationController; print('OK')"

# 3. 测试
python -m pytest -q

# 4. 设计文档清单一致性（门禁第 8 项，FAIL 会阻断推送）
python scripts/sync_docs.py
# 漂移时修复：python scripts/sync_docs.py --write
```

## 提交与推送

**Commit message 格式**：`<type>(<scope>): <subject>`

- type：`feat` `fix` `refactor` `docs` `chore` `perf` `test` `ci` `style`
- scope：`pm` `runtime` `router` `api` `ws` `ui` `config` `discovery` `db`
  `auth` `skill` `station` `deploy` `scripts`

**推送**：禁止 `git push` 直推，统一走脚本（自动同步 VERSION.json，要求工作区
干净且在 master 分支）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/sync_push.ps1
# 里程碑追加英文仓库：sync_push.ps1 -WithEN
```

`.githooks/pre-push` 有 9 项审核：语法、硬编码密钥、函数长度、模块 docstring、
类型标注、日志前缀、commit message、docs/design 清单校验、设计文档同步提醒。
第 1/2/8 项为 Blocker，其余为 Warning。

`.githooks/post-commit` 在代码文件（`lan_mesh/`、`scripts/`、`webui/` 等）提交后
自动触发 Repo Wiki 更新：写 `.qoder/repowiki/.pending/` 待办队列（已加入
`.gitignore`），若 10 分钟内无并发任务则后台拉起 `qoderclicn` 按
`.qoder/skills/repowiki-update/SKILL.md` 执行扫描与同步。纯 wiki 提交不递归触发；
`LAN_MESH_WIKI_DRY_RUN=1` 可调试（只验证守卫与入队，不拉起 Agent）。

## 状态文件维护

每轮迭代结束必须同步这三处，否则下一轮状态判断会失真：

- `loop_status.json` — `current_phase` 转 `completed`、更新 `next_tasks`、
  `iteration_count++`、`notes` 记录本轮要点与实测数字
- `VERSION.json` — 版本号、commit、`released_at`、`note`（推送脚本会校验）
- `docs/design/` — 涉及职责/接口/设计决策变化时同步对应功能域文档

## 关键 API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/secretary/chat` | POST | 秘书对话 |
| `/api/tasks` | GET/POST | 任务 CRUD |
| `/api/pm/{id}/deliver` | POST | 交付物上报 |
| `/api/pm/{id}/inject-input` | POST | 注入 Boss 回复 |
| `/api/tasks/{id}/cancel` | POST | 取消任务 |
| `/api/tasks/{id}/graph` | GET/PUT | DAG 图读写 |
| `/api/tasks/{id}/resume` | POST | 断点恢复 |
| `/api/runtime/task-flow` | GET | 任务流瀑布追踪 |
| `/role/start-pm` | POST | 本机启动 PM |
| `/pm/create-subagent` | POST | 创建子 Agent |

## 启动命令

```bash
python main.py station          # Station Director（推荐入口）
python main.py worker           # Worker 节点
python main.py station --port 8080
```

## 多 Agent 协作

本项目可能由多个编码 Agent（Codex CLI、Qoder Quest）并行开发。为避免互相覆盖：

- 开工前先读 `loop_status.json` 的 `current_phase` 与 `notes`，确认没人正在做同一
  任务；`AGENT_LOCKS.md`（若存在）记录了各 Agent 当前占用的文件范围。
- 单轮迭代只认领一个路线图任务，完成后立即回写 `loop_status.json`，缩短占用窗口。
- 跨 Agent 交接的结论写进 `loop_status.json.notes` 或 `docs/reference/`，不要只留在
  会话里——另一个 Agent 读不到你的对话历史。
