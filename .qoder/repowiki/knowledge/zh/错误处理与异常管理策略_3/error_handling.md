该代码库采用**混合式错误处理架构**，结合了 Python 的 `try-except` 机制、FastAPI 的 `HTTPException` 以及 Rust 的 `Result/Option` 模式。系统未定义全局统一的自定义异常类体系，而是依赖语言原生异常、HTTP 状态码和结构化返回字典来传递错误信息。

### 1. Web API 层：HTTP 异常与状态码映射
在 `lan_mesh/api.py` 中，错误主要通过 FastAPI 的 `HTTPException` 抛出，直接映射为 HTTP 状态码：
- **404 Not Found**: 用于资源不存在（如主机、Agent、任务、文件）。
- **403 Forbidden**: 用于权限或路径安全校验失败（如共享文件访问越界）。
- **409 Conflict**: 用于状态冲突（如重复启动 Secretary）。
- **503 Service Unavailable**: 用于核心组件未初始化（如 Orchestrator、MCP Gateway）。
- **402 Payment Required**: 用于业务逻辑限制（如项目预算耗尽）。

**约定**：API 端点在捕获到具体异常（如 `FileNotFoundError`）时，会将其转换为语义明确的 `HTTPException`，避免向客户端泄露底层堆栈信息。

### 2. 分布式通信层：静默失败与降级重试
由于系统基于 UDP 发现和 HTTP 远程调用，网络不稳定性是主要错误源：
- **UDP 发现 (`discovery.py`)**：采用**静默失败**策略。在广播、监听和探测过程中，捕获 `OSError`、`socket.timeout` 和 `JSONDecodeError` 后仅记录日志或忽略，确保发现服务线程不因单个数据包错误而崩溃。
- **任务编排 (`orchestrator.py`)**：在 `_dispatch_subtask` 中，使用 `requests.RequestException` 捕获网络错误。若子任务执行失败（HTTP 非 200 或超时），DAG 引擎会将子任务状态标记为 `failed` 并记录错误详情，进而触发顶层任务的失败聚合逻辑。
- **模型路由降级 (`agent_runtime.py`)**：实现了**自动降级链**。当首选 LLM 模型调用失败时，系统会自动遍历 `fallback_models` 列表进行重试。若整条链路失败，则返回包含错误信息的结构化字典，而非抛出异常中断进程。

### 3. 数据持久化层：兼容性处理与线程安全
- **数据库迁移 (`database.py`)**：在 `_init_db` 中，通过 `try-except sqlite3.OperationalError` 处理 `ALTER TABLE` 操作，确保在已存在列的情况下不会报错，实现了平滑的 schema 演进。
- **线程隔离**：使用 `threading.local()` 存储 SQLite 连接，避免了多线程环境下的连接竞争错误。

### 4. Rust 客户端层：Result 传播与优雅退出
在 `quicklan-main/src-tauri` 中：
- **控制 API (`control_api.rs`)**：内部函数返回 `Result<(), String>`，通过 `map_err` 将底层 I/O 错误转换为描述性字符串。连接处理中严格校验 `is_loopback()`，防止非法访问。
- **应用入口 (`lib.rs`)**：使用 `.expect("...")` 处理关键初始化错误（如 TCP 监听器启动失败），确保应用在不可恢复的错误下能明确终止并输出原因。
- **单实例守护**：在 Windows 平台通过 `CreateMutexW` 和 `GetLastError` 处理多实例启动冲突，并通过 TCP 通知已有实例。

### 5. 开发者准则
- **API 开发**：必须使用 `HTTPException` 包装业务错误，并选择合适的 HTTP 状态码。
- **远程调用**：所有 HTTP/UDP 交互必须包裹在 `try-except` 中，禁止让网络异常穿透到主逻辑层。
- **异步任务**：在后台线程（如 Bot 推送、任务调度）中，必须在最外层捕获 `Exception` 并记录日志，防止线程静默退出。
- **Rust 开发**：优先使用 `?` 运算符传播错误，仅在应用边界（如 `main` 或 `setup`）进行 `expect` 或日志记录。