该项目采用**双模架构**（Python FastAPI 后端 + Rust Tauri 桌面端），针对不同语言生态采用了差异化的错误处理策略，整体呈现出**框架驱动**与**结果导向**相结合的特征。

### 1. Python 后端 (lan_mesh)
- **HTTP 层错误映射**：在 `lan_mesh/api.py` 中，深度依赖 FastAPI 的 `HTTPException`。业务逻辑中的异常（如文件不存在、设备未注册、服务未初始化）被显式捕获并转换为标准的 HTTP 状态码（404, 403, 503, 400）。这种模式确保了前端能接收到语义清晰的错误响应。
- **运行时容错**：在 `lan_mesh/agent_runtime.py` 中，任务执行引擎采用“宽进严出”的策略。通过 `try...except Exception` 包裹所有技能处理器（如 LLM 调用、Shell 执行），将底层异常统一收敛为包含 `status: "failed"` 和 `error` 字段的字典。这种方式防止了单个子任务的崩溃导致整个 Agent 运行时退出。
- **外部依赖防护**：针对 LLM API 调用，使用 `requests.raise_for_status()` 主动触发异常，并由上层统一处理网络或认证失败。

### 2. Rust 桌面端 (quicklan-main)
- **Result<T, String> 范式**：在 `src-tauri/src/commands.rs` 及核心模块（`transfer.rs`, `library.rs`, `storage.rs`）中，广泛使用 `Result<T, String>` 作为错误返回类型。错误信息通常通过 `format!` 宏生成，包含详细的上下文（如文件路径、系统错误描述）。
- **错误传播与转换**：利用 Rust 的 `?` 操作符实现错误的自动向上传播。在边界处（如 Tauri Commands），将底层库的错误（如 `std::io::Error`, `rusqlite::Error`）通过 `map_err` 转换为对用户友好的中文错误字符串。
- **资源完整性校验**：在文件传输和共享存储中，引入了 SHA256 校验机制。如果哈希不匹配，会主动抛出错误并终止操作，确保数据一致性。
- **并发安全锁错误**：在处理共享状态（如 `Mutex<HashMap>`）时，对锁中毒（PoisonError）进行了处理，通常返回固定的中文提示（如“传输记录正在被占用”）。

### 3. 前端交互 (React/TypeScript)
- **透明透传**：`quicklan-main/src/api.ts` 通过 Tauri 的 `invoke` 调用后端命令。由于后端返回的是 `Result`，前端的 Promise 会在错误时 reject。目前代码中未见统一的全局错误拦截器，错误处理分散在各个 UI 组件中。

### 4. 开发者规范建议
- **Python 侧**：应优先抛出 `HTTPException` 而非让未处理异常穿透到框架默认处理器；在执行不可靠的外部调用（LLM、Subprocess）时必须包裹 `try...except`。
- **Rust 侧**：避免在核心逻辑中使用 `.unwrap()` 或 `.expect()`，除非在启动初始化阶段；所有公开 API 应返回 `Result` 并提供具有操作指导意义的错误消息；文件 IO 和网络操作必须考虑超时和中断处理。