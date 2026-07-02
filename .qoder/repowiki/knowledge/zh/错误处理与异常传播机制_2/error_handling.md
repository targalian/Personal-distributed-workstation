LAN Mesh 框架采用**基于 HTTP 状态码的显式错误传播**与**防御性编程**相结合的错误处理策略。系统未定义全局自定义异常类，而是依赖 FastAPI 的 `HTTPException` 进行 API 层错误反馈，并在业务逻辑层广泛使用 `try-except` 块捕获底层异常以防止服务崩溃。

### 1. API 层错误处理 (FastAPI)
- **统一出口**：所有 API 路由（`api.py`, `station_api.py`）通过抛出 `fastapi.HTTPException` 向客户端返回结构化错误。
- **状态码约定**：
  - `400 Bad Request`: 参数缺失或格式错误（如缺少 `task_id`）。
  - `403 Forbidden`: 安全校验失败（如共享文件路径穿越攻击）。
  - `404 Not Found`: 资源不存在（如主机、Agent、任务或文件未找到）。
  - `409 Conflict`: 状态冲突（如重复启动已运行的子进程）。
  - `502 Bad Gateway`: 远程主机通信失败（Station Director 调用 Worker API 时）。
  - `503 Service Unavailable`: 核心组件未初始化或功能未激活（如 Secretary 模式未开启）。
- **前置检查模式**：在 `station_api.py` 中定义了 `_check_secretary()` 辅助函数，用于在受保护的路由执行前统一校验业务状态，避免空指针或非法操作。

### 2. 业务逻辑层防御
- **静默失败与容错**：在非关键路径（如 WebSocket 广播 `broadcast_ws`、Bot 消息推送 `bot_gateway.py`）中，使用宽泛的 `except Exception` 捕获异常并记录日志，确保单点故障不影响主流程或其他客户端连接。
- **资源安全访问**：
  - `shared_folder.py` 实现了严格的路径解析逻辑 `resolve_path`，通过比对解析后的绝对路径前缀防止目录穿越（Path Traversal），并在越界时抛出 `ValueError`。
  - 文件操作中使用 `PermissionError` 和 `OSError` 捕获，跳过无法访问的文件。
- **降级与重试**：
  - `agent_runtime.py` 在 LLM 调用中实现了**降级链（Fallback Chain）**机制。当首选模型调用失败时，自动捕获异常并尝试备用模型，最终若全部失败则返回包含错误信息的占位结果，而非直接抛出异常中断任务。

### 3. 日志与观测
- **控制台日志**：系统主要依赖 `print(f"[Component] ...")` 进行运行时状态输出和错误记录（如 `[BotGateway]`、`[AgentRuntime]`）。
- **缺乏结构化日志**：目前未集成标准的 `logging` 模块或结构化日志框架，错误追踪主要依靠堆栈回溯和控制台输出。

### 4. 开发者规范
- **API 开发**：在新增 API 端点时，必须对输入参数进行校验，并在依赖服务不可用时抛出 `HTTPException(status_code=503)`。
- **跨主机调用**：Station Director 调用 Worker 接口时，必须包裹 `requests.RequestException` 并转换为 `502` 错误，以区分本地逻辑错误与网络通信错误。
- **文件系统操作**：任何涉及用户输入路径的操作都必须经过 `shared_folder.resolve_path` 校验，严禁直接拼接路径。
- **后台任务**：在异步广播或消息推送循环中，必须使用 `try-except Exception` 保护循环体，防止单个客户端断开导致整个广播线程终止。