## 1. 系统/方法概述
该仓库**未引入专用的日志框架**（如 `logging`、`loguru` 或 `structlog`）。日志输出完全依赖 Python 内置的 `print()` 函数，采用**手动格式化字符串**的方式向标准输出（stdout/stderr）写入信息。

- **核心机制**：直接使用 `print(f"[Component] Message")`。
- **结构化程度**：低。仅通过方括号包裹的组件前缀（如 `[Worker]`, `[Secretary]`, `[发现]`）进行简单的来源标识，缺乏统一的日志级别（INFO, WARN, ERROR）管理、时间戳、上下文追踪或结构化字段（JSON）。
- **第三方组件日志**：对于使用的 Web 框架（FastAPI/Uvicorn），在启动时显式将日志级别设置为 `warning` (`log_level="warning"`)，以抑制底层的 HTTP 访问日志和调试信息，保持控制台输出的整洁。

## 2. 关键文件与位置
日志逻辑分散在各个业务模块中，没有统一的日志初始化文件或配置模块。

- **入口与控制器**：
  - `main.py`: 程序入口，无日志输出，仅负责参数解析和角色分发。
  - `lan_mesh/secretary.py`: 包含大量启动状态、端口绑定、Web UI 地址及后台线程异常的 `print` 输出。
  - `lan_mesh/worker.py`: 包含注册状态、心跳失败、Agent Card 同步及子进程管理的 `print` 输出。
- **核心服务**：
  - `lan_mesh/discovery.py`: UDP 广播发现服务，输出端口绑定失败、回调异常等网络层信息。
  - `lan_mesh/orchestrator.py`: 任务编排引擎，输出任务分解、模型路由决策、子任务分发及执行结果。
  - `lan_mesh/preflight.py`: 启动自检模块，使用 ASCII 艺术格式打印详细的检查报告（Python 版本、依赖、端口占用等）。
- **其他模块**：
  - `lan_mesh/bot_gateway.py`: Bot 网关，输出微信/Telegram webhook 响应及轮询状态。
  - `lan_mesh/agent_runtime.py`: Agent 运行时，输出模型调用失败及降级尝试。

## 3. 架构与约定
### 3.1 输出格式约定
开发者遵循一种隐式的命名约定来标识日志来源：
```python
print(f"[{ComponentName}] {Message}")
```
常见组件前缀包括：
- `[Worker]` / `[Secretary]`: 节点角色标识。
- `[发现]`: 网络发现服务（DiscoveryService）。
- `[Orchestrator]`: 任务调度中心。
- `[Router]`: 模型路由决策。
- `[BotGateway]`: 外部通讯网关。

### 3.2 错误处理与静默
- **异常捕获**：在后台线程（如心跳循环、UDP 监听）中，异常通常被 `try-except` 捕获并通过 `print` 输出错误信息，防止线程崩溃。
- **静默模式**：Uvicorn 服务器配置为 `log_level="warning"`，意味着正常的 HTTP 请求（200 OK）不会在控制台产生日志，只有错误或警告才会由框架输出。

### 3.3 启动自检报告
`preflight.py` 提供了一个结构化的文本块输出，用于在启动前验证环境健康度。这是目前系统中唯一具有“仪表盘”风格的日志输出，使用了边框字符和状态图标（✅/❌/⚠️）。

## 4. 开发者应遵循的规则
1. **禁止引入新日志库**：除非项目架构发生重大变更，否则应继续使用 `print` 进行输出，以保持轻量级和无依赖特性。
2. **统一前缀格式**：所有手动日志必须包含 `[Component]` 前缀，以便在混合输出中快速定位来源。
3. **敏感信息脱敏**：由于直接输出到 stdout，严禁在日志中打印完整的 API Key、密码或详细的堆栈跟踪（除非在调试模式下）。
4. **后台线程容错**：在守护线程（daemon threads）中，必须捕获所有异常并打印简要错误信息，避免线程无声退出导致功能失效。
5. **生产环境重定向**：由于缺乏日志文件轮转机制，在生产部署时，建议通过操作系统层面的重定向（如 `nohup ... > lan_mesh.log 2>&1` 或 systemd journal）来管理日志持久化。