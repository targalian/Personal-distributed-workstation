# LAN Mesh 分布式 AI 工作站 — 代码审核规范

> 本规范针对 LAN Mesh 项目的 Python/FastAPI 后端 + Tauri/React 桌面端双栈架构，
> 结合分布式多智能体协作场景制定。

---

## 一、通用审核清单（每次 CR 必查）

| # | 检查项 | 说明 |
|---|--------|------|
| 1 | **功能正确性** | 代码是否实现了需求描述的完整功能 |
| 2 | **向后兼容** | 新增/修改 API 端点是否破坏已有客户端调用 |
| 3 | **错误处理** | 异常是否被捕获并以结构化方式返回（见 §四） |
| 4 | **安全性** | API Key/Token 是否通过环境变量读取，禁止硬编码 |
| 5 | **日志输出** | 关键路径是否有 `print(f"[模块名] ...")` 输出 |
| 6 | **类型安全** | 公共函数参数/返回值是否有类型标注 |
| 7 | **无废弃代码** | 已废弃逻辑是否清理（如 orchestrator.py 已标注废弃） |

---

## 二、命名规范

### 2.1 Python 后端

| 类别 | 规则 | 示例 |
|------|------|------|
| 文件名 | 小写 + 下划线 | `agent_runtime.py`, `model_router.py` |
| 类名 | PascalCase | `AgentRuntime`, `ModelPoolConfig` |
| 函数/方法 | 小写 + 下划线 | `execute_task()`, `_call_llm()` |
| 常量 | 全大写 + 下划线 | `PROVIDER_CONFIG`, `DEFAULT_PORT` |
| 私有成员 | 单下划线前缀 | `_current_skill`, `_handlers` |
| API 路径 | 小写 + 连字符/斜杠 | `/api/tasks`, `/pm/create-subagent` |

### 2.2 Tauri/Rust 后端

| 类别 | 规则 | 示例 |
|------|------|------|
| 函数名 | snake_case | `discover_peers()`, `send_heartbeat()` |
| 结构体/枚举 | PascalCase | `DeviceConfig`, `TransferStatus` |
| 常量 | SCREAMING_SNAKE_CASE | `DEFAULT_PORT`, `MAX_RETRIES` |

### 2.3 前端 (React/TypeScript)

| 类别 | 规则 | 示例 |
|------|------|------|
| 组件 | PascalCase | `<Dashboard />`, `<HostCard />` |
| 函数/变量 | camelCase | `fetchHosts()`, `taskList` |
| CSS 类 | kebab-case | `.host-card`, `.tab-panel` |
| 常量 | SCREAMING_SNAKE_CASE | `API_BASE_URL`, `WS_RECONNECT_INTERVAL` |

---

## 三、代码结构与模块组织

### 3.1 文件职责单一原则

每个模块文件只负责一个明确的领域，文件头部 docstring 必须声明职责。

**好的示例** (`agent_runtime.py`):
```python
"""
Agent 运行时 — Worker 端任务执行引擎

职责:
1. 接收 Secretary 分发的子任务
2. 根据技能类型执行任务 (调用 LLM API / 运行工具 / 本地处理)
3. 返回执行结果
"""
```

### 3.2 模块分层规则

```
protocol.py       ← 数据模型 & 协议定义（纯数据，无业务逻辑）
config.py         ← 配置加载 & Pydantic 校验
database.py       ← 持久化层（SQLite CRUD）
agent_runtime.py  ← Agent 执行引擎
pm_agent.py       ← PM Agent 编排逻辑
api.py            ← FastAPI 路由层（薄控制器，不含业务逻辑）
```

**审核要点：**
- 路由层 (`api.py`, `station_api.py`) 只做参数校验和路由分发，业务逻辑委托给对应模块
- `protocol.py` 中禁止出现 HTTP 调用或数据库操作
- 循环依赖检查：`protocol` ← `config` ← `database` ← 业务模块 ← `api`

### 3.3 函数长度限制

- 单个函数不超过 **80 行**（不含空行和注释）
- 超过时拆分为私有辅助方法，如 `_handle_code_generation()`, `_handle_shell_exec()`
- 异步事件循环函数（如 `_ws_push_loop`）可适当放宽，但需包含清晰的循环体注释

