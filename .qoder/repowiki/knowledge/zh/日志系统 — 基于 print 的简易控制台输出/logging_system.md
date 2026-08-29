## 1. 系统概述

LAN Mesh 项目早期未集成标准 `logging` 模块，运行时信息输出完全依赖内置的 `print()` 函数配合模块前缀标签。**当前核心模块（`station_controller.py`、`worker.py`、`agent_runtime.py`、`pm_agent.py`、`chat_handler.py` 等）已迁移到标准 `logging`**，由 `lan_mesh/logger.py` 统一提供格式、控制台/文件双通道与日志轮转；其余模块仍保留 `print` 前缀风格。

这种模式属于**开发阶段或轻量级应用**常见的简易日志策略，具备实现简单、无依赖的优点，但缺乏日志级别管理、结构化输出、文件持久化及异步处理能力。

## 2. 核心实现机制

### 2.1 输出方式
- **标准输出流**：所有日志均通过 `print()` 写入 `stdout`。
- **同步阻塞**：`print` 调用是同步的，在高并发或频繁 I/O 场景下可能轻微影响性能。

### 2.2 日志格式约定
开发者遵循一种非强制性的**前缀标记规范**，格式通常为：
`[模块名] 消息内容`

常见的前缀标记包括：
- `[Station]`: Station Director 主控的主流程日志（启动、配置加载、Web UI 地址、Secretary 激活）。
- `[Worker]`: 工作节点的主流程日志（注册、心跳、共享文件夹状态）。
- `[发现]` (Discovery): UDP 广播发现服务的底层网络事件（端口绑定、设备_seen、回调异常）。
- `[Orchestrator]`: 任务编排引擎的状态变更（任务提交、子任务分发、完成/失败）。
- `[Router]`: 模型路由器的决策过程（模型选择、评分、降级策略）。
- `[AgentRuntime]`: Worker 端 Agent 执行时的内部事件（LLM 调用失败、降级重试）。
- `[MCP:{name}]`: MCP 工具网关的连接状态。

### 2.3 日志级别模拟
虽然没有正式的 Level 枚举，但代码中隐含了以下语义：
- **INFO**: 常规状态流转，如 `服务已启动`, `任务已提交`, `Agent Card 已注册`。
- **WARNING/ERROR**: 异常捕获后的提示，如 `端口仍被占用,发现服务降级运行`, `心跳失败,尝试重新注册...`, `模型调用失败`。
- **DEBUG**: 极少出现，通常通过注释掉 `print` 或条件判断来实现（当前代码中未见显式的 Debug 开关）。

## 3. 关键文件分布

日志输出分散在各个核心业务模块中，主要集中在以下文件：

| 文件路径 | 主要日志内容 |
| :--- | :--- |
| `lan_mesh/logger.py` | 结构化日志系统：统一格式、控制台 + 文件双通道、5MB 轮转、LAN_MESH_LOG_* 环境变量控制 |
| `lan_mesh/station_controller.py` | Station 启动流程、端口检测、Web UI 地址、Secretary 激活/停用 |
| `lan_mesh/worker.py` | Worker 启动、注册结果、心跳状态、Agent 运行时初始化 |
| `lan_mesh/discovery.py` | UDP 端口绑定错误、设备发现回调异常、降级运行提示 |
| `lan_mesh/orchestrator.py` | 任务分解结果、模型路由决策详情、子任务 HTTP 调用状态 |
| `lan_mesh/agent_runtime.py` | LLM API 调用失败、降级链重试过程 |
| `lan_mesh/mcp_client.py` | MCP Server 连接状态、握手失败、命令不存在提示 |
| `main.py` | 仅作为入口，无直接日志输出，依赖子模块 |

## 4. 架构缺陷与改进建议

### 4.1 当前局限性
1. **无法动态控制级别**：生产环境中无法通过配置关闭 INFO 日志而保留 ERROR 日志，导致控制台噪音大。
2. **缺乏结构化数据**：日志为纯文本，难以被 ELK、Promtail 等日志收集系统解析字段（如 `device_id`, `task_id`）。
3. **无持久化机制**：重启后日志丢失，不利于故障回溯。
4. **线程安全隐忧**：虽然 CPython 的 `print` 大致线程安全，但在高并发多线程环境下（如多个 Worker 心跳线程、UDP 监听线程），日志行可能会交错截断。
5. **Uvicorn 日志隔离**：在 `station_controller.py` 和 `worker.py` 中，Uvicorn 的 `log_level` 被硬编码为 `"warning"`，这意味着 HTTP 请求日志默认被抑制，仅保留应用层的日志输出。

### 4.2 开发者规范
在当前架构下，开发人员应遵循以下约定：
- **必须添加前缀**：所有 `print` 语句必须包含 `[模块名]` 前缀，以便通过 `grep` 过滤。
- **异常捕获必打日志**：在 `try-except` 块中捕获异常时，必须 `print` 错误信息，否则静默失败将极难调试。
- **避免敏感信息**：由于日志直接输出到控制台，严禁打印 API Key、完整 Token 等敏感数据。

### 4.3 演进建议
若项目进入生产阶段，建议引入 `logging` 模块并进行如下改造：
1. **初始化 Root Logger**：在 `main.py` 或 `lan_mesh/__init__.py` 中配置 `logging.basicConfig`。
2. **模块级 Logger**：在每个模块顶部使用 `logger = logging.getLogger(__name__)`。
3. **结构化日志**：引入 `python-json-logger` 或 `structlog`，将 `device_id`, `task_id` 等上下文放入 JSON 字段。
4. **统一入口配置**：通过 `config.yaml` 增加 `logging.level` 和 `logging.file` 配置项。