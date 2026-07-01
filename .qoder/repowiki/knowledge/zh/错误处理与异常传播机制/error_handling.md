该仓库采用**语言原生异常模型**作为核心错误处理机制，未引入全局中间件或统一的错误码枚举体系。错误处理呈现出明显的**分层特征**：API 层使用 HTTP 状态码映射业务逻辑错误，Rust 层使用 `Result<T, String>` 进行显式错误传播，而底层业务逻辑则依赖 `try-except` 块进行容错和降级。

### 1. Python (FastAPI) 层：HTTPException 与业务校验
在 `lan_mesh/api.py` 中，错误主要通过抛出 `fastapi.HTTPException` 来处理。这是一种典型的 Web 框架模式，将内部状态直接映射为 HTTP 响应。
- **状态码约定**：
  - `404 Not Found`：用于资源不存在（如主机、Agent、任务、文件）。
  - `403 Forbidden`：用于权限或路径安全校验失败（如共享文件访问越界）。
  - `409 Conflict`：用于状态冲突（如重复启动子进程）。
  - `503 Service Unavailable`：用于依赖组件未初始化（如 Agent 运行时、编排器、MCP 网关）。
  - `402 Payment Required`：用于业务逻辑限制（如项目预算耗尽）。
- **实现模式**：在每个 API 端点入口处进行前置条件检查（Guard Clauses），若不满足则立即抛出异常。例如：
  ```python
  if not agent_runtime:
      raise HTTPException(status_code=503, detail="Agent 运行时未初始化")
  ```

### 2. Rust (Tauri) 层：Result<String> 与命令边界
在 `quicklan-main/src-tauri/` 中，错误处理遵循 Rust 的 `Result` 模式，但在 Tauri 命令边界处进行了简化。
- **错误类型**：绝大多数 `#[tauri::command]` 函数返回 `Result<T, String>`。错误信息以人类可读的中文或英文字符串形式传播。
- **传播策略**：使用 `?` 操作符在内部模块（如 `storage`, `library`, `transfer`）间传播错误，最终在命令函数顶层通过 `map_err` 转换为友好的提示字符串。
- **容错设计**：在非关键路径（如单例实例通知、托盘图标设置）使用 `unwrap_or` 或 `if let Err(_) = ...` 忽略错误，防止次要故障导致应用崩溃。

### 3. 业务逻辑层：静默失败与降级链
在核心业务逻辑中，系统倾向于**捕获异常并返回结构化错误状态**，而非向上抛出。
- **Agent 运行时 (`agent_runtime.py`)**：
  - **执行容错**：`execute` 方法包裹在 `try-except Exception` 中，任何未预期的异常都会被捕获并转化为 `{"status": "failed", "error": str(e)}` 返回给调用方。
  - **LLM 降级链**：在 `_call_llm_with_routing` 中实现了**自动重试与降级**。如果首选模型调用失败，系统会遍历 `fallback_models` 列表尝试其他模型，仅在所有尝试均失败后返回错误摘要。
- **数据库层 (`database.py`)**：
  - **Schema 演进容错**：在 `_init_db` 中，使用 `try-except sqlite3.OperationalError` 来处理 `ALTER TABLE` 操作，确保在列已存在时不会报错，实现了平滑的数据库迁移。
- **编排器 (`orchestrator.py`)**：
  - **异步任务隔离**：子任务的分发与执行在独立线程中进行，网络请求异常（`requests.RequestException`）被捕获并记录为子任务的 `failed` 状态，不会导致主调度循环崩溃。

### 4. 开发者约束与建议
- **禁止裸奔异常**：在 API 边界外，严禁让未处理的 `Exception` 穿透到框架层。应捕获并转化为业务定义的错误状态或日志。
- **错误信息本地化**：Rust 层的错误字符串目前混合了中英文，建议统一为英文以便后续国际化，或在 UI 层进行映射。
- **降级优先**：在涉及外部依赖（如 LLM API、网络探测）时，必须实现超时控制和降级策略（Fallback），避免单点故障阻塞整个工作流。
- **日志记录**：目前在 `print` 中记录关键错误（如模型调用失败、主机离线）。在生产环境中，应替换为结构化日志系统（如 `logging` 模块或 `tracing`），以便追踪错误上下文。