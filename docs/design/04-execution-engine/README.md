# 04 执行引擎

Worker 侧的任务执行能力：守护进程、Agent 运行时、能力卡片、Prompt 定制、
工具/MCP/沙箱/技能体系。

## 模块清单

| 模块 | 职责一句话 |
|---|---|
| worker.py | Worker 守护进程: 配置采集/共享目录/发现/注册/心跳 |
| agent_runtime.py | Worker 端任务执行引擎 (LLM 调用/工具执行/结果返回) |
| agent_card.py | Agent Card 能力卡片 (借鉴 A2A 协议) |
| agent_prompt.py | 子 Agent Prompt 模板与定制构建器 |
| tool_registry.py | 插件化工具注册表 (内置 + YAML 插件 + 动态注册) |
| mcp_client.py | MCP 客户端 (JSON-RPC 2.0, stdio/HTTP 双传输) |
| mcp_gateway.py | MCP 网关: 中央工具调度枢纽 (连接池/聚合/路由) |
| sandbox.py | 代码执行沙箱 (F2.2, subprocess 隔离) |
| skill_registry.py | 技能库注册表 (中央管理与分发) |

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
