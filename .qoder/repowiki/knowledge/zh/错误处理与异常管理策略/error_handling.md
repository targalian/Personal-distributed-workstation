LAN Mesh 分布式智能编排平台采用**基于框架的 HTTP 状态码映射**与**防御性编程**相结合的错误处理模式。系统未定义全局统一的错误类型（如自定义 Exception 类），而是依赖 FastAPI 的 `HTTPException` 进行 API 层错误反馈，并在业务逻辑层广泛使用 `try-except` 块捕获运行时异常。

### 1. API 层错误处理 (FastAPI)
- **HTTP 异常映射**：在 `lan_mesh/api.py` 中，所有业务校验失败均通过抛出 `fastapi.HTTPException` 处理。系统严格遵循 RESTful 规范：
  - `404 Not Found`：资源不存在（如设备未注册、文件缺失、任务/项目 ID 无效）。
  - `403 Forbidden`：权限或安全校验失败（如共享文件路径越界）。
  - `402 Payment Required`：业务逻辑限制（如项目预算耗尽）。
  - `503 Service Unavailable`：核心组件未初始化（如编排器、MCP 网关、项目管理器缺失）。
- **WebSocket 容错**：在 `/ws` 端点中，采用静默捕获策略。`WebSocketDisconnect` 和通用 `Exception` 均被捕获并忽略，仅在 `finally` 块中清理客户端连接集合，确保推送循环不因单点故障中断。

### 2. 节点间通信容错 (Worker-Master)
- **注册与心跳重试**：`lan_mesh/worker.py` 中的 `_register_with_master` 和 `_send_heartbeat` 方法包裹在 `try-except requests.RequestException` 中。网络波动导致的请求失败不会导致 Worker 崩溃，而是通过日志记录并进入下一次心跳周期的重试逻辑。
- **超时控制**：所有跨节点 HTTP 请求均设置 `timeout=5` 秒，防止因目标节点无响应而导致本地线程阻塞。

### 3. 任务执行与 Agent 运行时
- **降级链机制**：`lan_mesh/agent_runtime.py` 实现了 LLM 调用的**自动降级策略**。当首选模型调用失败时，系统会遍历 `fallback_models` 列表尝试备用模型。若整条链路失败，则返回包含错误信息的结构化结果而非抛出异常，确保任务状态可追踪。
- **沙箱化执行**：Shell 命令执行 (`subprocess.run`) 和文件操作均包裹在 `try-except` 中。特别针对 `subprocess.TimeoutExpired` 进行了专项捕获，防止恶意或长耗时命令卡死 Agent。
- **统一结果封装**：`execute` 方法将所有内部异常捕获并转换为 `{"status": "failed", "error": str(e)}` 格式，保证 Master 接收到的永远是合法的结构化数据。

### 4. 启动前自检 (Preflight)
- **主动式错误预防**：`lan_mesh/preflight.py` 在系统启动前执行 10 项关键检查（Python 版本、依赖包、端口占用、目录权限等）。
- **自动修复与分级报告**：对于非致命问题（如配置文件缺失），系统尝试自动创建默认配置；对于致命问题（如端口冲突、依赖缺失），直接终止启动并输出带图标（✅/❌/⚠️）的诊断报告，避免系统在不可用状态下运行。

### 5. Rust/Tauri 端错误处理
- **Result 传播与静默失败**：在 `quicklan-main/src-tauri` 中，错误处理遵循 Rust 惯例。控制 API (`control_api.rs`) 使用 `map_err` 将底层 IO 错误转换为 JSON 错误响应。对于非核心功能（如托盘图标加载、窗口焦点设置），广泛使用 `let _ = ...` 忽略错误，确保 UI 交互的流畅性。
- **单实例互斥**：通过 Windows API `CreateMutexW` 实现单实例守护，若检测到已有实例则通过 TCP 通知旧实例并退出新进程，从进程层面避免资源竞争错误。

### 6. 数据库层健壮性
- **线程安全与迁移兼容**：`lan_mesh/database.py` 使用 `threading.local` 确保 SQLite 连接的线程隔离。在表结构变更时（如添加 `project_id` 列），使用 `try-except sqlite3.OperationalError` 捕获“列已存在”异常，实现平滑的数据库迁移。