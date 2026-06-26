## 1. 系统概述
LAN Mesh 分布式智能编排平台目前**未集成专用的日志框架**（如 Python 的 `logging`、`loguru` 或 Rust 的 `tracing`）。系统的可观测性输出完全依赖于各语言内置的标准输出函数：
- **Python 后端 (`lan_mesh`)**：使用 `print()`。
- **Rust 桌面端 (`quicklan-main`)**：使用 `eprintln!` 处理错误，`println!` 处理常规输出。
- **前端 (React)**：使用浏览器原生的 `console` API。

这种设计体现了轻量级、低依赖的开发理念，适用于局域网工具类应用和开发调试阶段，但缺乏结构化日志、动态分级过滤及文件持久化等生产级特性。

## 2. 核心实现与约定

### 2.1 标签化前缀约定
为了在缺乏日志级别的情况下区分模块和角色，开发者遵循 `[组件名] 消息内容` 的前缀格式：
- `[Master]` / `[Secretary]`：主控节点的核心逻辑。
- `[Worker]`：工作节点的生命周期、注册与心跳状态。
- `[发现]`：UDP 广播发现服务的端口绑定与回调异常。
- `[AgentRuntime]`：模型路由调用与降级逻辑。
- `[Orchestrator]`：任务分解与子任务调度进度。
- `[MCP:xxx]`：MCP 客户端的连接与握手状态。

### 2.2 关键文件分布
| 模块 | 关键文件 | 日志职责 |
| :--- | :--- | :--- |
| **Python Worker** | `lan_mesh/worker.py` | 设备 ID 展示、注册结果、心跳失败重试。 |
| **Python Discovery** | `lan_mesh/discovery.py` | UDP 端口冲突警告、发现回调异常捕获。 |
| **Python Preflight** | `lan_mesh/preflight.py` | 启动自检报告，使用 ASCII 字符绘制表格输出 ✅/❌ 状态。 |
| **Rust Backend** | `quicklan-main/src-tauri/src/*.rs` | 底层网络监听失败、单实例互斥锁获取失败等严重错误。 |
| **Web Server** | `lan_mesh/worker.py`, `lan_mesh/master.py` | 显式配置 Uvicorn `log_level="warning"` 以抑制 HTTP 访问噪音。 |

## 3. 架构特征与约束
1. **无日志持久化**：所有日志仅输出至 `stdout/stderr`，进程重启后丢失。排查历史问题需依赖终端回滚或外部重定向（如 `nohup`）。
2. **无动态分级**：无法在运行时动态调整日志详细程度。开发者需通过阅读源码或手动 `grep` 前缀来筛选信息。
3. **同步阻塞风险**：`print()` 和 `eprintln!` 均为同步操作，在高并发场景下可能对性能产生微小影响。
4. **错误处理模式**：
   - **Python**：在后台线程（如 `_heartbeat_loop`）中使用 `try-except` 包裹业务逻辑并打印异常，防止线程静默崩溃。
   - **Rust**：在 `Result::Err` 分支中记录错误，确保关键故障不被吞没。

## 4. 开发者规范
1. **统一前缀**：新增模块输出时，必须使用 `[模块名]` 格式作为前缀。
2. **异常保护**：在独立线程中务必捕获并打印异常，避免“静默失败”。
3. **敏感信息脱敏**：严禁在控制台打印 API Key、密码或完整的用户隐私数据。
4. **演进建议**：若项目进入生产环境，建议引入 `logging` (Python) 或 `tracing-subscriber` (Rust)，并配置统一的日志格式与文件轮转策略。