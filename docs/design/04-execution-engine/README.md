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
**错误追踪埋点** (iter-45, F1.4 数据源): `_call_llm_with_routing` 降级链耗尽时
capture 到 module=`llm` (context 携带失败链), 异常隔离不影响降级返回。

**多机实测加固** (iter-55, 补强#3):
- `PROVIDER_CONFIG` 补 `volcengine-ark` 置首位 (coding/v3 端点 +
  ARK_API_KEY) — 无路由信息路径 (`_call_llm_full`, pm_planner 规划走
  此路径) 不再跳过 default_model 所在 provider
- 补 `_get_default_model(provider)` 定义 (全库缺失的 AttributeError
  隐患, defaults 含 ark-code-latest 兜底)
- `_ensure_env_loaded` 重写: key_envs 补 ARK_API_KEY; 不再因「部分 key
  已有值」提前 return (否则仅 aliyun key 环境跳过 .env 加载致 ark
  key 缺失); dotenv 缺失时手动解析 .env 兜底
- `main.py` 启动 dotenv ImportError 时同样手动解析兜底 (基础解释器
  无 python-dotenv 场景, 否则 Key 全部缺失)

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

## PM 执行态快照与断点恢复 (iter-53)

**背景**: 修复 multi 模式下聚合永不触发的真实缺陷 — 原先 `_run_task`
finally 无条件停 `running`, 而 `aggregate_results` 只在 progress_loop
中触发, 多子任务任务分发后 progress_loop 10s 内退出、聚合/交付
永不执行; 同时补齐 PM 重启断点恢复 (补强评估缺口 1)。

**快照序列化 (pm_state.py)**:
- `PMState.to_snapshot()`: 16 字段序列化 (plan/task/subtask_outputs/
pending/dispatched/task_station/task_agent/teams/subagents/retry/
超时/启动时间/clarification_question, 线程安全)
- `restore_from()`: 就地恢复 — 只重写字段不替换对象, 保持
planner/dispatcher/monitor 的共享引用有效 (resume 关键约束)

**快照写点 (pm_agent.py)**:
| 阶段 | 写点 |
|---|---|
| planning_done | 规划完成 |
| monitoring | 分发完成 (multi 模式保持 running 等聚合) |
| executing | 子任务结果注入 |
| awaiting_input | 澄清等待 (携带 clarification_question) |
| paused | 暂停 |

快照经 HTTP POST 到 Secretary `/api/pm/{pm_id}/snapshot` 落库
(pm_snapshots 表 UPSERT 一 PM 一快照, 异常静默降级); 完成/失败/取消
时清除; 聚合收尾 (pm_monitor.aggregate_results 末尾) 清快照 + 停 running。

**断点续跑 (resume_from_snapshot → _run_resumed)**: 解析快照就地恢复后按四
场景执行 — 澄清等待重发问题; 无分解标记失败; 全部完成直接聚合; 部分完成
保留已完成输出、重分发未完成 (依赖未满足挂回 pending, 远端子 Agent 随进程
消失不能等回报)。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-29 | iter-55 | 多机实测加固 (补强#3): PROVIDER_CONFIG 补 volcengine-ark 置首位; _get_default_model 补齐定义; _ensure_env_loaded 重写 (ARK key + 部分 key 不再提前 return + dotenv 缺失手动解析); main.py dotenv 兜底 |
| 2026-08-28 | iter-53 | PM 执行态快照持久化 + 断点恢复: PMState 序列化/就地恢复 + 六阶段快照写点 + resume_from_snapshot/_run_resumed 四场景续跑 + multi 模式聚合修复 (_multi_monitoring) |
| 2026-08-27 | iter-45 | agent_runtime 降级链耗尽错误埋点 (module=llm, 携带失败链) |
| 2026-08-16 | iter-27 后 | 初建 |
