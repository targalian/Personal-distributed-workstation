# Work Station

分布式个人 AI 工作站 — 将局域网内多台异构主机组成统一调度网格，由项目经理 Agent (PM Agent) 驱动任务自动拆解、团队组建、分布式执行与结果聚合，通过 Web 端与秘书对话管理全局。

## 项目结构

```
work_station/
├── lan_mesh/                  # Python/FastAPI 分布式 AI Agent 网格
│   ├── web/                   # Web UI 仪表盘 (深色主题, 7 Tab 面板)
│   │   ├── templates/dashboard.html
│   │   └── static/
│   ├── station_controller.py  # Station Director 主控制器 (Secretary/Worker 统一入口)
│   ├── station_api.py         # Secretary 侧 API (任务/PM/团队/聊天)
│   ├── station_director.py    # 主机管理与评级
│   ├── worker.py              # Worker 守护进程 (含 PM Agent 内嵌支持)
│   ├── pm_agent.py            # 项目经理 Agent (任务分解/团队组建/进度管理/结果聚合)
│   ├── chat_handler.py        # 秘书聊天处理器 (Web 对话 + 状态注入 + 意图检测)
│   ├── agent_prompt.py        # 子 Agent 通用 Prompt 模板与定制构建器
│   ├── agent_runtime.py       # Agent 运行时 (多 Provider LLM + 技能路由 + 定制 prompt)
│   ├── agent_card.py          # Agent 能力声明卡
│   ├── model_router.py        # 模型路由器 (L1-L4 难度分级 + 加权评分)
│   ├── model_pool.yaml        # 模型池配置
│   ├── project.py             # 项目管理与预算控制
│   ├── mcp_gateway.py         # MCP 工具网关
│   ├── mcp_client.py          # MCP 客户端
│   ├── orchestrator.py        # (已废弃, 仅留任务分类工具函数, 被 PM Agent 替代)
│   ├── host_info.py           # 主机硬件信息采集
│   ├── host_rating.py         # 主机评级 (S/A/B/C/D)
│   ├── shared_folder.py       # 共享文件夹管理
│   ├── discovery.py           # UDP 广播发现
│   ├── database.py            # SQLite 持久化 (主机/任务/PM/团队/进度)
│   ├── protocol.py            # 数据模型与协议定义
│   ├── config.py              # Pydantic 配置加载
│   ├── tool_registry.py       # 工具注册表
│   ├── preflight.py           # 启动前自检
│   └── api.py                 # FastAPI 路由层 (Worker API)
├── quicklan-main/             # Tauri/React 局域网文件共享桌面应用
├── scripts/                   # 跨平台一键启动脚本
│   ├── start_workstation.bat  # Windows 双击启动
│   ├── start_workstation.ps1  # PowerShell 启动 (支持参数)
│   └── start_workstation.sh   # Linux/Mac 启动
├── main.py                    # 统一启动入口
├── config.yaml                # 运行配置
└── requirements.txt           # Python 依赖
```

## 核心能力

### PM Agent 驱动的任务编排
- Boss 通过 Web 端提交任务 → Secretary 在合适 work_station 上注册 PM Agent
- PM 使用 multi-agent-architect skill 分析任务复杂度，决策团队架构
- 自主分解任务为子任务列表，梳理依赖关系（DAG 拓扑排序）
- 在合适 work_station 上创建子 Agent 或团队，分配定制化 system prompt
- 简单任务 PM 自行完成，复杂任务组建团队分发

### 子 Agent Prompt 定制体系
- **通用模板**：所有子 Agent 共享基础准则（身份、工作规范、进度上报协议、自检要求）
- **角色模板**：7 种技能类型各有角色名、职责、质量标准
- **动态生成**：PM 按任务类型、团队结构、依赖关系为每个子 Agent 生成定制 prompt
- **运行时更新**：PM 可在任务执行中途通过 `/pm/update-prompt` 动态调整子 Agent prompt