---

## 四、错误处理规范

### 4.1 API 端点错误返回

所有 API 端点必须返回结构化错误，禁止裸 500：

```python
# ✅ 正确：使用 HTTPException 携带结构化信息
raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))

# ❌ 错误：未捕获异常导致裸 500
result = some_dangerous_call()  # 可能抛异常但没 try/except
```

### 4.2 Agent 执行错误封装

`AgentRuntime.execute()` 返回统一的错误结构：

```python
# ✅ 正确
try:
    result = handler(input_data)
    return {"output": result, "status": "completed", "usage": usage}
except Exception as e:
    return {"output": {}, "status": "failed", "error": str(e)}
```

### 4.3 PM Agent 失败接管三级策略

PM Agent 处理子任务失败时，**必须**按以下顺序尝试恢复：

1. **同站重试** — 在原 Worker 上重新执行
2. **换站重试** — 选择另一台 Worker 执行
3. **PM 本地接管** — PM 自身兜底处理

**审核要点：** 检查 `pm_agent.py` 中是否有遗漏的失败路径未覆盖三级策略。

### 4.4 LLM 调用降级链

模型路由器必须实现降级链重试：

```python
# ✅ 正确：沿 fallback chain 逐模型重试
for model_id in chain:
    try:
        return self._call_openai_compatible(prompt, model_id, ...)
    except Exception as e:
        last_error = e
        print(f"[AgentRuntime] 模型 {model_id} 调用失败: {e}, 尝试降级...")
        continue
```

---

## 五、日志与输出规范

### 5.1 输出格式

项目采用 `print` 作为日志输出方式，格式统一为：

```python
print(f"[模块名] 描述信息: {变量值}")
```

**模块名前缀对照表：**

| 模块 | 前缀 |
|------|------|
| Agent 运行时 | `[AgentRuntime]` |
| PM Agent | `[PM]` |
| 模型路由 | `[ModelRouter]` |
| Secretary | `[Secretary]` |
| Worker | `[Worker]` |
| 发现服务 | `[Discovery]` |
| 数据库 | `[DB]` |
| WebSocket | `[WS]` |
| 启动自检 | `[Preflight]` |

### 5.2 关键路径日志要求

以下场景**必须**有日志输出：

- API 请求到达与响应（含耗时）
- LLM API 调用（模型名、token 用量）
- 任务状态变更（创建/分发/完成/失败）
- PM Agent 决策过程（单 Agent vs 团队、子任务拆分）
- 降级链触发
- WebSocket 连接/断开
- UDP 发现事件

### 5.3 敏感信息过滤

```python
# ❌ 禁止：日志中输出 API Key
print(f"[ModelRouter] 使用 Key: {api_key}")

# ✅ 正确：脱敏输出
print(f"[ModelRouter] 使用 Key: {api_key[:8]}...")
```

---

## 六、安全规范

### 6.1 API Key 管理

- **禁止硬编码**：所有 API Key 必须通过环境变量读取
- `PROVIDER_CONFIG` 中 `api_key_env` 字段存储环境变量名，运行时通过 `os.environ.get()` 读取

```python
# ✅ 正确
api_key = os.environ.get(cfg["api_key_env"], "")

# ❌ 错误
api_key = "sk-xxxxxxxxxxxxx"
```

### 6.2 路径安全

文件操作必须防路径遍历：

```python
# ✅ 正确：校验路径不逃逸
full_path = shared_folder.get_file(file_path)  # 内部做路径安全检查

# ❌ 错误：直接拼接用户输入
with open(f"{shared_dir}/{user_input}", "r") as f:
```

### 6.3 Shell 执行安全

`_handle_shell_exec` 使用 `subprocess.run()` 时：

- 必须设置 `timeout` 参数
- 审核时关注是否可能被注入危险命令
- 生产环境应增加命令白名单机制

### 6.4 WebSocket 安全

- WS 连接必须有心跳检测，超时自动断开
- 推送消息前校验连接状态
- 禁止在 WS 中传输敏感凭证

---

## 七、性能规范

### 7.1 LLM 调用

- 必须设置 `timeout=120`（当前项目标准）
- 必须追踪 token 用量（`input_tokens`, `output_tokens`）并返回给调用方
- `max_tokens` 参数应根据任务类型合理设置

