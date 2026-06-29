## 1. 系统概述
LAN Mesh 分布式智能编排平台目前**未集成专用的日志框架**（如 Python 标准库 `logging`、`loguru` 或 `structlog`）。系统的可观测性输出完全依赖于内置的 `print()` 函数，通过手动添加前缀标签（如 `[Master]`、`[Worker]`、`[发现]`）来区分模块和角色。这种模式属于**轻量级控制台日志**，适用于开发调试和小型局域网部署场景，但缺乏结构化日志、日志分级过滤、文件持久化及异步写入等生产级特性。

## 2. 核心实现方式
### 2.1 日志输出模式
- **直接打印**：在关键业务逻辑节点（如启动、注册、心跳失败、异常捕获）直接调用 `print()`。
- **标签约定**：
  - `[Master]`：主控节点的核心逻辑（`lan_mesh/master.py`）。
  - `[Worker]`：工作节点的核心逻辑（`lan_mesh/worker.py`）。
  - `[发现]`：UDP 广播发现服务（`lan_mesh/discovery.py`）。
  - `[AgentRuntime]`：代理运行时环境（`lan_mesh/agent_runtime.py`）。
- **异常处理**：在 `try-except` 块中捕获异常后，将错误信息拼接字符串打印到标准输出。

### 2.2 Web 服务器日志抑制
- **Uvicorn 配置**：在启动 FastAPI/Uvicorn 服务时，显式将 `log_level` 设置为 `"warning"`。
  ```python
  config = uvicorn.Config(
      app,
      host="0.0.0.0",
      port=self.state.api_port,
      log_level="warning",  # 抑制 HTTP 访问日志和调试信息
  )
  ```
  这一决策旨在减少高频 HTTP 请求产生的噪音，确保控制台仅显示业务层面的关键状态变更。

## 3. 关键文件分布
| 文件路径 | 职责 | 日志内容示例 |
| :--- | :--- | :--- |
| `lan_mesh/master.py` | 主控节点生命周期 | `[Master] 设备 ID: ...`, `[Master] 清理离线主机异常: ...` |
| `lan_mesh/worker.py` | 工作节点生命周期 | `[Worker] 主机信息已注册到 Master ...`, `[Worker] 心跳失败...` |
| `lan_mesh/discovery.py` | UDP 发现服务 | `[发现] UDP 绑定端口 ... 失败`, `[发现] on_device_seen 回调异常` |
| `lan_mesh/preflight.py` | 启动自检报告 | 使用 ASCII 字符绘制自检表格，输出 ✅/❌ 状态 |
| `lan_mesh/agent_runtime.py` | 模型路由与降级 | `[AgentRuntime] 模型 ... 调用失败, 尝试降级...` |

## 4. 架构约束与开发者规范
### 4.1 当前约束
1. **无日志持久化**：所有日志仅输出至 `stdout/stderr`，进程重启后日志丢失。若需审计，必须依赖外部重定向（如 `nohup` 或 Docker 日志驱动）。
2. **无动态分级**：无法在运行时动态调整日志详细程度（如从 INFO 切换到 DEBUG）。
3. **线程安全隐忧**：虽然 CPython 的 GIL 使得 `print` 在大多数情况下是原子性的，但在高并发或多线程密集输出时，仍可能出现行交错现象。

### 4.2 开发者应遵循的规范
1. **统一前缀**：新增模块输出日志时，必须使用 `[模块名]` 格式作为前缀，以便通过 `grep` 快速过滤。
2. **异常吞没保护**：在后台线程（如 `_heartbeat_loop`, `_listen_loop`）中，务必使用 `try-except` 包裹业务逻辑并打印异常，防止线程静默崩溃。
3. **避免敏感信息**：由于日志直接输出到控制台，严禁打印 API Key、密码或完整的用户隐私数据。
4. **启动自检优先**：所有关键环境依赖检查应集中在 `preflight.py` 中，并通过格式化表格输出，确保启动失败原因清晰可见。

## 5. 演进建议
若项目进入生产环境或需要更复杂的运维支持，建议引入 `logging` 模块并进行如下改造：
- 初始化全局 Logger，配置 `StreamHandler` 和可选的 `FileHandler`。
- 定义统一的日志格式：`%(asctime)s [%(levelname)s] %(name)s - %(message)s`。
- 将现有的 `print()` 调用替换为 `logger.info()`, `logger.error()` 等等价调用。