### 六项生产级优化
1. **依赖感知拓扑分发** — 前序任务完成后自动注入结果到后续任务 input_data
2. **PM 动态调整 Prompt** — Worker 新增端点，PM 可中途纠偏/补上下文/调策略
3. **技能选择性注入** — AgentRuntime 按 required_skill 只加载匹配技能，减少 token 占用
4. **PM 结果聚合** — 全部子任务完成后 LLM 按依赖顺序聚合为最终交付物
5. **失败接管策略** — 子 Agent 失败后三级策略：同站重试 → 换站重试 → PM 本地接管
6. **子 Agent 自检** — 完成时附带 self_check 字段，PM 验证后才确认

### 秘书聊天接口
- Web 端聊天窗口，Boss 直接与秘书对话
- 秘书注入工作站状态上下文（在线主机数、活跃 PM、进行中任务）
- 意图检测：提交任务、启动/停止秘书、查询状态/进度/主机/任务

### 分布式主机管理
- Station Director 统一管理，UDP 广播自动发现，HTTP 心跳注册
- 主机硬件评级系统（S/A/B/C/D），任务分发按评级排序选择最优节点
- Worker 自动采集本机配置（CPU/内存/磁盘/OS/网络）

### 模型路由器
- **L1-L4 难度分级**：规则驱动，按文本长度、关键词、技能类型自动判定
- **加权评分算法**：`Score = 能力覆盖率×0.4 + 成本反向×0.3 + 速度×0.2 - 负载×0.1`
- **降级链容灾**：首选模型失败时自动沿 Fallback Chain 重试
- **多 Provider 支持**：DeepSeek / OpenAI / Anthropic / Qwen / 阿里云 Token Plan

### 项目隔离与预算管控
- 每个项目独立工作空间、预算配额、模型白名单
- Token 用量自动计量，成本实时追踪
- 预算超支自动暂停或切换经济模型

### Web UI 仪表盘
- 深色主题，7 个 Tab 面板：
  - **Work Station 监控** — 主机列表、评级、资源使用率
  - **任务管理** — 任务提交、状态追踪、PM Agent 分配信息
  - **秘书对话** — 与秘书聊天窗口（状态感知 + 意图检测）
  - **团队管理** — PM 及团队树形展示（所在 station、状态、进度报告）
  - **Agent 状态** — Agent Card、技能、工具
  - **MCP 工具** — 工具列表与配置
  - **项目管理** — 项目隔离、预算、消费记录
- WebSocket 实时推送（心跳、任务变更、PM 注册、进度报告、聊天回复）

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据校验 | Pydantic v2 |
| 配置管理 | PyYAML + Pydantic |
| 持久化 | SQLite |
| LLM 调用 | requests（OpenAI 兼容协议） |
| 桌面应用 | Tauri + React + TypeScript |
| 发现协议 | UDP 广播 |
| 通信协议 | HTTP REST + WebSocket |
| 多智能体决策 | multi-agent-architect skill (10 步决策框架) |

## 快速开始

### 一键启动（推荐）

**Windows 双击启动：**

双击 `scripts/start_workstation.bat` 即可。脚本自动完成：检查 Python → 创建虚拟环境 → 安装依赖 → 复制配置 → 启动 Station Director。

**PowerShell 启动（支持参数）：**

```powershell
# 基本启动
.\scripts\start_workstation.ps1

# 指定端口和名称
.\scripts\start_workstation.ps1 -Port 8080 -Name "控制中心"

# 同时启动本地 Worker (后台)
.\scripts\start_workstation.ps1 -WithWorker
```

**Linux/Mac 启动：**

```bash
bash scripts/start_workstation.sh

# 指定端口和名称
bash scripts/start_workstation.sh --port 8080 --name "Control Center"

# 同时启动本地 Worker
bash scripts/start_workstation.sh --with-worker
```

启动后打开 `http://localhost:45470` 进入 Web UI，点击「设为主节点」激活 Secretary。

### 手动安装

```powershell
python -m venv .venv
.venv\Scripts\Activate
pip install -r requirements.txt
```

### 配置模型池

复制模板并填入 API Key 环境变量名：

```powershell
Copy-Item lan_mesh\model_pool.example.yaml lan_mesh\model_pool.yaml
```

设置环境变量（按需）：

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:OPENAI_API_KEY = "sk-xxx"
$env:ALIYUN_TOKENPLAN_API_KEY = "你的TokenPlan专属Key"
```

### 启动节点

```powershell
# Station Director (主节点, 含 Web UI)
python main.py station