### 7.2 异步与并发

- FastAPI 端点中涉及 IO 的操作使用 `async/await`
- 长时间运行的后台任务使用 `threading` 或 `asyncio.create_task()`
- 心跳循环、WS 推送循环必须有合理的 sleep 间隔，避免 CPU 空转

### 7.3 数据库

- SQLite 操作必须使用事务保护写操作
- 查询结果集应有合理上限（避免 OOM）
- 避免在请求处理路径中做全表扫描

### 7.4 模型路由性能

- 路由决策应在 **50ms** 内完成（纯规则 + 评分计算）
- 加权评分公式：`Score = 能力覆盖率×0.4 + 成本反向×0.3 + 速度×0.2 - 负载×0.1`
- 审核时验证评分权重是否与项目需求一致

---

## 八、分布式系统专项审核

### 8.1 PM Agent 六项生产级要求

审核 PM Agent 相关代码时，必须逐项检查：

| # | 要求 | 审核要点 |
|---|------|----------|
| 1 | **依赖感知拓扑分发** | 检查 `depends_on` 是否正确解析，前序完成后是否自动注入 `_dependency_outputs` |
| 2 | **动态 Prompt 更新** | 检查 `POST /pm/update-prompt` 是否调用 `set_custom_prompt()` |
| 3 | **选择性技能注入** | 检查 `_current_skill` 是否在执行前正确设置 |
| 4 | **结果聚合** | 检查 `build_aggregation_prompt()` 是否按依赖顺序拼接 |
| 5 | **失败接管** | 检查三级策略是否完整实现（同站→换站→本地） |
| 6 | **自检验证** | 检查 `self_check` 字段是否被 PM 验证 |

### 8.2 节点通信

- Worker 注册必须携带完整 `HostInfo`（含硬件评级 S/A/B/C/D）
- 心跳间隔与 `device_ttl` 配置一致（默认 3s 心跳，12s 超时）
- UDP 发现端口固定为 45454

### 8.3 状态一致性

- Secretary 作为唯一真相源，Worker 只上报状态
- 任务状态变更必须通过 API 端点通知 Secretary，禁止 Worker 间直接同步
- WebSocket 推送为最终一致性，前端需处理短暂不一致

---

## 九、配置管理规范

### 9.1 Pydantic 配置模型

- 所有配置项必须有 Pydantic BaseModel 定义
- 每个字段必须有合理默认值（零配置可启动）
- 新增配置项必须同步更新 `config.py` 和 `config.yaml`

### 9.2 配置加载顺序

```
1. 显式指定路径 (config_path 参数)
2. 环境变量 LAN_MESH_CONFIG
3. ~/.lan_mesh/config.yaml
4. ./config.yaml
5. 默认值
```

**审核要点：** 新增配置加载逻辑是否遵循此优先级链。

### 9.3 模型池配置

- `model_pool.yaml` 中每个模型必须有完整的 `ModelEntryConfig` 字段
- `api_key_env` 字段存储环境变量名（非 Key 本身）
- `fallback` 链必须指向池内已存在的模型 ID

---

## 十、Git 提交规范

### 10.1 Commit Message 格式

```
<type>(<scope>): <subject>

[可选 body]
```

**type 取值：**

| type | 含义 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `refactor` | 重构（不改变行为） |
| `docs` | 文档更新 |
| `chore` | 构建/依赖/脚本 |
| `perf` | 性能优化 |

**scope 取值：** `pm`, `runtime`, `router`, `api`, `ws`, `ui`, `config`, `discovery`, `db`

**示例：**
```
feat(pm): 实现子任务依赖感知拓扑分发

- 新增 _dependency_outputs 注入机制
- 前序任务完成后自动分发后续任务
- 增加 pending 任务队列管理
```

### 10.2 PR 审核要求

- 每个 PR 关联一个 issue 或需求描述
- 涉及 API 变更时必须更新 README.md 中的 API 概览
- 涉及配置变更时必须同步更新 `config.yaml` 和 `config.py`
- 涉及新模块时必须确认无循环依赖

---

## 十一、CR 审核流程

### 11.1 审核分级

