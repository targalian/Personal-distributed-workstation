该仓库采用**双轨制配置系统**，分别服务于 Python 后端（LAN Mesh）和 Rust/Tauri 前端（QuickLAN）。整体设计遵循“约定优于配置”与“强类型校验”原则，支持 YAML 文件、环境变量及命令行参数的多层级覆盖。

### 1. Python 后端配置 (LAN Mesh)

**核心机制：**
- **框架**：使用 `Pydantic` 进行强类型数据建模与校验，结合 `yaml` 库解析配置文件。
- **加载逻辑**：`lan_mesh/config.py` 定义了 `load_config()` 函数，按以下优先级查找并合并配置：
  1. 显式指定的路径 (`--config` 参数)。
  2. 环境变量 `LAN_MESH_CONFIG` 指向的路径。
  3. 用户主目录下的 `~/.lan_mesh/config.yaml`。
  4. 项目根目录下的 `./config.yaml`。
  5. 若均不存在，则返回包含默认值的 `AppConfig` 实例。

**配置分层与覆盖：**
- **文件配置**：`config.yaml` 定义了发现协议端口、Worker/Secretary 节点的基础端口、共享文件夹路径等。
- **环境变量**：敏感信息（如 API Key）不直接写入 YAML，而是通过 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`），在运行时动态读取。
- **命令行参数**：`main.py` 入口允许通过 `--port`, `--name`, `--shared` 等参数实时覆盖 YAML 中的对应字段，实现灵活的临时部署。

**专项配置模块：**
- **模型池 (Model Pool)**：`model_pool.yaml` 独立管理 LLM 模型元数据（ID、厂商、成本、能力评分、降级链）。通过 `load_model_pool()` 加载，支持从包目录或环境变量 `LAN_MESH_MODEL_POOL` 指定路径。该文件被 `.gitignore` 排除，以保护敏感配置。
- **主机信息采集**：`collect_config.py` 是一个自包含脚本，用于动态采集主机硬件与网络状态（CPU、内存、磁盘、GPU），生成 JSON/TXT 报告，作为运行时环境配置的补充。

### 2. Rust/Tauri 前端配置 (QuickLAN)

**核心机制：**
- **构建配置**：`tauri.conf.json` 管理应用元数据、窗口行为、安全策略及打包选项（NSIS 安装器）。
- **运行时设置**：`src-tauri/src/settings.rs` 实现了 `SettingsService`，负责管理用户偏好（昵称、下载目录）。
  - **存储位置**：配置持久化存储在操作系统标准配置目录下的 `settings.json`（通过 `dirs::config_dir()` 定位）。
  - **容错处理**：加载时若文件缺失或解析失败，自动回退到默认值（如默认昵称为 hostname，默认下载目录为 `~/Downloads/QuickLAN`）。
  - **数据规范化**：保存前对昵称进行清洗（去空格、截断至 32 字符），并确保下载目录存在。

### 3. 开发规范与最佳实践

1. **敏感信息管理**：严禁将 API Key、Token 等敏感信息硬编码在 `config.yaml` 或代码中。必须使用 `api_key_env` 映射到环境变量。
2. **配置扩展性**：新增配置项时，必须在 `lan_mesh/config.py` 中定义对应的 Pydantic Model，并设置合理的默认值，确保向后兼容。
3. **路径处理**：所有涉及文件系统的路径配置（如 `shared_folder`, `db_path`）必须通过 `_expand()` 函数处理，以支持 `~` 和环境变量展开。
4. **多环境适配**：利用 `LAN_MESH_CONFIG` 环境变量区分开发、测试和生产环境的配置文件路径，避免修改代码逻辑。