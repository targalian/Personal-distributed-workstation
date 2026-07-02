## 1. 系统概述
本项目（LAN Mesh）采用**极简的基于标准输出（stdout/stderr）的控制台日志机制**。未引入任何第三方结构化日志框架（如 `logging`、`loguru` 等），而是直接使用 Python 内置的 `print()` 函数进行状态反馈、调试信息输出和错误报告。

这种设计符合轻量级分布式网格工具的定位，便于在终端直接观察节点状态，但缺乏日志级别管理、持久化存储和结构化查询能力。

## 2. 核心实现方式
### 2.1 日志输出工具
- **主要工具**: `print()`
- **输出目标**: 默认标准输出（控制台）。
- **格式化风格**: `[组件名] 消息内容`
  - 例如: `[Worker] 设备 ID: ...`, `[Station] 新主机入站: ...`, `[Orchestrator] 任务已提交: ...`

### 2.2 日志前缀规范
代码中通过硬编码的前缀来区分日志来源模块：
- `[Worker]`: Worker 代理节点 (`lan_mesh/worker.py`)
- `[Secretary]`: 传统中心控制节点 (`lan_mesh/secretary.py`)
- `[Station]`: 工作站主管节点 (`lan_mesh/station_director.py`, `lan_mesh/station_controller.py`)
- `[发现]`: UDP 网络发现服务 (`lan_mesh/discovery.py`)
- `[Orchestrator]`: 任务编排器 (`lan_mesh/orchestrator.py`)
- `[MCP:{name}]`: MCP 协议客户端 (`lan_mesh/mcp_client.py`)
- `[AgentRuntime]`: Agent 运行时 (`lan_mesh/agent_runtime.py`)

### 2.3 日志级别模拟
虽然没有正式的日志级别，但通过语境隐含了不同重要性：
- **INFO/STATUS**: 启动信息、注册成功、心跳正常、任务提交。
- **WARN/ERROR**: 注册失败、心跳丢失、端口占用、自检未通过、异常捕获。
  - 错误通常伴随 `Exception` 捕获并打印具体错误信息。

## 3. 关键文件与逻辑
| 文件路径 | 职责 | 日志示例 |
| :--- | :--- | :--- |
| `lan_mesh/worker.py` | Worker 节点生命周期、注册、心跳 | `[Worker] 主机信息已注册到 Secretary ...` |
| `lan_mesh/station_director.py` | 主机评级、入站/离线事件 | `[Station] 新主机入站: ... [S级]` |
| `lan_mesh/station_controller.py` | Station 模式激活、资源管理 | `[Station] Secretary 模式已激活 ...` |
| `lan_mesh/secretary.py` | 传统 Secretary 节点逻辑 | `[Secretary] 模型路由器已加载: ...` |
| `lan_mesh/discovery.py` | UDP 广播与监听 | `[发现] UDP 绑定端口 ... 失败` |
| `lan_mesh/orchestrator.py` | 任务分解与调度 | `[Orchestrator] 子任务完成: ...` |
| `main.py` | 统一入口 | 无直接日志，依赖子模块输出 |

## 4. 架构约束与开发者规范
### 4.1 当前约束
1. **无日志持久化**: 所有日志仅输出到控制台，进程重启后日志丢失。若需审计历史事件，需依赖 SQLite 数据库中的事件记录（如 `host_events` 表）。
2. **无异步日志支持**: `print()` 是同步阻塞调用，在高并发场景下可能轻微影响性能（但在本项目的低频心跳/事件场景下可忽略）。
3. **难以过滤**: 无法通过配置动态关闭某些模块的日志，只能通过重定向 stdout/stderr 或 grep 过滤。
4. **Uvicorn 日志抑制**: 在启动 FastAPI/Uvicorn 时，显式设置了 `log_level="warning"`，以抑制框架自带的 INFO 级访问日志，保持控制台输出整洁，仅关注业务逻辑日志。

### 4.2 开发者建议
1. **保持一致的前缀**: 新增模块时，请在 `print()` 中添加统一的 `[Module Name]` 前缀，以便于日志追踪。
2. **错误处理**: 捕获异常时，务必打印异常详情（如 `print(f"[Module] 错误: {e}")`），避免静默失败。
3. **关键状态上屏**: 节点启动、注册、角色切换、任务状态变更等关键生命周期事件必须输出日志。
4. **避免敏感信息**: 由于日志直接输出到控制台，避免打印密钥、完整路径等敏感信息。
5. **未来演进**: 若项目规模扩大，建议迁移至 `logging` 模块或 `loguru`，以支持文件轮转、JSON 格式化和远程日志收集。