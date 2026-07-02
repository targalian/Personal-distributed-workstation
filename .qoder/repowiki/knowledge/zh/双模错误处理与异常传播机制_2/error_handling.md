该仓库采用基于 Python 和 Rust (Tauri) 的双模错误处理策略，核心依赖于语言原生的异常/Result 机制及轻量级的控制台日志反馈。

### 1. 系统与方法
- **Python 后端 (LAN Mesh)**：主要使用 Python 内置的 `try-except` 块进行异常捕获。错误通常通过 `print` 或简单的日志函数输出到标准输出（stdout/stderr），缺乏统一的自定义异常类层次结构或全局错误中间件。关键操作（如网络发现、任务编排）中的错误会导致局部失败或进程退出，依赖外部脚本（如 `start_secretary.sh`）进行重启。
- **Rust 前端/桌面端 (QuickLAN)**：遵循 Rust 惯用的 `Result<T, E>` 模式进行错误传播。在 Tauri 命令（Commands）中，错误通常被转换为字符串或特定的 JSON 结构返回给前端 UI，由 React 组件负责展示错误提示。

### 2. 关键文件与包
- **`lan_mesh/worker.py`**：包含 Worker 节点的核心逻辑，使用 `try-except` 处理网络连接和任务执行中的异常。
- **`lan_mesh/master.py`**：Master 控制器逻辑，处理来自 Agent 的错误响应和超时情况。
- **`lan_mesh/discovery.py`**：网络发现模块，捕获 UDP 广播和 socket 通信中的 `OSError` 或 `TimeoutError`。
- **`quicklan-main/src-tauri/src/commands.rs`**：Tauri 后端命令实现，使用 `Result` 类型处理文件传输、设备发现等操作中的错误，并将其序列化后返回前端。
- **`quicklan-main/src/api.ts`**：前端 API 调用层，捕获来自 Tauri 或 HTTP 接口的错误并更新 UI 状态。

### 3. 架构与约定
- **去中心化容错**：在分布式网格中，单个节点的错误（如 Worker 离线）不应导致整个系统崩溃。Master 节点通过心跳检测和超时机制识别失效节点，并将其从可用资源池中移除。
- **轻量级反馈**：错误信息主要以开发者友好的文本形式输出到控制台，便于调试。生产环境中缺乏结构化的错误日志聚合系统。
- **前端错误边界**：React 应用通过状态管理（State）捕获 API 错误，并在 UI 上显示 Toast 通知或错误面板，避免页面白屏。

### 4. 开发者规则
- **Python 端**：在涉及网络 I/O 和外部进程调用的地方必须使用 `try-except` 包裹，避免未捕获异常导致节点崩溃。建议使用具体的异常类型而非裸 `except Exception`。
- **Rust 端**：所有可能失败的操作应返回 `Result`。在 Tauri Command 中，确保错误消息对用户友好且不含敏感路径信息。
- **日志规范**：错误输出应包含时间戳和上下文信息（如节点 ID、任务 ID），以便在分布式环境中追踪问题根源。