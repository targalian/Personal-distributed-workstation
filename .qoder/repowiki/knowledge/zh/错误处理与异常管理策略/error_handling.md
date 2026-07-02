该代码库采用**语言原生机制**（Python `try/except` + Rust `Result<T, E>`）结合**框架级中间件**（FastAPI `HTTPException`）的混合错误处理模式。整体设计偏向实用主义，强调在分布式环境下的容错性与自愈能力。

### 1. Python 后端 (lan_mesh)

#### 核心模式：HTTP 状态码映射与静默容错
- **API 层 (`lan_mesh/api.py`)**：
  - 使用 FastAPI 的 `HTTPException` 将业务逻辑错误转换为标准 HTTP 状态码。
  - **常见映射**：
    - `404 Not Found`：资源不存在（如设备未注册、文件缺失）。
    - `403 Forbidden`：权限或路径安全校验失败（如共享文件越权访问）。
    - `409 Conflict`：状态冲突（如重复启动子进程）。
    - `503 Service Unavailable`：依赖组件未初始化（如 Agent 运行时、编排器缺失）。
  - **WebSocket 容错**：在广播消息时捕获所有 `Exception` 并静默移除失效连接，防止单点故障导致推送中断。

- **执行引擎 (`lan_mesh/agent_runtime.py`)**：
  - **沙箱化执行**：`execute` 方法包裹在宽泛的 `try...except Exception` 中，确保任何技能处理器（LLM 调用、Shell 执行）的崩溃都不会导致 Worker 进程退出，而是返回 `status: "failed"` 结构。
  - **降级链重试**：在 LLM 调用中实现手动重试逻辑。当首选模型失败时，自动遍历 `fallback_models` 列表，记录最后一条错误信息并在全部失败后返回友好提示。
  - **超时控制**：对 `subprocess` 和 `requests` 调用设置明确的 `timeout`，并专门捕获 `TimeoutExpired` 进行状态转换。

- **基础设施层 (`lan_mesh/database.py`, `preflight.py`)**：
  - **数据库兼容性**：在 SQLite 初始化时使用 `try...except OperationalError` 处理列已存在的场景，支持平滑升级。
  - **启动自检 (`preflight.py`)**：引入 `CheckResult` 数据类，将环境检查（端口占用、依赖缺失、权限不足）结构化。区分 `critical`（致命，中止启动）与 `non-critical`（警告，继续运行），并提供自动修复建议（如自动生成配置文件）。

### 2. Rust 前端/桌面端 (quicklan-main)

#### 核心模式：Result 传播与命令层统一封装
- **Tauri Commands (`commands.rs`)**：
  - 所有暴露给前端的命令均返回 `Result<T, String>`。
  - **错误转换**：底层操作（如文件 IO、网络发现）的错误通过 `.map_err(|e| format!("...: {e}"))` 转换为人类可读的中文错误字符串，直接透传给 UI 展示。
  - **状态守卫**：使用 `ok_or_else` 处理 `Option` 类型，将“未找到设备”等逻辑缺失转化为明确的错误描述。

- **传输服务 (`transfer.rs`)**：
  - **异步错误隔离**：在 `tokio::spawn` 的任务中，错误被捕获并通过 `emit_failure` 事件发送给前端，而不是导致后台监听线程崩溃。
  - **完整性校验**：在文件接收完成后比对 SHA256，若不匹配则主动触发 `fail_receive` 流程，确保数据一致性错误能被用户感知。

### 3. 开发者规范与建议

1. **禁止裸奔的 Panic**：在 Rust 侧严禁在 Command 函数中使用 `unwrap()`，必须使用 `?` 运算符或 `match` 处理 `Result`。
2. **结构化错误返回**：Python 侧在执行任务时，应始终返回包含 `status` 和 `error` 字段的字典，避免抛出未捕获的异常导致 RPC 调用断开。
3. **超时即正义**：所有涉及网络请求（HTTP/TCP）和外部进程调用的代码，必须设置合理的超时时间，并针对超时提供明确的反馈。
4. **日志与静默**：对于非关键的路径探测或心跳丢失，采用静默处理或低级别日志打印；对于影响用户操作的错误（如文件发送失败），必须通过事件系统或 HTTP 响应明确告知原因。