## 1. 系统概述
本项目未引入专用的日志框架（如 Python 的 `logging` 模块或 Rust 的 `tracing`/`log` crate），而是采用**标准输出（Standard Output）**作为唯一的日志与状态反馈通道。这种设计体现了轻量级、低依赖的开发理念，适用于局域网工具类应用。

- **Python 端 (`lan_mesh`)**：完全依赖内置 `print()` 函数进行控制台输出。
- **Rust 端 (`quicklan-main`)**：主要依赖 `eprintln!` 宏处理错误与关键状态，前端 React 部分使用 `console` API。

## 2. 核心实现与文件

### Python 后端 (`lan_mesh`)
- **输出方式**：直接使用 `print()`。
- **结构化约定**：采用 `[组件名] 消息内容` 的前缀格式，便于人工筛选。
  - 示例：`[Master] 设备 ID: ...`, `[Worker] 心跳失败...`, `[发现] UDP 绑定端口...`
- **关键文件**：
  - `lan_mesh/master.py`：启动信息、配置刷新异常、离线清理异常。
  - `lan_mesh/worker.py`：注册状态、心跳失败、Agent Card 注册结果。
  - `lan_mesh/discovery.py`：UDP 端口绑定冲突、回调异常。
  - `main.py`：作为入口，本身不产生日志，但通过调用 `master`/`worker` 模块触发其内部输出。

### Rust 桌面端 (`quicklan-main`)
- **输出方式**：使用 `eprintln!` 将错误信息写入标准错误流（stderr）。
- **应用场景**：主要用于捕获底层网络监听失败、单实例互斥锁获取失败等严重错误。
- **关键文件**：
  - `quicklan-main/src-tauri/src/lib.rs`：单实例检查失败、插件初始化。
  - `quicklan-main/src-tauri/src/discovery.rs`：UDP 发现服务启动失败。
  - `quicklan-main/src-tauri/src/transfer.rs`：TCP 传输监听器启动失败。

### 前端界面 (`quicklan-main/src`)
- **输出方式**：使用浏览器标准的 `console.error()` 等 API。
- **关键文件**：
  - `quicklan-main/src/App.tsx` / `api.ts`：API 调用失败时的前端调试输出。

## 3. 架构特征与约定

1. **无级别管理**：所有输出均为同一优先级，缺乏 `INFO`、`WARN`、`ERROR` 等日志级别的程序化过滤能力。开发者需通过阅读源码或手动 grep 前缀来区分重要性。
2. **同步阻塞风险**：Python 端的 `print()` 是同步操作，在高并发或高频心跳场景下可能对性能产生微小影响（目前项目频率较低，影响可忽略）。
3. **错误处理模式**：
   - **Python**：在 `try-except` 块中捕获异常后，直接 `print(f"[组件] 异常: {e}")`，随后通常选择忽略或重试。
   - **Rust**：在 `Result::Err` 分支中使用 `eprintln!` 记录错误，确保关键故障不被静默吞没。
4. **配置缺失**：`config.yaml` 和 `AppConfig` (Pydantic) 中未定义任何日志相关配置（如日志路径、级别、轮转策略）。

## 4. 开发者指南

- **新增日志时**：请严格遵循 `[组件名] 消息` 的格式，例如 `[Orchestrator] 任务分解完成`。
- **错误记录**：对于非致命错误，使用 `print` 或 `eprintln` 记录；对于致命错误，建议在记录后执行 `sys.exit(1)` 或抛出异常。
- **调试建议**：由于缺乏日志文件持久化，排查历史问题需依赖终端回滚或重定向输出（如 `python main.py master > log.txt 2>&1`）。
- **未来演进**：若需提升可观测性，建议引入 `loguru` (Python) 或 `tracing-subscriber` (Rust) 以支持结构化日志与文件落盘。