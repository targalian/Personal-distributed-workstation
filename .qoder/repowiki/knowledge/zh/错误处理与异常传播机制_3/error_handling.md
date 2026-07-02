该仓库采用**分层、多语言混合**的错误处理策略，核心原则是：**底层捕获并转换，上层统一响应或降级**。系统未定义全局统一的自定义异常类体系，而是依赖语言原生机制（Python `Exception`/`HTTPException`，Rust `Result<T, String>`）结合业务逻辑进行错误传播。

### 1. Python 后端 (FastAPI)
- **HTTP 异常标准化**：在 `lan_mesh/api.py` 和 `lan_mesh/station_api.py` 中，所有 API 端点均使用 FastAPI 的 `HTTPException` 向客户端返回结构化错误。常见状态码包括：
  - `400`: 参数缺失或无效（如 `缺少 task_id`）。
  - `404`: 资源不存在（如 `设备未注册`、`文件不存在`）。
  - `409`: 状态冲突（如 `启动失败`、`角色已存在`）。
  - `503`: 服务不可用（如 `Agent 运行时未初始化`、`Secretary 未激活`）。
- **防御性编程**：在路由层大量使用前置检查（如 `_check_secretary()`），确保业务组件就绪后再执行逻辑，避免空指针或状态不一致错误。
- **静默失败与容错**：在 WebSocket 广播 (`broadcast_ws`) 和非关键路径（如 UDP 发现补充）中，采用 `try...except Exception: pass` 或记录死连接后移除，防止单点故障影响整体服务可用性。
- **LLM 调用降级**：在 `lan_mesh/agent_runtime.py` 中，实现了**模型降级链（Fallback Chain）**。当首选模型调用失败时，自动尝试备用模型，并将最终错误信息封装在返回结果中（`status: "failed"`），而非直接抛出异常中断任务流。

### 2. Rust 前端/桌面端 (Tauri)
- **Result 类型传播**：在 `quicklan-main/src-tauri/src/commands.rs` 中，所有 Tauri 命令均返回 `Result<T, String>`。错误通过 `?` 操作符或 `map_err` 转换为人类可读的字符串消息，直接反馈给前端 UI。
- **内部 HTTP 服务器健壮性**：`control_api.rs` 实现了一个轻量级 TCP 服务器用于进程间通信。它通过 `eprintln!` 记录启动失败，并在连接处理中使用 `match` 匹配错误，返回标准 HTTP 错误码（400/404/503）的 JSON 响应，确保外部调用方能正确解析错误。
- **Panic 策略**：仅在应用入口 (`lib.rs`) 的 `run()` 方法中使用 `expect`，确保启动失败时能明确报错并退出；其他异步任务中避免 panic，优先返回 `Err`。

### 3. 开发规范与建议
- **API 层**：必须使用 `HTTPException` 返回错误，禁止直接抛出未捕获的 Python 异常。
- **业务逻辑层**：对于可恢复的错误（如网络波动、模型超时），应实现重试或降级逻辑，并返回包含错误详情的字典，而非中断流程。
- **Rust 命令层**：避免使用 `unwrap()`，除非能绝对保证安全性；优先使用 `ok_or_else` 或 `map_err` 提供清晰的错误上下文。
- **日志记录**：目前错误日志较为分散，建议在关键错误路径（如 LLM 调用失败、远程主机连接超时）增加结构化日志记录，以便排查分布式环境下的问题。