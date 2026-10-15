# 04 执行引擎

Worker 侧的任务执行能力：守护进程、Agent 运行时、能力卡片、Prompt 定制、
工具/MCP/沙箱/技能体系。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| agent_card.py | Agent Card 生成与管理 — 借鉴 A2A 协议的 Agent Card 机制 |
| agent_prompt.py | 子 Agent 通用 Prompt 模板与定制构建器 |
| agent_runtime.py | Agent 运行时 — Worker 端任务执行引擎 |
| mcp_client.py | MCP 客户端 — 轻量级 JSON-RPC 2.0 客户端 |
| mcp_gateway.py | MCP 网关 — 中央工具调度枢纽 |
| sandbox.py | F2.2: 代码执行沙箱 — 安全隔离执行 Agent 生成的代码。 |
| skill_registry.py | 技能库注册表 — 中央技能管理与分发系统 |
| tool_registry.py | 工具注册表 — 插件化工具管理系统 |
| worker.py | Worker Agent - 部署在各主机上的守护进程 |
<!-- /AUTO:module-list -->
---

## worker.py — Worker 守护进程

**启动流程**: 生成 device_id → 采集 host_info → 创建 shared_folder →
启动 FastAPI → UDP 发现 Secretary → HTTP 注册 → 心跳循环。

**职责**: 本机配置采集、共享文件夹暴露、接受 Secretary 的任务分发与文件下载。

## agent_runtime.py — Agent 运行时（52KB，Worker 侧最大模块）

**职责**: 接收分发子任务并按技能类型执行。

**执行策略**: code_generation / code_review / document_summary（外部 LLM API）、
shell_exec、file_ops、monitoring、rag_search（预留）。

**关键设计**: `custom_system_prompt` 注入点 —— PM 分发的定制 prompt 在此覆盖
默认人设；LLM API Key 经环境变量/资源池配置获取（S1/S3 密钥同步的受益方）。

## agent_card.py — Agent Card（借鉴 A2A 协议）

每个 Worker 启动时生成能力卡片（技能声明、可用工具、模型偏好），
Secretary 据此做任务匹配与分发。

## agent_prompt.py — Prompt 定制体系

**组件**:
- `BASE_SUBAGENT_PROMPT`: 所有子 Agent 共享通用部分（身份/准则/协议/约束）
- `build_subagent_prompt()`: PM 按任务定制角色、上下文、依赖、质量要求
- `PROGRESS_REPORT_FORMAT`: 标准化进度上报格式
- `build_dispatch_context()`: 分发时附加上下文

**链路**: PM 调用 build → `/pm/create-subagent` 端点 system_prompt 字段 →
Worker 注入 AgentRuntime.custom_system_prompt。

## tool_registry.py — 工具注册表

参考 Anthropic MCP Tool 概念：内置工具（file_read/file_write/shell_exec/
http_request）+ YAML 插件 + 运行时动态注册 + 执行调度。每个工具含
name/description/input_schema/handler。

## mcp_client.py + mcp_gateway.py — MCP 体系

- **mcp_client.py**: JSON-RPC 2.0 客户端，支持 stdio（本地子进程）与
  HTTP（远程 Server）两种传输；initialize → tools/list → tools/call。
- **mcp_gateway.py**: 中央工具调度枢纽。维护全部 MCP Server 连接池 →
  聚合工具列表（统一 /tools/list）→ 路由调用（统一 /tools/call）→
  自动重连；工具描述按模型强弱动态调整示例数量。

**架构链路**: Agent → POST /tools/call → MCP Gateway → JSON-RPC → MCP Servers

## sandbox.py — 代码执行沙箱（F2.2）

subprocess 隔离执行 Agent 生成代码（非 eval）：超时保护（默认 30s / 上限 120s）、
临时工作目录隔离、可选 venv 隔离、输出截断 10KB。

## skill_registry.py — 技能库注册表

**分发链路**: Station Director 扫描 `skills/` 目录 → 注册 SQLite
（skills + skill_assignments 表）→ Worker HTTP 拉取
（GET /api/station/skills/download?role=worker）→ 缓存
`~/.lan_mesh/skills_cache/` → AgentRuntime 读取缓存构建 system prompt。

**技能文件结构**: `skills/{skill_id}/SKILL.md`（含 YAML front matter）+
可选 reference.md。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建 |


## Worker PM 启动（iter-46）

WorkerAgent.start_pm 使用 pm_start.py 共用门控与持久化启动回执，不再自行生成与 Secretary 不一致的 PM 编号。启动前经带节点认证的 GET 查询 Secretary 任务当前归属和状态；启动端点在线程池内执行，避免回查和启动阻塞事件循环。重复启动返回原回执，进程变化后的旧启动禁止自动重新执行；pm-status 返回 task_id，供响应丢失后的只读核实。详见 03 域 PM 启动章节。

## iter-48：Worker 启动记录查询

Worker 的 `inspect_pm_start` 读取 `worker-pm-starts.sqlite3`，与 Station 使用相同 PM 核实逻辑。查询端点 `/role/pm-starts/{pm_id}` 必须携带 task_id，并在线程池中读取。当前 PM 已替换或进程重启时，历史成功启动只返回结果未知，不自动重建 Agent。

## iter-49：按身份停止 PM

Worker `control_pm` 与 Station 共用停止验证器，不再对无身份请求直接操作当前 PM。`/role/pm-control`、旧 stop/cancel/pause 路径均校验节点和任务身份。停止确认表示 PM 设置停止信号；正在运行的工具不强制终止，晚到执行结果由 PM 停止边界和 Secretary 事务隔离。PM 状态查询携带当前实例的控制回执以恢复丢失响应。

## 执行回执只读查询（iter-53）

`read_execution_receipt` 以只读 SQLite 查询既有执行记录；GET `/tasks/executions/{execution_id}` 不再调用创建存储的 get_store。缺失库/表/记录返回404，不能据此判断原请求未执行。POST仍先持久化claim再运行；同进程未完成回执为started，进程变化为unknown。

## 恢复中的执行边界（iter-54）

恢复后的 PM 不重新派发已有执行编号。远端轮询先核对原设备身份，再读取原回执；缺失不当作未执行。本地已开始但无返回结果拒绝续跑。新工具调用前重新验证恢复授权、停止意图和原时限；已有在途工具不强制终止。Station 与 Worker 共用恢复实现，真实本机 HTTP 验证原回执到依赖步骤的交付链路，实际模型仍模拟。