| 级别 | 适用场景 | 最少审核人数 |
|------|----------|-------------|
| **L1 快速审核** | 文档、注释、样式修复 | 1 |
| **L2 常规审核** | Bug 修复、小功能迭代 | 1 |
| **L3 深度审核** | 新模块、API 变更、架构调整 | 2 |
| **L4 架构审核** | 核心协议变更、数据库 Schema 变更 | 2 + 架构师 |

### 11.2 审核关注点优先级

1. **正确性** — 功能是否满足需求
2. **安全性** — 是否有安全漏洞
3. **可维护性** — 代码是否易于理解和修改
4. **性能** — 是否有明显的性能问题
5. **风格** — 是否符合命名和格式规范

### 11.3 审核反馈模板

```
## 审核意见

### 🔴 必须修改 (Blocker)
- [文件:行号] 问题描述

### 🟡 建议修改 (Suggestion)
- [文件:行号] 建议描述

### 🟢 可选优化 (Nit)
- [文件:行号] 优化建议

### ✅ 亮点
- 值得肯定的做法
```

---

## 十二、自动化审核（Git Hooks）

项目提供 `.githooks/` 目录，包含两个自动化钩子：

### 12.1 commit-msg — 提交信息格式校验

**触发时机：** 每次 `git commit` 时

**检查内容：**
- 提交信息必须符合 `<type>(<scope>): <subject>` 格式
- `type`: feat, fix, refactor, docs, chore, perf, test, ci, style
- `scope`: pm, runtime, router, api, ws, ui, config, discovery, db, auth, skill, station
- Merge/Revert 提交自动放行

### 12.2 pre-push — 上库前代码审核

**触发时机：** 每次 `git push` 时

**检查内容（共 7 项）：**

| # | 检查项 | 严重级别 | 说明 |
|---|--------|----------|------|
| 1 | Python 语法 | Blocker | `py_compile` 编译检查 |
| 2 | 硬编码密钥 | Blocker | 检测 `api_key = "sk-xxx"` 等模式 |
| 3 | 函数长度 | Warning | 超过 80 行的函数告警 |
| 4 | 模块 docstring | Warning | 缺少模块级文档字符串 |
| 5 | 类型标注 | Warning | 公共函数参数缺少类型注解 |
| 6 | 日志格式 | Warning | `print()` 缺少 `[模块名]` 前缀 |
| 7 | Commit 格式 | Warning | 提交信息格式不规范 |

**Blocker 项阻止 push，Warning 项仅提示不阻止。**

**审核基线：** 按当前分支上游自动解析（`master` → `gitee/master`，`en` → `origin/EN`），无上游时回退 `gitee/master` 或 `HEAD~1`。

### 12.3 启用方式

**自动启用：** 运行启动脚本时自动配置：
```powershell
.\scripts\start_workstation.ps1
```

**手动启用：**
```powershell
git config core.hooksPath .githooks
```

**临时跳过：**
```powershell
git push --no-verify   # 跳过 pre-push
git commit --no-verify # 跳过 commit-msg
```

### 12.4 AI 深度审核

Git Hooks 执行的是规则化静态检查。对于语义级深度审核，在 Qoder 中使用：

```
/code-review
```

AI 审核覆盖 PM Agent 六项生产级要求、架构分层合规性等 Hooks 无法自动检查的内容。

### 12.5 双仓库上库（Gitee 中文 + GitHub CN/EN）

项目同时维护两个远程仓库，上库必须双端推送：

| 远程 | 分支映射 | 内容版本 |
|------|----------|----------|
| `gitee` | `master` → `master` | 中文 README |
| `origin` (GitHub) | `master` → `CN` | 中文 README |
| `origin` (GitHub) | `en` → `EN` | 英文 README |

**上库流程：** 在 `master` 提交后运行：

```powershell
.\scripts\sync_push.ps1
```

脚本自动：检查工作区干净 → 合并 `master` 到 `en`（英文 README 由 `en` 分支 `.gitattributes` 的 `merge=ours` 属性保护，不会被中文覆盖）→ 推送 `gitee master` → 推送 `origin master:CN en:EN`。

**注意：** 两次推送都会触发 pre-push 钩子，任一端 Blocker 不通过即中止，需修复后重跑脚本。
