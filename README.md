# Work Station

分布式个人 AI 工作站 — 将局域网内多台异构主机组成统一调度网格，智能路由外部大模型 API，实现任务自动拆解、分布式执行、项目隔离与预算管控。

## 项目结构

```
work_station/
├── lan_mesh/          # Python/FastAPI 分布式 AI Agent 网格 (Secretary/Worker)
│   ├── web/           # Web UI 仪表盘 (深色主题, Tab 多面板)
│   ├── secretary.py   # 中心控制节点
│   ├── worker.py      # 工作节点守护进程
│   ├── orchestrator.py# 任务编排引擎 (DAG 拆解 + 调度)
│   ├── model_router.py# 模型路由器 (L1-L4 难度分级 + 加权评分)
│   ├── project.py     # 项目管理与预算控制
│   ├── agent_runtime.py# Agent 运行时 (多 Provider LLM 调用)
│   ├── api.py         # FastAPI 路由层 (Secretary/Worker API)
│   ├── mcp_gateway.py # MCP 工具网关
│   └── protocol.py    # 数据模型与协议定义
├── quicklan-main/     # Tauri/React 局域网文件共享桌面应用
├── main.py            # 统一启动入口
├── config.yaml        # 运行配置
└── requirements.txt   # Python 依赖
```

## 核心能力

### 分布式任务编排
- Secretary/Worker 架构，UDP 广播自动发现，HTTP 心跳注册
- 任务自动拆解为子任务 DAG，按依赖顺序并行调度
- Agent 能力声明（Agent Card），技能匹配分发

### 模型路由器（Phase 2）
- **L1-L4 难度分级**：规则驱动，按文本长度、关键词、技能类型自动判定
- **加权评分算法**：`Score = 能力覆盖率×0.4 + 成本反向×0.3 + 速度×0.2 - 负载×0.1`
- **降级链容灾**：首选模型失败时自动沿 Fallback Chain 重试
- **多 Provider 支持**：DeepSeek / OpenAI / Anthropic / Qwen，统一 OpenAI 兼容 API
- **策略适配**：`cost_first`（省钱优先）/ `quality_first`（质量优先）/ `balanced`

### 项目隔离（Phase 3）
- 每个项目独立工作空间、预算配额、模型白名单
- Token 用量自动计量，成本实时追踪
- 预算超支自动暂停或切换经济模型

### MCP 工具网关
- 动态加载外部工具（文件读写、Shell 命令、浏览器控制等）
- MCP 协议兼容，配置文件注册即用

### Web UI 仪表盘
- 深色主题，5 个 Tab 面板：主机监控 / 任务管理 / Agent 状态 / MCP 工具 / 项目管理
- WebSocket 实时推送状态变更

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

## 快速开始

### 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\Activate
pip install -r requirements.txt
```

### 配置模型池

复制模板并填入 API Key 环境变量名：

```powershell
cp lan_mesh/model_pool.example.yaml lan_mesh/model_pool.yaml
```

设置环境变量（按需）：

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:OPENAI_API_KEY = "sk-xxx"
$env:QWEN_API_KEY = "sk-xxx"
```

### 启动 Secretary 节点

```powershell
python main.py secretary
```

Secretary 启动后在 `http://localhost:45470` 提供 Web UI 仪表盘和 API。

### 启动 Worker 节点

```powershell
python main.py worker
```

Worker 自动发现 Secretary 并注册，等待任务分发。

## 端口说明

| 端口 | 协议 | 用途 |
|------|------|------|
| 45454 | UDP | 设备发现广播 |
| 45460 | TCP | Worker HTTP API |
| 45470 | TCP | Secretary HTTP API + Web UI |

## API 概览

### 任务管理
- `POST /api/tasks/submit` — 提交任务（支持关联项目）
- `GET /api/tasks/{id}` — 查询任务状态与子任务 DAG

### 项目管理
- `POST /api/projects` — 创建项目（含预算、模型白名单、路由策略）
- `GET /api/projects` — 列出所有项目
- `GET /api/projects/{id}/usage` — 查看消费记录

### 模型路由
- `POST /api/route/dry-run` — 路由决策预览（输入文本 → 推荐模型 + 评分详情）
- `GET /api/models` — 模型池列表

### 实时通信
- `WS /ws` — WebSocket 推送（主机状态、任务变更、项目事件）

## 路线图

| 阶段 | 状态 | 内容 |
|------|------|------|
| Phase 1 基建层 | ✅ | FastAPI Worker 模板、UDP 发现、心跳注册 |
| Phase 2 路由器 | ✅ | 难度分类器、加权评分路由、降级链 |
| Phase 3 项目隔离 | ✅ | 项目目录隔离、预算计数器、消费追踪 |
| Phase 4 工作流编排 | ✅ | 预设模板（代码任务/文档任务/系统任务） |
| Phase 5 优化与仪表盘 | 🔄 | Tab 多面板仪表盘（已完成），语义缓存（待做） |

## License

MIT
