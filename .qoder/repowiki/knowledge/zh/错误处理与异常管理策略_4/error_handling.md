LAN Mesh 采用**防御性编程**与**运行时自愈**相结合的错误处理策略。系统未定义全局统一的自定义异常类体系，而是深度依赖语言原生异常（Python `Exception`/`OSError`，Rust `Result`）并结合 HTTP 状态码进行跨服务通信的错误表达。

### 1. 核心处理模式

*   **HTTP 协议层错误映射**：
    *   在 FastAPI 路由中，业务逻辑错误被显式转换为 `fastapi.HTTPException`，携带标准 HTTP 状态码（如 `404 Not Found`, `403 Forbidden`, `503 Service Unavailable`, `402 Payment Required`）。
    *   **示例**：文件下载失败时捕获 `FileNotFoundError` 并转为 `404`；项目预算不足时直接抛出 `402`。
*   **网络通信的静默失败与重试**：
    *   UDP 发现服务 (`discovery.py`) 和 Worker 心跳循环 (`worker.py`) 采用“尽力而为”策略。网络异常（`OSError`, `requests.RequestException`）通常被 `try-except` 捕获并记录日志，随后进入重试循环或忽略，确保局部网络波动不导致进程崩溃。
*   **启动前自检 (Preflight Checks)**：
    *   通过 `preflight.py` 模块在应用启动前执行严格的環境检查（Python 版本、依赖包、端口占用、目录权限）。致命错误（`critical=True`）会直接终止启动并输出格式化报告，非致命错误则发出警告。
*   **任务执行的沙箱化错误隔离**：
    *   `AgentRuntime` 在执行子任务时，使用顶层 `try-except Exception` 包裹处理器调用。任何技能执行中的异常都会被捕获并转化为包含 `status: "failed"` 和 `error` 字段的字典返回给调度器，防止单个任务失败污染 Agent 进程。
*   **数据库 schema 演进兼容**：
    *   `database.py` 在初始化时使用 `try-except sqlite3.OperationalError` 来处理 `ALTER TABLE` 操作，确保在已有数据库上添加新列时不会因列已存在而报错。

### 2. Rust (Tauri) 端的错误传播

*   **Result 类型与 expect**：
    *   QuickLAN 模块遵循 Rust 惯例，使用 `Result<T, E>` 进行错误传播。在初始化关键组件（如 TCP 监听器、Library 加载）失败时，使用 `.map_err(|err| format!(...))?` 将错误转换为字符串并向上传播。
    *   应用入口 `lib.rs` 使用 `.expect("failed to run QuickLAN")` 处理 Tauri 运行时的最终错误，确保启动失败时有明确的 panic 信息。
*   **单实例互斥**：
    *   通过 Windows API `CreateMutexW` 实现单实例检查，若检测到实例已存在，则通过 TCP 连接通知旧实例并退出新实例，这是一种基于操作系统原语的错误/状态处理机制。

### 3. 开发者约定

*   **禁止裸奔的 panic**：在 Python 业务逻辑中，严禁让未捕获的异常穿透到 API 层（除非是严重的内部错误），应统一转换为 `HTTPException` 或返回错误状态的字典。
*   **网络调用的超时保护**：所有涉及网络 I/O 的操作（`requests.post`, `socket.recvfrom`）必须设置 `timeout`，并在捕获超时异常后进行合理的降级处理。
*   **日志记录**：捕获异常后应打印简要错误信息（如 `[Worker] 注册失败: {e}`），便于分布式环境下的故障排查。