该仓库**未建立统一的日志框架或结构化日志系统**。日志输出主要依赖 Python 内置的 `print()` 函数进行控制台打印，缺乏日志级别管理、持久化存储及结构化字段支持。

### 1. 核心实现方式
- **原始打印 (`print`)**：全项目（包括 `secretary.py`, `worker.py`, `orchestrator.py`, `discovery.py` 等）广泛使用 `print(f"[Component] Message")` 格式。通过手动添加前缀（如 `[Worker]`, `[Secretary]`, `[Orchestrator]`）来区分来源模块。
- **Web 框架日志抑制**：在 `secretary.py` 和 `worker.py` 中启动 `uvicorn` 时，显式配置 `log_level="warning"`，以抑制 FastAPI/Uvicorn 默认的 INFO/DEBUG 访问日志，减少控制台噪音。
- **启动自检报告**：`preflight.py` 使用 ASCII 艺术风格（边框、图标）直接在 `stdout` 打印启动前的环境检查结果，而非记录到日志文件。

### 2. 关键文件与模式
- **`lan_mesh/secretary.py` & `lan_mesh/worker.py`**：定义了主要的运行时输出逻辑。例如：
  ```python
  print(f"[Worker] 设备 ID: {self.state.device_id}")
  print(f"[Secretary] 服务已启动!")
  ```
- **`lan_mesh/orchestrator.py`**：任务调度状态通过 `print` 实时反馈，如子任务分发、完成或失败信息。
- **`lan_mesh/preflight.py`**：`run_preflight` 函数直接操作 `print` 生成格式化的检查报告。

### 3. 开发者约定与建议
- **当前约定**：若需添加调试或状态信息，直接使用 `print(f"[模块名] 描述信息")`。
- **局限性**：
  - 无法通过环境变量动态调整日志详细程度（Verbose/Quiet）。
  - 生产环境下难以追踪历史日志，因为输出未被重定向至文件或日志收集系统。
  - 缺乏错误堆栈的自动捕获与记录机制（目前仅在部分 `except` 块中打印异常字符串）。
- **改进方向**：建议引入 Python 标准库 `logging` 或 `loguru`，统一配置 `Formatter` 以包含时间戳、线程 ID 和日志级别，并支持将日志输出到 `~/.lan_mesh/logs/` 目录下的文件中。