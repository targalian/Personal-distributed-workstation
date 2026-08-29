# Agent 能力声明系统

<cite>
**本文引用的文件**
- [agent_card.py](file://lan_mesh/agent_card.py)
- [agent_runtime.py](file://lan_mesh/agent_runtime.py)
- [skill_registry.py](file://lan_mesh/skill_registry.py)
- [protocol.py](file://lan_mesh/protocol.py)
- [host_info.py](file://lan_mesh/host_info.py)
- [worker.py](file://lan_mesh/worker.py)
- [station_api.py](file://lan_mesh/station_api.py)
- [api.py](file://lan_mesh/api.py)
- [discovery.py](file://lan_mesh/discovery.py)
- [shared_folder.py](file://lan_mesh/shared_folder.py)
- [tool_registry.py](file://lan_mesh/tool_registry.py)
- [task.py](file://lan_mesh/task.py)
- [config.py](file://lan_mesh/config.py)
</cite>

## 更新摘要
**变更内容**
- 新增技能管理系统：引入 SkillRegistry 中央技能管理与分发系统
- AgentRuntime 增强：支持通过 _skills_cache 目录自动扫描和构建系统提示
- Worker 技能拉取：新增技能缓存机制，支持远程技能分发
- 技能 API 端点：新增完整的技能管理 REST API
- 技能文件格式：支持 YAML front matter 的技能描述格式

## 目录
1. [简介](#简介)
2. [项目结构](#项目结构)
3. [核心组件](#核心组件)
4. [架构总览](#架构总览)
5. [详细组件分析](#详细组件分析)
6. [依赖关系分析](#依赖关系分析)
7. [性能考量](#性能考量)
8. [故障排查指南](#故障排查指南)
9. [结论](#结论)
10. [附录](#附录)

## 简介
本文件面向 Agent 能力声明系统，围绕 AgentCard 的设计理念与结构展开，解释技能（Skill）与工具（ToolDef）的定义方式；详解 generate_agent_card 如何根据主机环境动态生成能力声明（包含 IP 地址、API 端口、主机名等元数据）；阐述 AgentRuntime 的运行时管理机制（任务执行、状态跟踪、结果返回）；详细介绍新增的技能管理系统，包括技能注册、权限分配、远程分发和本地缓存机制；并提供 AgentCard 的 JSON 模式与实际生成示例，展示不同环境下的能力声明差异。

## 项目结构
系统采用模块化设计，核心围绕 Worker/Master 的发现与注册、AgentCard 能力声明、AgentRuntime 任务执行、技能管理系统、工具注册表与任务 DAG 管理展开。关键模块如下：
- 协议与数据模型：定义 AgentCard、Skill、ToolDef、HostInfo、SubTask、Task 等数据结构
- 主机信息采集：自动收集 CPU/内存/磁盘/网络等硬件与运行时信息
- Agent 能力声明：生成 AgentCard，包含技能与工具清单
- Agent 运行时：执行子任务，路由到对应技能处理器，支持 LLM 注入技能知识
- 技能管理系统：中央技能注册表，支持技能扫描、权限分配、远程分发
- 工具注册表：内置工具与插件化工具管理
- 任务 DAG：子任务依赖与拓扑排序
- 发现与 API：UDP 广播发现、Worker/Master HTTP API
- 共享文件夹：跨主机文件共享与配置报告

```mermaid
graph TB
subgraph "协议与模型"
P["protocol.py<br/>AgentCard/Skill/ToolDef/HostInfo/SubTask/Task"]
end
subgraph "主机信息"
HI["host_info.py<br/>采集 CPU/内存/磁盘/网络/运行时"]
end
subgraph "能力声明"
AC["agent_card.py<br/>generate_agent_card/默认技能/工具"]
end
subgraph "运行时"
AR["agent_runtime.py<br/>执行子任务/路由/LLM调用/技能缓存"]
end
subgraph "技能管理"
SR["skill_registry.py<br/>技能扫描/权限分配/远程分发"]
end
subgraph "工具注册"
TR["tool_registry.py<br/>内置工具/插件/执行调度"]
end
subgraph "任务管理"
TD["task.py<br/>TaskDAG/拓扑排序/状态更新"]
end
subgraph "发现与API"
DS["discovery.py<br/>UDP广播/监听/清理"]
API["api.py<br/>Worker/Master路由/WS推送"]
STA["station_api.py<br/>技能管理API/角色分配"]
end
subgraph "共享文件夹"
SF["shared_folder.py<br/>文件共享/配置报告"]
end
subgraph "入口"
WK["worker.py<br/>Worker启动/注册/心跳/技能拉取"]
MK["station_controller.py<br/>Master启动/注册/清理/WebUI"]
end
HI --> AC
AC --> WK
WK --> API
MK --> API
DS --> WK
DS --> MK
API --> AR
TR --> AR
TD --> API
SF --> WK
SF --> MK
SR --> STA
STA --> WK
AR --> SR
```

**图表来源**
- [protocol.py:159-298](file://lan_mesh/protocol.py#L159-L298)
- [host_info.py:129-191](file://lan_mesh/host_info.py#L129-L191)
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)
- [skill_registry.py:43-388](file://lan_mesh/skill_registry.py#L43-L388)
- [tool_registry.py:217-338](file://lan_mesh/tool_registry.py#L217-L338)
- [task.py:16-91](file://lan_mesh/task.py#L16-L91)
- [discovery.py:33-259](file://lan_mesh/discovery.py#L33-L259)
- [api.py:39-649](file://lan_mesh/api.py#L39-L649)
- [station_api.py:623-714](file://lan_mesh/station_api.py#L623-L714)
- [shared_folder.py:16-219](file://lan_mesh/shared_folder.py#L16-L219)
- [worker.py:62-325](file://lan_mesh/worker.py#L62-L325)
- [station_controller.py](file://lan_mesh/station_controller.py#L67-L324)

**章节来源**
- [protocol.py:159-298](file://lan_mesh/protocol.py#L159-L298)
- [host_info.py:129-191](file://lan_mesh/host_info.py#L129-L191)
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)
- [skill_registry.py:43-388](file://lan_mesh/skill_registry.py#L43-L388)
- [tool_registry.py:217-338](file://lan_mesh/tool_registry.py#L217-L338)
- [task.py:16-91](file://lan_mesh/task.py#L16-L91)
- [discovery.py:33-259](file://lan_mesh/discovery.py#L33-L259)
- [api.py:39-649](file://lan_mesh/api.py#L39-L649)
- [station_api.py:623-714](file://lan_mesh/station_api.py#L623-L714)
- [shared_folder.py:16-219](file://lan_mesh/shared_folder.py#L16-L219)
- [worker.py:62-325](file://lan_mesh/worker.py#L62-L325)
- [station_controller.py](file://lan_mesh/station_controller.py#L67-L324)

## 核心组件
- AgentCard：借鉴 A2A 协议的 Agent 能力卡片，包含 agent_id、agent_name、version、宿主信息（device_id、hostname、ip、api_port）、能力声明（skills、tools、model_preferences、max_concurrent_tasks）、运行时状态（status、current_task_count、registered_at、last_seen）
- Skill：技能声明，包含 name、description、input_schema（JSON Schema）、tags、category、default_access、version
- ToolDef：工具定义，包含 name、description、mcp_compatible、input_schema（JSON Schema）
- AgentRuntime：Worker 端运行时，负责接收子任务、按 required_skill 路由到对应处理器、执行 LLM API 或本地操作、返回结果与 token 使用统计，支持通过 _skills_cache 目录自动构建系统提示
- SkillRegistry：中央技能注册表，管理技能注册、权限分配、内容分发，支持技能扫描、权限管理和远程分发
- ToolRegistry：工具注册表，支持内置工具、YAML 插件工具、运行时动态注册
- TaskDAG：子任务有向无环图，支持依赖声明、拓扑排序、就绪任务筛选、循环依赖检测
- DiscoveryService：UDP 广播发现，负责广播自身存在、监听其他设备、TTL 清理
- Worker/Master：Worker 自动注册到 Master，提供 HTTP API；Master 维护主机与 Agent 注册表、提供 Web UI 与任务编排

**章节来源**
- [protocol.py:161-234](file://lan_mesh/protocol.py#L161-L234)
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)
- [skill_registry.py:43-388](file://lan_mesh/skill_registry.py#L43-L388)
- [tool_registry.py:217-338](file://lan_mesh/tool_registry.py#L217-L338)
- [task.py:16-91](file://lan_mesh/task.py#L16-L91)
- [discovery.py:33-259](file://lan_mesh/discovery.py#L33-L259)
- [worker.py:62-325](file://lan_mesh/worker.py#L62-L325)
- [station_controller.py](file://lan_mesh/station_controller.py#L67-L324)

## 架构总览
Agent 能力声明系统的核心流程：
- Worker 启动后采集主机信息，生成 AgentCard，并通过 HTTP 注册到 Master
- Master 接收 Worker 注册与心跳，维护 HostRecord 与 AgentCard
- Master 通过 API 接口提交任务，任务分解为子任务并构建 TaskDAG
- Master 根据 AgentCard 的 skills/tools 与子任务 required_skill 进行匹配与分发
- Worker 的 AgentRuntime 接收子任务，路由到对应技能处理器执行
- 技能管理系统支持远程技能分发，Worker 拉取已授权技能到本地缓存
- AgentRuntime 执行 LLM 技能时，自动从本地技能缓存构建系统提示
- 执行结果返回给 Master，更新任务状态与消费记录

```mermaid
sequenceDiagram
participant W as "Worker"
participant M as "Master"
participant SR as "SkillRegistry"
participant DB as "数据库"
participant RT as "AgentRuntime"
participant LLM as "LLM API"
W->>M : "POST /api/register (HostInfo)"
M->>DB : "upsert_host(HostRecord)"
W->>M : "POST /api/agents/register (AgentCard)"
M->>DB : "upsert_agent(AgentCard)"
M->>DB : "submit_task(Task) -> TaskDAG(SubTasks)"
M->>W : "POST /tasks/execute (SubTask)"
W->>SR : "GET /api/station/skills/download"
SR-->>W : "技能包 (content/reference)"
W->>W : "缓存到 ~/.lan_mesh/skills_cache/"
W->>RT : "execute(subtask)"
alt required_skill in [code_generation, code_review, document_summary]
RT->>RT : "_build_system_prompt()"
RT->>LLM : "_call_llm_with_routing(prompt)"
LLM-->>RT : "content, model, usage"
else shell_exec/file_ops/monitoring
RT->>RT : "_handle_*"
end
RT-->>W : "{output, status, usage}"
W-->>M : "result"
M->>DB : "update_subtask/status"
```

**图表来源**
- [worker.py:126-171](file://lan_mesh/worker.py#L126-L171)
- [station_api.py:643-646](file://lan_mesh/station_api.py#L643-L646)
- [agent_runtime.py:47-96](file://lan_mesh/agent_runtime.py#L47-L96)
- [agent_runtime.py:193-275](file://lan_mesh/agent_runtime.py#L193-L275)
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)

## 详细组件分析

### AgentCard 设计理念与结构
- 设计理念：借鉴 A2A 协议的 Agent Card，强调"能力声明"与"可发现性"。每个 Worker 启动时生成 AgentCard 并注册到 Master，Master 依据技能与工具清单进行任务匹配与分发
- 结构组成：
  - 宿主信息：device_id、hostname、ip、api_port
  - 能力声明：skills（技能列表）、tools（工具列表）、model_preferences（模型偏好）、max_concurrent_tasks（最大并发）
  - 运行时状态：status、current_task_count、registered_at、last_seen
- JSON 模式：AgentCard 为 dataclass，支持 to_dict/from_dict，便于序列化为 JSON

**章节来源**
- [protocol.py:202-234](file://lan_mesh/protocol.py#L202-L234)
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)

### 技能（Skill）与工具（ToolDef）定义
- Skill：描述一类可执行的任务，包含 name、description、input_schema（JSON Schema）、tags、category、default_access、version
- ToolDef：描述可调用的外部工具，包含 name、description、mcp_compatible、input_schema（JSON Schema）
- 默认技能与工具：系统内置一组通用技能与工具，支持代码生成/审查/摘要、RAG 检索预留、Shell 执行、文件读写、HTTP 请求等

**章节来源**
- [protocol.py:161-193](file://lan_mesh/protocol.py#L161-L193)
- [agent_card.py:18-111](file://lan_mesh/agent_card.py#L18-L111)
- [agent_card.py:115-162](file://lan_mesh/agent_card.py#L115-L162)

### generate_agent_card 动态生成能力声明
- 输入参数：device_id、agent_name、ip、api_port、hostname、skill_names、tool_names、model_preferences、max_concurrent_tasks
- 动态选择：若传入 skill_names/tool_names，则仅包含指定项；否则使用默认集合
- 输出：AgentCard 实例，包含 skills/tools 的 JSON 序列化形式
- 元数据来源：ip 来自主机信息采集，hostname 来自主机名，api_port 来自 Worker 启动时端口分配

```mermaid
flowchart TD
Start(["开始: generate_agent_card"]) --> CheckSkills["是否指定 skill_names?"]
CheckSkills --> |是| UseSelectedSkills["使用指定技能集合"]
CheckSkills --> |否| UseDefaultSkills["使用默认技能集合"]
UseSelectedSkills --> CheckTools["是否指定 tool_names?"]
UseDefaultSkills --> CheckTools
CheckTools --> |是| UseSelectedTools["使用指定工具集合"]
CheckTools --> |否| UseDefaultTools["使用默认工具集合"]
UseSelectedTools --> BuildCard["构造 AgentCard"]
UseDefaultTools --> BuildCard
BuildCard --> ReturnCard["返回 AgentCard"]
```

**图表来源**
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)

**章节来源**
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)
- [host_info.py:129-191](file://lan_mesh/host_info.py#L129-L191)

### AgentRuntime 运行时管理机制
- 任务执行：execute 接收子任务，按 required_skill 路由到对应处理器
- 技能处理器：
  - code_generation/code_review/document_summary：调用 LLM API（优先 DeepSeek，其次 OpenAI），返回内容与 token 使用
  - rag_search：预留接口，当前返回提示
  - shell_exec：执行 Shell 命令，支持超时
  - file_ops：文件读写、列出、删除
  - monitoring：采集 CPU/内存/磁盘使用率与时间戳
- LLM API 调用：优先使用环境变量配置的 DEEPSEEK_API_KEY 或 OPENAI_API_KEY
- 技能缓存：支持从本地 _skills_cache 目录自动扫描技能文件，构建系统提示
- 结果返回：统一返回 {output, status, usage/error}

```mermaid
classDiagram
class AgentRuntime {
+agent_id : str
+shared_folder : str
+_skills_cache_dir : Path
-_handlers : dict
+execute(subtask) dict
-_handle_code_generation(input_data) dict
-_handle_code_review(input_data) dict
-_handle_document_summary(input_data) dict
-_handle_rag_search(input_data) dict
-_handle_shell_exec(input_data) dict
-_handle_file_ops(input_data) dict
-_handle_monitoring(input_data) dict
-_build_system_prompt(task_context) str
-_call_llm_with_routing(prompt, input_data) dict
-_call_deepseek(prompt, api_key) dict
-_call_openai(prompt, api_key) dict
}
```

**图表来源**
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)

**章节来源**
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)

### 技能管理系统（SkillRegistry）
- 中央技能管理：部署在 Station Director 端，是技能库的唯一权威来源
- 技能扫描：自动扫描 skills/ 目录，解析每个子目录的 SKILL.md front matter，注册新技能或更新元数据
- 权限分配：支持按角色、Agent、主机分配技能访问权限
- 内容分发：Worker 通过 HTTP API 拉取已授权技能到本地缓存
- 技能文件格式：支持 YAML front matter，包含 name、description、category、tags、default_access、version 等元数据
- 远程分发：支持按角色或 Agent ID 过滤技能包，实现细粒度权限控制

**章节来源**
- [skill_registry.py:43-388](file://lan_mesh/skill_registry.py#L43-L388)

### 工具注册表（ToolRegistry）
- 支持三种注册方式：内置工具、YAML 插件工具、运行时动态注册
- 内置工具：file_read、file_write、shell_exec、http_request、dir_list、python_eval
- 执行调度：call_tool 根据工具名调用对应处理器，返回 MCP 兼容格式
- 插件加载：load_plugins 从 YAML 配置导入模块与函数，动态注册 ToolDef 与处理器

**章节来源**
- [tool_registry.py:217-338](file://lan_mesh/tool_registry.py#L217-L338)

### 任务 DAG 管理（TaskDAG）
- 子任务依赖建模：邻接表与入度数组
- 拓扑排序：Kahn 算法，检测循环依赖
- 就绪任务：当前状态为 pending 且所有前置依赖已完成
- 状态更新：update_subtask 支持批量更新子任务属性

**章节来源**
- [task.py:16-91](file://lan_mesh/task.py#L16-L91)

### 发现与 API（Worker/Master）
- Worker：启动时创建 DiscoveryService，广播自身存在；注册 HostInfo 与 AgentCard；周期性发送心跳；提供 /info、/tasks/execute、/shared 等 API；支持技能拉取
- Master：维护 HostRecord 与 AgentCard；提供 /api/register、/api/heartbeat、/api/agents、/api/tasks、/api/projects、/tools/* 等 API；WebSocket 推送状态变化
- 技能 API：提供技能扫描、权限分配、内容分发等完整技能管理接口

**章节来源**
- [worker.py:62-325](file://lan_mesh/worker.py#L62-L325)
- [station_controller.py](file://lan_mesh/station_controller.py#L67-L324)
- [api.py:39-649](file://lan_mesh/api.py#L39-L649)
- [station_api.py:623-714](file://lan_mesh/station_api.py#L623-L714)
- [discovery.py:33-259](file://lan_mesh/discovery.py#L33-L259)

## 依赖关系分析
- 协议层：AgentCard/Skill/ToolDef/HostInfo/SubTask/Task 作为跨模块的数据契约
- Worker 依赖：host_info（采集）、agent_card（生成 AgentCard）、agent_runtime（执行）、api（HTTP 路由）、discovery（发现）、shared_folder（共享）、skill_registry（技能管理）
- Master 依赖：discovery（发现）、database（持久化）、api（HTTP 路由）、orchestrator（任务编排）、project（项目管理）、mcp_gateway（工具网关）、skill_registry（技能管理）

```mermaid
graph LR
Protocol["protocol.py"] --> Worker["worker.py"]
Protocol --> Master["station_controller.py"]
Protocol --> AgentRuntime["agent_runtime.py"]
Protocol --> SkillRegistry["skill_registry.py"]
Protocol --> ToolRegistry["tool_registry.py"]
Protocol --> TaskDAG["task.py"]
HostInfo["host_info.py"] --> Worker
HostInfo --> Master
AgentCard["agent_card.py"] --> Worker
AgentCard --> Master
Discovery["discovery.py"] --> Worker
Discovery --> Master
SharedFolder["shared_folder.py"] --> Worker
SharedFolder --> Master
API["api.py"] --> Worker
API --> Master
StationAPI["station_api.py"] --> Master
SkillRegistry --> StationAPI
SkillRegistry --> Worker
AgentRuntime --> Worker
```

**图表来源**
- [protocol.py:159-298](file://lan_mesh/protocol.py#L159-L298)
- [worker.py:62-325](file://lan_mesh/worker.py#L62-L325)
- [station_controller.py](file://lan_mesh/station_controller.py#L67-L324)
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)
- [skill_registry.py:43-388](file://lan_mesh/skill_registry.py#L43-L388)
- [tool_registry.py:217-338](file://lan_mesh/tool_registry.py#L217-L338)
- [task.py:16-91](file://lan_mesh/task.py#L16-L91)
- [host_info.py:129-191](file://lan_mesh/host_info.py#L129-L191)
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)
- [discovery.py:33-259](file://lan_mesh/discovery.py#L33-L259)
- [shared_folder.py:16-219](file://lan_mesh/shared_folder.py#L16-L219)
- [api.py:39-649](file://lan_mesh/api.py#L39-L649)
- [station_api.py:623-714](file://lan_mesh/station_api.py#L623-L714)

**章节来源**
- [protocol.py:159-298](file://lan_mesh/protocol.py#L159-L298)
- [worker.py:62-325](file://lan_mesh/worker.py#L62-L325)
- [station_controller.py](file://lan_mesh/station_controller.py#L67-L324)
- [agent_runtime.py:28-357](file://lan_mesh/agent_runtime.py#L28-L357)
- [skill_registry.py:43-388](file://lan_mesh/skill_registry.py#L43-L388)
- [tool_registry.py:217-338](file://lan_mesh/tool_registry.py#L217-L338)
- [task.py:16-91](file://lan_mesh/task.py#L16-L91)
- [host_info.py:129-191](file://lan_mesh/host_info.py#L129-L191)
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)
- [discovery.py:33-259](file://lan_mesh/discovery.py#L33-L259)
- [shared_folder.py:16-219](file://lan_mesh/shared_folder.py#L16-L219)
- [api.py:39-649](file://lan_mesh/api.py#L39-L649)
- [station_api.py:623-714](file://lan_mesh/station_api.py#L623-L714)

## 性能考量
- LLM API 调用：优先使用 DeepSeek，其次 OpenAI，避免重复请求；返回内容包含 token 使用，便于成本控制
- Shell 执行：支持超时控制，防止长时间阻塞
- 文件操作：内置工具与本地文件系统交互，注意路径安全与权限
- 发现与心跳：UDP 广播与 HTTP 心跳间隔合理设置，避免频繁网络开销
- 任务编排：TaskDAG 拓扑排序与就绪任务筛选，减少无效轮询
- 技能缓存：本地技能缓存避免重复网络传输，提高 LLM 调用响应速度
- 技能分发：按角色或 Agent ID 过滤技能包，减少不必要的数据传输

## 故障排查指南
- LLM API 未配置：当未设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY 时，_call_llm_full 返回提示信息，需检查环境变量
- 端口冲突：Worker 启动时会查找可用端口，若端口范围被占用，需调整配置
- 网络发现异常：UDP 端口占用可能导致发现服务降级，检查端口占用与防火墙
- 工具执行错误：ToolRegistry.call_tool 返回 isError 与错误信息，检查工具定义与输入参数
- 任务循环依赖：TaskDAG.has_cycle 检测到环时，需修正子任务依赖关系
- 技能拉取失败：检查 Station Director 端技能扫描是否成功，确认 Worker 能访问 Secretary API
- 技能缓存异常：检查 ~/.lan_mesh/skills_cache 目录权限，确认技能文件格式正确
- 技能权限问题：确认技能分配是否正确，检查角色权限与 Agent ID 映射

**章节来源**
- [agent_runtime.py:172-193](file://lan_mesh/agent_runtime.py#L172-L193)
- [worker.py:240-249](file://lan_mesh/worker.py#L240-L249)
- [discovery.py:159-174](file://lan_mesh/discovery.py#L159-L174)
- [tool_registry.py:259-288](file://lan_mesh/tool_registry.py#L259-L288)
- [task.py:68-70](file://lan_mesh/task.py#L68-L70)
- [station_api.py:643-646](file://lan_mesh/station_api.py#L643-L646)

## 结论
Agent 能力声明系统通过 AgentCard 将 Worker 的技能与工具能力标准化表达，结合 DiscoveryService 与 HTTP API 实现自动发现与注册；AgentRuntime 提供统一的任务执行框架，支持 LLM API 与本地操作，增强的技能缓存机制使 LLM 能够注入最新的技能知识；SkillRegistry 提供完整的技能管理能力，支持技能扫描、权限分配与远程分发；ToolRegistry 与 TaskDAG 进一步增强了工具扩展性与任务编排能力。整体架构清晰、模块解耦良好，适合在局域网环境中进行分布式任务协作与能力匹配。

## 附录

### AgentCard JSON 模式与字段说明
- agent_id：Agent 唯一标识
- agent_name：Agent 名称
- version：AgentCard 版本
- device_id：宿主设备 ID
- hostname：主机名
- ip：本机 IP 地址
- api_port：HTTP API 端口
- skills：技能列表（Skill.to_dict）
- tools：工具列表（ToolDef.to_dict）
- model_preferences：模型偏好列表
- max_concurrent_tasks：最大并发任务数
- status：Agent 运行状态（idle/busy/offline）
- current_task_count：当前任务计数
- registered_at/last_seen：注册与最近更新时间戳

**章节来源**
- [protocol.py:202-234](file://lan_mesh/protocol.py#L202-L234)

### generate_agent_card 参数与行为
- device_id：设备 ID（与 Worker 共享）
- agent_name：Agent 名称
- ip：本机 IP
- api_port：HTTP API 端口
- hostname：主机名
- skill_names：启用的技能列表（None 则全部启用）
- tool_names：启用的工具列表（None 则全部启用）
- model_preferences：模型偏好列表（默认 ["deepseek-v3", "gpt-4o-mini"]）
- max_concurrent_tasks：最大并发任务数（默认 5）

**章节来源**
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)

### 技能管理系统 API 端点
- GET /api/station/skills：列出所有已注册技能，可按分类过滤
- GET /api/station/skills/stats：返回技能库统计信息
- GET /api/station/skills/scan：手动触发扫描注册新技能
- GET /api/station/skills/download：Worker 拉取已授权技能包
- GET /api/station/skills/role/{role}：获取角色可用的技能列表
- GET /api/station/skills/{skill_id}：获取技能详情及完整内容
- POST /api/station/skills/{skill_id}/assign：分配技能给角色/Agent/主机
- DELETE /api/station/skills/{skill_id}/assign：撤销技能分配

**章节来源**
- [station_api.py:623-714](file://lan_mesh/station_api.py#L623-L714)

### 技能文件格式与示例
技能文件采用 YAML front matter 格式，包含以下元数据：
- name：技能名称
- description：技能描述
- category：技能分类
- tags：标签列表
- default_access：默认访问权限（如 ["station", "secretary"]）
- version：版本号

**章节来源**
- [skill_registry.py:22-31](file://lan_mesh/skill_registry.py#L22-L31)
- [skills/multi-agent-architect/SKILL.md:1-8](file://lan_mesh/skills/multi-agent-architect/SKILL.md#L1-L8)

### 不同环境下的能力声明差异示例
- 环境 A（具备 LLM API Key）：AgentCard 中包含 code_generation、code_review、document_summary 等技能，且 model_preferences 指定可用模型
- 环境 B（无 LLM API Key）：AgentCard 仍包含相同技能，但 LLM 调用将返回未配置提示
- 环境 C（禁用部分技能/工具）：通过 skill_names/tool_names 指定启用集合，生成的 AgentCard 仅包含选定能力
- 环境 D（技能缓存完整）：AgentRuntime 能够从本地 _skills_cache 目录构建系统提示，提供更丰富的 LLM 上下文
- 环境 E（技能权限受限）：Worker 只能拉取授权的技能，本地缓存中缺少未授权技能内容

**章节来源**
- [agent_card.py:167-217](file://lan_mesh/agent_card.py#L167-L217)
- [agent_runtime.py:172-193](file://lan_mesh/agent_runtime.py#L172-L193)
- [worker.py:181-207](file://lan_mesh/worker.py#L181-L207)
