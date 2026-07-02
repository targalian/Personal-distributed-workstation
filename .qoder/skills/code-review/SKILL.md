---
name: code-review
description: 审核 LAN Mesh 分布式 AI 工作站项目代码，遵循项目专属审核规范（命名、分层、错误处理、安全、性能、PM Agent 六项生产级要求）。当用户请求代码审核、审查 PR、检查代码质量，或使用 /code-review 时触发。
---

# LAN Mesh 代码审核技能

## 审核流程

收到审核请求后，按以下步骤执行：

1. **判定审核级别** — 根据变更范围选择 L1-L4（见 §审核分级）
2. **执行通用清单** — 逐项检查 7 项必查项
3. **执行专项审核** — 根据变更文件类型选择对应专项
4. **输出结构化反馈** — 使用反馈模板输出

## 通用审核清单（每次必查）

| # | 检查项 | 说明 |
|---|--------|------|
| 1 | 功能正确性 | 代码是否实现了需求描述的完整功能 |
| 2 | 向后兼容 | 新增/修改 API 端点是否破坏已有客户端调用 |
| 3 | 错误处理 | 异常是否被捕获并以结构化方式返回 |
| 4 | 安全性 | API Key/Token 通过环境变量读取，禁止硬编码 |
| 5 | 日志输出 | 关键路径有 `print(f"[模块名] ...")` 输出 |
| 6 | 类型安全 | 公共函数参数/返回值有类型标注 |
| 7 | 无废弃代码 | 已废弃逻辑是否清理 |

## 命名规范速查

### Python 后端
- 文件名: `小写_下划线.py`
- 类名: `PascalCase`
- 函数/方法: `小写_下划线()`，私有用 `_前缀`
- 常量: `SCREAMING_SNAKE_CASE`
- API 路径: `/api/tasks`, `/pm/create-subagent`

### Rust 后端 (Tauri)
- 函数: `snake_case()`
- 结构体/枚举: `PascalCase`
- 常量: `SCREAMING_SNAKE_CASE`

### 前端 (React/TypeScript)
- 组件: `<PascalCase />`
- 函数/变量: `camelCase`
- CSS 类: `kebab-case`
- 常量: `SCREAMING_SNAKE_CASE`

## 模块分层规则

```
protocol.py       ← 数据模型（纯数据，无业务逻辑）
config.py         ← Pydantic 配置校验
database.py       ← SQLite CRUD
agent_runtime.py  ← Agent 执行引擎
pm_agent.py       ← PM Agent 编排逻辑
api.py            ← FastAPI 路由层（薄控制器）
```

**审核要点：**
- 路由层只做参数校验和路由分发，不含业务逻辑
- `protocol.py` 禁止 HTTP 调用或数据库操作
- 依赖方向: `protocol ← config ← database ← 业务模块 ← api`
- 单函数不超过 **80 行**（不含空行和注释）

## 错误处理专项

### API 端点 — 结构化错误
```python
# ✅ 正确
raise HTTPException(status_code=409, detail=result.get("message", "启动失败"))

# ❌ 禁止：裸 500
result = some_dangerous_call()  # 无 try/except
```

### Agent 执行 — 统一返回结构
```python
# ✅ 正确
try:
    result = handler(input_data)
    return {"output": result, "status": "completed", "usage": usage}
except Exception as e:
    return {"output": {}, "status": "failed", "error": str(e)}
```

### PM Agent 失败接管三级策略
1. **同站重试** — 原 Worker 重新执行
2. **换站重试** — 选另一台 Worker
3. **PM 本地接管** — PM 自身兜底

**审核要点：** `pm_agent.py` 中每个失败路径是否覆盖三级策略。

### LLM 降级链重试
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

## 日志输出规范

格式: `print(f"[模块名] 描述信息: {变量值}")`

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

**必须有日志的场景：** API 请求/响应、LLM 调用（模型名+token用量）、任务状态变更、PM 决策过程、降级链触发、WS 连接/断开、UDP 发现事件

**敏感信息脱敏：**
```python
# ❌ 禁止
print(f"使用 Key: {api_key}")
# ✅ 正确
print(f"使用 Key: {api_key[:8]}...")
```

## 安全专项

- **API Key**: 全部通过 `os.environ.get()` 读取，禁止硬编码
- **路径安全**: 文件操作使用 `shared_folder.get_file()` 防路径遍历
- **Shell 执行**: `subprocess.run()` 必须设 `timeout`，生产环境加命令白名单
- **WebSocket**: 心跳检测 + 超时断开 + 禁止传输敏感凭证

## 性能专项

- **LLM 调用**: `timeout=120`，必须追踪 `input_tokens` / `output_tokens`
- **异步**: IO 操作用 `async/await`，后台任务用 `asyncio.create_task()`
- **数据库**: 写操作加事务保护，避免全表扫描
- **模型路由**: 决策 < 50ms，评分公式 `能力×0.4 + 成本反向×0.3 + 速度×0.2 - 负载×0.1`

## 分布式系统专项 — PM Agent 六项要求

审核 `pm_agent.py` 时**必须逐项检查**：

| # | 要求 | 审核要点 |
|---|------|----------|
| 1 | 依赖感知拓扑分发 | `depends_on` 解析正确，前序完成后自动注入 `_dependency_outputs` |
| 2 | 动态 Prompt 更新 | `POST /pm/update-prompt` 调用 `set_custom_prompt()` |
| 3 | 选择性技能注入 | `_current_skill` 在执行前正确设置 |
| 4 | 结果聚合 | `build_aggregation_prompt()` 按依赖顺序拼接 |
| 5 | 失败接管 | 三级策略完整实现（同站→换站→本地） |
| 6 | 自检验证 | `self_check` 字段被 PM 验证 |

**节点通信：** Worker 注册携带完整 `HostInfo`，心跳 3s/超时 12s，UDP 端口 45454

**状态一致性：** Secretary 是唯一真相源，禁止 Worker 间直接同步

## 配置管理规范

- 所有配置项有 Pydantic BaseModel + 合理默认值
- 加载优先级: 显式路径 > 环境变量 `LAN_MESH_CONFIG` > `~/.lan_mesh/config.yaml` > `./config.yaml` > 默认值
- 新增配置必须同步更新 `config.py` 和 `config.yaml`
- `model_pool.yaml` 中 `api_key_env` 存环境变量名，`fallback` 链指向池内已有模型

## Git 提交规范

```
<type>(<scope>): <subject>
```

| type | 含义 |
|------|------|
| feat | 新功能 |
| fix | Bug 修复 |
| refactor | 重构 |
| docs | 文档 |
| chore | 构建/依赖/脚本 |
| perf | 性能优化 |

scope: `pm`, `runtime`, `router`, `api`, `ws`, `ui`, `config`, `discovery`, `db`

## 审核分级

| 级别 | 适用场景 | 最少审核人 |
|------|----------|-----------|
| L1 | 文档、注释、样式 | 1 |
| L2 | Bug 修复、小功能 | 1 |
| L3 | 新模块、API 变更 | 2 |
| L4 | 核心协议/Schema 变更 | 2 + 架构师 |

## 反馈模板

```markdown
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

## 详细参考

完整审核规范详见项目根目录 [CODE_REVIEW_GUIDE.md](../../../CODE_REVIEW_GUIDE.md)，包含更多示例和边界情况说明。
