该代码库采用**防御性编程**与**分层容错**相结合的策略来处理错误。由于缺乏统一的异常类体系，错误处理主要依赖于 `try-except` 块、状态码检查、预检机制（Preflight）以及自动降级/重试逻辑。核心设计目标是确保分布式节点（Worker/Secretary）在部分组件失败时仍能保持基本运行或优雅退出。

### 1. 启动前自检机制 (Preflight Check)
在应用启动初期，通过 `preflight.py` 执行严格的環境检查，防止因配置缺失或权限不足导致运行时崩溃。
- **关键文件**: `lan_mesh/preflight.py`
- **检查项**:
  - Python 版本与核心依赖包完整性。
  - 配置文件存在性（缺失时自动创建默认配置）。
  - 数据目录与共享文件夹的读写权限。
  - 网络接口可用性及端口占用情况（UDP/HHTTP）。
- **处理策略**:
  - **致命错误 (Critical)**: 如 Python 版本过低、核心依赖缺失、端口被占用且无法绑定，直接终止启动 (`sys.exit(1)`)。
  - **非致命警告 (Non-critical)**: 如 HTTP API 端口被占用，程序会自动尝试递增端口并继续运行，仅输出警告信息。
  - **自动修复**: 检测到配置文件缺失时，自动写入默认 YAML 配置。

### 2. 数据库层的兼容性处理
SQLite 数据库层通过“静默忽略”策略处理 schema 演进过程中的冲突，确保旧版本数据库能平滑升级。
- **关键文件**: `lan_mesh/database.py`
- **模式**:
  - 使用 `try-except sqlite3.OperationalError` 捕获 `ALTER TABLE ADD COLUMN` 异常。如果列已存在，则忽略错误，避免启动失败。
  - 在读取数据时，通过 `if "col_name" in r.keys()` 判断字段是否存在，为缺失字段提供默认值，保证向后兼容。

### 3. 网络通信与分布式容错
在 Worker 与 Secretary 的交互中，广泛使用超时控制和连接状态检查来应对网络波动。
- **关键文件**: `lan_mesh/worker.py`, `lan_mesh/orchestrator.py`, `lan_mesh/mcp_client.py`
- **HTTP 请求**:
  - 所有 `requests` 调用均设置 `timeout` 参数（如 5s, 30s, 120s），防止线程无限阻塞。
  - 捕获 `requests.RequestException`，在注册或心跳失败时记录日志并进入重试循环，而不是抛出异常。
- **MCP 网关**:
  - **自动重连**: `MCPGateway` 维护健康检查循环，检测到 Server 断开时自动尝试重连。
  - **路由容错**: 调用工具时，如果指定 Server 不可用，返回结构化错误对象 `{"isError": True, ...}`，而非抛出异常，确保网关服务不中断。
- **子进程管理**:
  - 启动 Secretary 子进程时，使用 `subprocess.TimeoutExpired` 处理停止超时，强制 `kill` 进程以防止僵尸进程残留。

### 4. LLM 调用与任务执行的降级链
Agent 运行时实现了多层级的错误恢复机制，确保在外部 API 不稳定时任务仍能尝试完成。
- **关键文件**: `lan_mesh/agent_runtime.py`, `lan_mesh/orchestrator.py`
- **模型降级链 (Fallback Chain)**:
  - `_call_llm_with_routing` 方法支持配置主选模型和备选模型列表。
  - 当主选模型调用失败（网络错误或 API 限制）时，自动遍历备选模型列表进行重试。
  - 若所有模型均失败，返回包含错误信息的结构化结果，标记任务状态为 `failed` 但不会导致 Agent 进程崩溃。
- **任务编排容错**:
  - `Orchestrator` 在分发子任务时，捕获 HTTP 异常并将子任务状态更新为 `failed`，同时记录错误详情。
  - 支持检测子任务失败后的整体任务状态变更，防止无限等待。

### 5. API 层的异常映射
FastAPI 路由层将内部逻辑错误转换为标准的 HTTP 状态码，便于前端或调用方识别。
- **关键文件**: `lan_mesh/api.py`
- **模式**:
  - 使用 `raise HTTPException(status_code=..., detail=...)` 处理业务逻辑错误（如资源不存在 404、服务未初始化 503、预算不足 402）。
  - 对于文件操作等可能引发系统异常的地方，捕获 `FileNotFoundError` 或 `ValueError` 并转换为 404/403 响应。

### 6. 编码与输出安全
针对跨平台运行时的编码问题，提供了安全的输出包装。
- **关键文件**: `lan_mesh/preflight.py`
- **策略**: `_safe_print` 函数捕获 `UnicodeEncodeError`，在遇到无法编码的字符（如 Emoji 或特殊框线）时，自动替换为 ASCII 等价字符，防止控制台输出崩溃。

### 开发规范建议
1. **禁止裸奔异常**: 所有涉及 I/O（网络、文件、数据库）的操作必须包裹在 `try-except` 中，并记录具体错误原因。
2. **超时必设**: 任何网络请求或子进程调用必须设置合理的 `timeout`。
3. **状态优于异常**: 在分布式通信中，优先返回包含 `ok/error` 字段的结构化数据，而非直接抛出异常，以便调用方决定重试或降级。
4. **兼容性检查**: 修改数据库 Schema 或协议结构时，必须增加字段存在性检查，确保旧数据能平滑迁移。