# Worker (工作节点)
python main.py worker
```

### 在其他主机上启动 Worker

在其他局域网主机上重复上述安装步骤，然后运行：

```powershell
python main.py worker --name "计算节点-01"
```

Worker 自动发现 Station Director 并注册。

## 端口说明

| 端口 | 协议 | 用途 |
|------|------|------|
| 45454 | UDP | 设备发现广播 |
| 45460 | TCP | Worker HTTP API |
| 45470 | TCP | Station Director HTTP API + Web UI |

## API 概览

### 任务管理
- `POST /api/tasks` — 提交任务（自动选择 work_station、注册 PM Agent）
- `GET /api/tasks` — 任务列表
- `GET /api/tasks/{id}` — 任务详情

### PM Agent 管理
- `GET /api/pm` — 列出所有 PM Agent
- `GET /api/pm/{pm_id}` — PM 详情（含团队结构 + 进度）
- `GET /api/pm/{pm_id}/teams` — PM 下属团队
- `GET /api/pm/{pm_id}/progress` — PM 进度报告
- `POST /api/pm/{pm_id}/status` — PM 上报状态（Worker 调用）
- `POST /api/pm/{pm_id}/progress` — PM 上报进度（Worker 调用）

### 团队管理
- `GET /api/teams` — 所有团队
- `GET /api/teams/{team_id}` — 团队详情

### 秘书聊天
- `POST /api/secretary/chat` — 发送消息（{message} → {reply, action_taken}）
- `GET /api/secretary/chat/history` — 对话历史
- `DELETE /api/secretary/chat/history` — 清空对话历史（内存 + DB）

### Worker PM 端点（PM Agent 调用）
- `POST /role/start-pm` — 在 Worker 上启动 PM Agent
- `POST /role/stop-pm` — 停止 PM Agent
- `GET /role/pm-status` — PM Agent 运行状态
- `POST /pm/create-subagent` — 创建子 Agent（含定制 system_prompt）
- `POST /pm/update-prompt` — 动态更新子 Agent prompt
- `POST /pm/progress-report` — 子 Agent 向 PM 上报进度
- `GET /pm/subagents` — 子 Agent 列表

### 项目管理
- `POST /api/projects` — 创建项目（含预算、模型白名单、路由策略）
- `GET /api/projects` — 列出所有项目
- `GET /api/projects/{id}/usage` — 查看消费记录

### 模型路由
- `POST /api/route/dry-run` — 路由决策预览
- `GET /api/models` — 模型池列表

### 实时通信
- `WS /ws` — WebSocket 推送（主机状态、任务变更、PM 注册、进度报告、聊天回复、团队更新）

## 运行流程

```
Boss 提交任务 (Web UI)
  → Secretary 选择最优 work_station (按评级排序)
  → POST /role/start-pm → Worker 创建 PM Agent
  → PM 分析任务 (multi-agent-architect skill + LLM)
  → PM 决策: 简单任务自执行 / 复杂任务组建团队
  → PM 创建子 Agent (含定制 prompt) + 按依赖拓扑分发
  → 子 Agent 执行 + 阶段进度上报
  → PM 收集进度 + 依赖完成后自动分发后续任务
  → 全部完成 → PM LLM 聚合结果 → 上报 Secretary
  → Web UI 实时展示 PM/团队/进度
```

## 路线图

| 阶段 | 状态 | 内容 |
|------|------|------|
| Phase 1 基建层 | ✅ | FastAPI Worker、UDP 发现、心跳注册 |
| Phase 2 路由器 | ✅ | 难度分类器、加权评分路由、降级链 |
| Phase 3 项目隔离 | ✅ | 项目目录隔离、预算计数器、消费追踪 |
| Phase 4 工作流编排 | ✅ | 预设模板（代码/文档/系统任务） |
| Phase 5 仪表盘 | ✅ | 7 Tab 多面板仪表盘、WebSocket 实时推送 |
| Phase 6 PM Agent | ✅ | 项目经理 Agent、团队组建、子 Agent prompt 定制、聊天接口 |
| Phase 7 生产级优化 | ✅ | 依赖感知分发、动态 prompt、选择性注入、结果聚合、失败接管、自检 |
| Phase 8 增强 | 🔄 | 子 Agent 间直接通信、语义缓存、技能市场 |

## License

MIT
