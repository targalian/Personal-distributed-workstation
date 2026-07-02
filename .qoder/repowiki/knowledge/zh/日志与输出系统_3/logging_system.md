## 1. 核心结论
该仓库**未建立统一的日志系统**。代码中不存在标准的 `logging` 模块配置、结构化日志框架（如 `structlog`、`loguru`）或集中式日志管理逻辑。

当前的“日志”行为表现为：**直接使用 `print()` 函数向标准输出（stdout）打印带前缀的文本字符串**，且 Uvicorn 底层日志被强制设置为 `warning` 级别以抑制常规 HTTP 访问日志。

## 2. 实现方式与模式

### 2.1 基于 `print` 的控制台输出
所有核心组件（Worker, Secretary, Station, Discovery）均使用 `print()` 进行状态汇报和错误提示。
- **格式约定**：采用 `[组件名] 消息内容` 的简单前缀格式，例如：
  - `[Worker] 设备 ID: ...`
  - `[Secretary] 模型路由器已加载...`
  - `[发现] UDP 绑定端口失败...`
- **缺乏等级控制**：没有区分 `INFO`, `ERROR`, `DEBUG` 等级。所有信息均以相同方式输出，无法通过日志级别过滤噪音。
- **缺乏结构化**：输出为纯文本，不包含时间戳、线程 ID、请求 ID 等上下文信息，难以进行自动化解析或接入 ELK/Splunk 等日志系统。

### 2.2 Web 服务器日志抑制
在 `lan_mesh/secretary.py` 和 `lan_mesh/station_controller.py` 中，启动 Uvicorn 时显式配置了日志级别：
```python
config = uvicorn.Config(
    app,
    host="0.0.0.0",
    port=self.state.api_port,
    log_level="warning",  # 仅显示警告及以上级别，屏蔽 INFO/DEBUG
)
```
这表明开发者有意减少底层框架产生的日志噪音，但未提供替代的应用层日志方案。

### 2.3 启动自检报告
`lan_mesh/preflight.py` 实现了一个基于 `print` 的格式化自检报告，使用 ASCII 字符绘制边框和图标（✅, ❌, ⚠️），用于在启动阶段向用户展示环境检查结果。这属于交互式 CLI 输出，而非系统运行日志。

## 3. 关键文件
- `lan_mesh/worker.py`: 大量使用 `print` 汇报注册、心跳、技能拉取状态。
- `lan_mesh/secretary.py`: 使用 `print` 汇报服务启动、模型加载、离线清理异常。
- `lan_mesh/station_controller.py`: 使用 `print` 汇报 Bot 通道加载、Secretary 模式激活状态。
- `lan_mesh/discovery.py`: 使用 `print` 汇报 UDP 端口绑定冲突及回调异常。
- `lan_mesh/preflight.py`: 负责启动前的环境检查与控制台报告输出。

## 4. 开发者建议
1. **禁止直接 `print`**：在业务逻辑中应避免直接使用 `print`，应引入 Python 标准库 `logging` 或第三方库 `loguru`。
2. **统一日志入口**：建议在 `lan_mesh/__init__.py` 或新建 `lan_mesh/logger.py` 中初始化全局 Logger，配置统一的格式（包含时间、级别、模块名）。
3. **结构化异常处理**：当前代码中多处 `except Exception as e: print(...)` 仅打印了异常消息，丢失了堆栈跟踪信息。应使用 `logger.exception(...)` 记录完整堆栈。
4. **保留 CLI 交互输出**：对于 `preflight.py` 这类面向用户的交互式输出，可保留 `print` 或使用 `rich` 库增强体验，但应与系统运行日志分离。